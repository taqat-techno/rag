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

import logging
import subprocess
from pathlib import Path
from typing import Callable, Optional

from ragtools.devenv import default_synced_detector

logger = logging.getLogger("ragtools.service")

#: The exact Qdrant version ragtools manages. Pinned because snapshots restore
#: only within a minor and downgrade is irreversible — the installer ships this
#: build and upgrades are deliberate, chained, and backed up first.
PINNED_QDRANT_VERSION = "1.15.5"

#: Engine log rotation. Matches the service log policy so one operator habit
#: covers both files.
ENGINE_LOG_NAME = "qdrant.log"
ENGINE_LOG_MAX_BYTES = 10 * 1024 * 1024
#: Five, not three. Each engine process now gets its own generation, and the
#: bounded restart policy alone can burn three in under two minutes — which
#: would roll the crash that started the storm off the end of the shelf.
ENGINE_LOG_BACKUPS = 5


class ManagedStartError(RuntimeError):
    """The managed Qdrant server could not be started or verified safely."""


def engine_log_path(data_dir) -> Path:
    """Where the engine's own output goes. Beside ``service.log``."""
    return Path(data_dir) / "logs" / ENGINE_LOG_NAME


def rotate_engine_log(path: Path, *, max_bytes: int = ENGINE_LOG_MAX_BYTES,
                      backups: int = ENGINE_LOG_BACKUPS,
                      per_instance: bool = True) -> None:
    """Roll the engine log so ONE FILE HOLDS ONE ENGINE'S LIFE.

    Rotation happens at START, never on a size trigger while the engine runs,
    because the writer is a child process holding the handle — renaming a file
    out from under it is how you get a log that silently stops.

    ``per_instance`` is the v3.4.0 change and it is the point. v3.3.0 rotated
    only past 10 MB, so a *small* log from an engine that died was appended to by
    its successor and the two runs interleaved in one file with nothing marking
    the boundary. Reading it back, you could not tell which lines belonged to the
    process that crashed. Now every start rolls a non-empty file aside, so
    ``qdrant.log.1`` is exactly the previous engine and nothing else.

    Failure is non-fatal by design: on Windows a rename fails while another
    process still holds the file open — precisely the orphaned-engine case — and
    appending to a shared file beats refusing to log at all.
    """
    try:
        if not path.is_file():
            return
        size = path.stat().st_size
        if size == 0:
            return
        if not per_instance and size < max_bytes:
            return
        for n in range(backups - 1, 0, -1):
            older, newer = path.with_suffix(f".log.{n}"), path.with_suffix(f".log.{n + 1}")
            if older.is_file():
                older.replace(newer)
        path.replace(path.with_suffix(".log.1"))
    except OSError as exc:
        logger.warning("could not rotate the engine log at %s: %s "
                       "(appending instead; the previous run's output is kept)",
                       path, exc)


def open_engine_log(data_dir, *, per_instance: bool = True):
    """``(handle, path)`` for the engine's output, or ``(None, None)``.

    A REAL OS handle, because that is the only kind a child process can inherit
    — the same reasoning `_streams.py` applies to the null device. Returning
    ``None`` is not a failure to paper over: :meth:`QdrantSupervisor.start`
    falls back to ``DEVNULL``, never to inheritance.
    """
    path = engine_log_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate_engine_log(path, per_instance=per_instance)
        return open(path, "ab", buffering=0), path
    except OSError as exc:
        logger.warning("could not open the engine log at %s: %s — the engine's "
                       "output will be discarded this run", path, exc)
        return None, None


def write_engine_marker(data_dir, text: str) -> bool:
    """Append one SERVICE-written line to the engine log. Never raises.

    The engine's own output says what it was doing; it cannot say which pid it
    was, when we started it, or why it stopped — a process that dies does not
    get to write its own epitaph. So the supervisor writes the boundaries, into
    the same file, and a reader gets one coherent story instead of two half
    ones in different places.
    """
    try:
        path = engine_log_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab", buffering=0) as handle:
            handle.write((text.rstrip("\n") + "\n").encode("utf-8", "replace"))
        return True
    except OSError as exc:
        logger.warning("could not write an engine-log marker: %s", exc)
        return False


def engine_marker(kind: str, **fields) -> str:
    """A machine-greppable marker line: ``=== ragtools <kind> k=v k=v ===``."""
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    return f"=== ragtools {kind} at={stamp} {body} ==="


def _spawn_kwargs(stream) -> dict:
    """Keyword arguments that keep the child off an inherited handle.

    THE DEFECT THIS EXISTS TO PREVENT. `Popen(cmd)` with no `stdout=` inherits
    the parent's handles. Under the windowed launcher (`ragw.exe`, a
    GUI-subsystem build with no console) the parent HAS no standard handles, so
    CPython creates an anonymous pipe, hands the child the write end, and closes
    the read end immediately — `subprocess.Popen._get_handles`. The child then
    holds a write handle to a pipe with no reader, and every write it makes
    fails with ERROR_BROKEN_PIPE for the entire life of the process. Measured,
    not assumed: a child in that position reports "The process tried to write to
    a nonexistent pipe."

    So the engine is given somewhere real to write, or `DEVNULL`, and never the
    thing it was given before.
    """
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": stream if stream is not None else subprocess.DEVNULL,
        "stderr": subprocess.STDOUT if stream is not None else subprocess.DEVNULL,
    }
    # Console-window suppression is a PLATFORM question, and this project keeps
    # every platform branch behind one seam — `ragtools.platform`. Asking
    # `sys.platform` here would put dispatch back in a module that has no
    # business knowing which OS it is on, which a structural test correctly
    # refuses. See `PlatformAdapter.child_process_flags`.
    try:
        from ragtools.platform import adapter

        kwargs.update(adapter().child_process_flags())
    except Exception:  # noqa: BLE001 — an unsupported platform still gets a sink
        pass
    return kwargs


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
        data_dir: Optional[str] = None,
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
        self.data_dir = data_dir
        self._spawn = spawn
        self._http_get = http_get
        self._sleep = sleep or (lambda s: None)
        self._is_synced_path = is_synced_path
        self._proc = None
        #: Where the engine's output went this run, and why if it went nowhere.
        #: Surfaced on /health: a logging failure must not block the engine, but
        #: it must not be invisible either.
        #:
        #: Known from ``data_dir`` ALONE, before anything is opened, because a
        #: REATTACHED engine never calls :meth:`start` — and v3.3.0 therefore
        #: reported ``log_path: null`` for it with no ``log_error`` to explain
        #: the null. "There is no log" and "the log is over there, written by
        #: the process that spawned it" are different facts.
        self.log_path: Optional[str] = (
            str(engine_log_path(data_dir)) if data_dir else None)
        self.log_error: str = ""
        self._log_handle = None

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
        """Pre-flight the storage path, then spawn the process onto a real sink.

        The spawn is never bare. ``stdout``/``stderr`` go to ``qdrant.log`` when
        one can be opened and to ``DEVNULL`` when one cannot — see
        :func:`_spawn_kwargs` for why inheritance is the one option that is
        never taken.
        """
        if self._is_synced_path(Path(self.storage_path)):
            raise ManagedStartError(
                f"refusing to start managed Qdrant: storage_path {self.storage_path} "
                "is on a synced / FUSE path (documented silent data loss)."
            )

        stream = None
        if self.data_dir:
            stream, path = open_engine_log(self.data_dir)
            if stream is None:
                self.log_error = ("the engine log could not be opened; this "
                                  "run's engine output is discarded")
            else:
                self._log_handle = stream
                self.log_path = str(path)
        else:
            self.log_error = ("no data directory was given to the supervisor, "
                              "so this engine has nowhere to write")

        try:
            self._proc = self._spawn(self.command(), **_spawn_kwargs(stream))
        except TypeError:
            # An injected double that accepts only the command. Tests use these,
            # and a signature mismatch must not stop the engine from starting.
            self._proc = self._spawn(self.command())

        # The service writes the boundary the engine cannot write for itself.
        # A crashed process does not get to record which pid it was or when it
        # started, and that is exactly what you need when reading `qdrant.log.1`
        # back after the fact.
        if self.data_dir and stream is not None:
            write_engine_marker(self.data_dir, engine_marker(
                "engine-start",
                pid=getattr(self._proc, "pid", None),
                exe=self.binary_path,
                storage=self.storage_path,
                http=self.http_port,
                grpc=self.grpc_port,
            ))
        return self._proc

    def _close_log(self) -> None:
        """Drop our copy of the log handle. The child keeps its own."""
        handle, self._log_handle = self._log_handle, None
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass

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
            self._close_log()
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=timeout)
        except Exception:
            pass
        finally:
            self._proc = None
            self._close_log()

    def describe(self) -> dict:
        """The lifecycle record an operator needs to trace one engine.

        Every field here answers a question that was unanswerable during the
        v3.2.0 incident: which process, which binary, which store, which ports,
        and where its own account of itself went.
        """
        proc = self._proc
        return {
            "pid": getattr(proc, "pid", None),
            "executable": self.binary_path,
            "argv": self.command(),
            "http_port": self.http_port,
            "grpc_port": self.grpc_port,
            "storage_path": self.storage_path,
            "config_path": self.config_path,
            "pinned_version": self.pinned_version,
            "log_path": self.log_path,
            "log_error": self.log_error,
        }
