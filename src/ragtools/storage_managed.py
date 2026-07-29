"""Managed native Qdrant lifecycle (RAG v3, Stage S4).

The installed profile normally runs a pinned native Qdrant server, supervised
by ragtools — no Docker required. This module holds the binary-independent,
unit-testable logic: which release asset a platform needs (and which platforms
have none), the generated server config (loopback-only, telemetry off, low
segment count), and the start -> readiness -> version/identity gate. The real
process and HTTP are injected so this is testable without spawning anything.

Official constraints encoded here:
* No Windows-ARM64 build is published -> refuse (fall back to embedded/external).
* Linux aarch64 ships musl only (no gnu).
* Bind 127.0.0.1 (Qdrant defaults to 0.0.0.0); disable telemetry (on by default).
* Storage must not sit on a synced/FUSE path (documented silent data loss).
* Snapshots restore only within a minor; downgrade is irreversible -> pin.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S4 -> G4)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

from ragtools.devenv import default_synced_detector

#: The exact Qdrant version ragtools manages. Pinned because snapshots restore
#: only within a minor and downgrade is irreversible — the installer ships this
#: build and upgrades are deliberate, chained, and backed up first.
PINNED_QDRANT_VERSION = "1.15.5"


class ManagedStartError(RuntimeError):
    """The managed Qdrant server could not be started or verified safely."""


#: Map (system, machine) -> Qdrant release-asset stem. Absent key => no build.
_ASSET_BY_PLATFORM = {
    ("windows", "amd64"): "qdrant-x86_64-pc-windows-msvc",
    ("windows", "x86_64"): "qdrant-x86_64-pc-windows-msvc",
    ("darwin", "arm64"): "qdrant-aarch64-apple-darwin",
    ("darwin", "aarch64"): "qdrant-aarch64-apple-darwin",
    ("darwin", "x86_64"): "qdrant-x86_64-apple-darwin",
    ("linux", "x86_64"): "qdrant-x86_64-unknown-linux-gnu",
    ("linux", "amd64"): "qdrant-x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "qdrant-aarch64-unknown-linux-musl",
    ("linux", "arm64"): "qdrant-aarch64-unknown-linux-musl",
}


def resolve_qdrant_asset(system: str, machine: str) -> Optional[str]:
    """Release-asset stem for a platform, or ``None`` if no build exists.

    ``None`` (e.g. Windows-ARM64, or any unknown platform) means managed mode
    must refuse and fall back to embedded/external — never guess a binary.
    """
    return _ASSET_BY_PLATFORM.get((system.strip().lower(), machine.strip().lower()))


def generate_qdrant_config(
    *, storage_path: str, http_port: int, grpc_port: int,
    snapshots_path: Optional[str] = None, api_key: Optional[str] = None
) -> dict:
    """Generate the managed server config (serialised to JSON by the caller).

    Loopback-only, telemetry off, and ``default_segment_number = 2`` so a
    collection-per-project layout does not multiply into hundreds of segments.

    ``api_key`` is the per-installation engine credential. It is what turns "is
    this engine mine?" from an inference into an authenticated fact: another
    installation's engine rejects our client outright, so the cross-instance
    adoption behind the v3.1.0 incident stops being possible rather than merely
    unlikely. The engine is loopback-only, so this defends against accidental
    adoption — including by a *foreign* Qdrant that happens to hold the port —
    rather than against a network attacker.
    """
    service: dict = {
        "host": "127.0.0.1",  # NEVER 0.0.0.0 (Qdrant's default)
        "http_port": http_port,
        "grpc_port": grpc_port,
        "enable_cors": False,
    }
    if api_key:
        service["api_key"] = api_key
    return {
        "log_level": "INFO",
        "telemetry_disabled": True,
        "service": service,
        "storage": {
            "storage_path": storage_path,
            "snapshots_path": snapshots_path or str(Path(storage_path).parent / "snapshots"),
            "on_disk_payload": True,
            "optimizers": {
                # 0 = one segment per CPU, per collection. Pin low for many
                # collections. Applied globally.
                "default_segment_number": 2,
            },
        },
    }


class QdrantSupervisor:
    """Start, health-gate, version-verify, and stop a managed Qdrant process.

    The subprocess spawn, HTTP GET, and sleep are injected so the whole
    lifecycle is unit-testable without a real binary or server.
    """

    def __init__(
        self,
        *,
        binary_path: str,
        storage_path: str,
        http_port: int,
        grpc_port: int,
        config_path: Optional[str] = None,
        pinned_version: str = PINNED_QDRANT_VERSION,
        api_key: Optional[str] = None,
        spawn: Callable = subprocess.Popen,
        http_get: Optional[Callable] = None,
        sleep: Optional[Callable] = None,
        is_synced_path: Callable = default_synced_detector,
    ):
        self.binary_path = binary_path
        self.storage_path = storage_path
        self.http_port = http_port
        self.grpc_port = grpc_port
        self.config_path = config_path
        self.pinned_version = pinned_version
        self.api_key = api_key
        self._spawn = spawn
        self._http_get = http_get
        self._sleep = sleep or (lambda s: None)
        self._is_synced_path = is_synced_path
        self._proc = None

    @property
    def proc(self):
        """The spawned child, or None. Read by the ownership checks."""
        return self._proc

    def _get(self, path: str):
        """One authenticated GET against our own engine.

        The key travels on every request, not only the ones Qdrant currently
        demands it for: which endpoints are exempt is a property of the engine
        version, and this call must not start trusting a stranger the day that
        list changes.
        """
        if self._http_get is None:
            raise ManagedStartError("no http_get configured")
        url = f"{self.base_url}{path}"
        if not self.api_key:
            return self._http_get(url)
        try:
            return self._http_get(url, headers={"api-key": self.api_key})
        except TypeError:
            # An injected double that takes only a url. Tests use these, and a
            # signature mismatch must not read as an unreachable engine.
            return self._http_get(url)

    def command(self) -> list:
        """The real spawn command. ``--config-path`` carries the loopback bind,
        ports, and storage path when a generated config was written."""
        cmd = [self.binary_path]
        if self.config_path:
            cmd += ["--config-path", self.config_path]
        return cmd

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    def start(self):
        """Pre-flight the storage path, then spawn the process."""
        if self._is_synced_path(Path(self.storage_path)):
            raise ManagedStartError(
                f"refusing to start managed Qdrant: storage_path {self.storage_path} "
                "is on a synced / FUSE path (documented silent data loss)."
            )
        self._proc = self._spawn(self.command())
        return self._proc

    def wait_ready(self, timeout: float = 30.0, interval: float = 0.5):
        """Poll ``/readyz`` until 200, the child dies, or the timeout expires.

        **The child's liveness is checked first, every iteration.** This is the
        v3.1.0 defect, and it is the whole of it: a 200 on this port used to be
        accepted as proof the engine had started, when it only ever proved that
        *something* had answered. A second instance whose own child had just
        died of address-in-use polled the canonical engine, got 200, and adopted
        another installation's store.

        A process that has exited cannot be the thing answering. So an exited
        child is a hard failure here — and, decisively, it is reported as
        ``address in use`` rather than as a timeout, because the operator needs
        to know a rival engine is running, not that theirs was slow.
        """
        if self._http_get is None:
            raise ManagedStartError("no http_get configured")
        waited = 0.0
        while waited <= timeout:
            self._assert_child_alive()
            try:
                r = self._get("/readyz")
                if getattr(r, "status_code", 0) == 200:
                    return True
            except Exception:
                pass
            self._sleep(interval)
            waited += interval
        self._assert_child_alive()
        raise ManagedStartError(
            f"managed Qdrant did not become ready within {timeout}s"
        )

    def _assert_child_alive(self) -> None:
        """Refuse the moment our own process is gone."""
        proc = self._proc
        if proc is None:
            return
        code = proc.poll()
        if code is None:
            return
        raise ManagedStartError(
            f"the managed Qdrant we started exited during startup (exit code "
            f"{code}). Anything now answering on port {self.http_port} belongs "
            f"to another process — most commonly a second RAG Tools instance "
            f"whose engine already holds this port. Refusing to adopt it."
        )

    def verify_version(self):
        """GET / and refuse if the engine version != the pinned version.

        Kept, but no longer load-bearing for ownership: every instance ships the
        same pinned build, so a version match discriminates nothing between two
        of *our* engines. It catches a foreign or mismatched engine only. The
        ownership proof is the child check above plus the API key.
        """
        r = self._get("/")
        body = r.json() if hasattr(r, "json") else {}
        version = (body or {}).get("version", "")
        if version != self.pinned_version:
            raise ManagedStartError(
                f"managed Qdrant version mismatch: engine reports {version!r}, "
                f"expected pinned {self.pinned_version!r}. Refusing to trust it."
            )
        return version

    def stop(self, timeout: float = 10.0):
        """Graceful terminate; idempotent."""
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=timeout)
        except Exception:
            pass
        finally:
            self._proc = None
