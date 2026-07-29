"""Managed Qdrant boot integration (Phase 5 / W5-B).

`QdrantSupervisor` (S4) could start and health-gate a pinned Qdrant server but
was never wired into service startup. This module is that wiring: decide whether
to start one, find the binary, and hand back a plan the lifespan can execute.

**Why this matters more than anything else in the plan.** Qdrant "local mode" is
not embedded Qdrant — it is ~8,166 lines of pure Python doing brute-force search.
Measured on the live install: 2.11 s per query at 147k points, scaling linearly.
Running the real engine is how that becomes milliseconds, and it also removes the
exclusive single-process lock that shaped the whole service design.

**Degrade honestly.** No Qdrant build is published for some platforms (notably
Windows ARM64). When managed mode cannot start, the service falls back to
embedded and says why — it never guesses a binary and never fails to boot.

Plan: docs/planning/RAG_MODERNIZATION_MASTER_PLAN.md  (Phase 5 -> G5, D5)
"""

from __future__ import annotations

import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ragtools.platform import current_platform, host_machine, host_system
from ragtools.storage_managed import (  # noqa: F401 — re-exported for monkeypatching
    PINNED_QDRANT_VERSION,
    resolve_qdrant_asset,
)

logger = logging.getLogger("ragtools.service")

#: Loopback ports for the managed engine. Deliberately clear of the service's
#: own 21420/21422 range so a stray managed instance can never be mistaken for
#: the API (see the instance-identity finding).
#:
#: These are DEFAULTS, not constants. They were effectively constants in v3.1.0
#: — overridable only by environment variables nothing set — so every managed
#: instance on a machine targeted the same port while writing to a different
#: storage directory. The ports collided; the data directories did not; the
#: loser silently wrote into the winner's store. See
#: :func:`ragtools.service.engine_ownership.resolve_engine_ports`.
DEFAULT_HTTP_PORT = 21500
DEFAULT_GRPC_PORT = 21501

def _binary_name() -> str:
    """Executable name for the host platform."""
    return "qdrant.exe" if current_platform() == "windows" else "qdrant"


_BINARY_NAME = _binary_name()


class ManagedUnavailable(RuntimeError):
    """Managed mode was explicitly configured but cannot be honoured."""


@dataclass
class ManagedPlan:
    """The decision, made before any process is spawned."""

    should_start: bool
    reason: str
    fallback_to_embedded: bool = False
    binary: Optional[str] = None
    http_port: int = DEFAULT_HTTP_PORT
    grpc_port: int = DEFAULT_GRPC_PORT
    storage_path: Optional[str] = None
    url: Optional[str] = None
    #: This installation's engine credential and identity. Carried on the plan so
    #: the caller writes exactly what it verified against.
    api_key: Optional[str] = None
    instance_id: Optional[str] = None
    #: True when this instance explicitly declared itself a deliberate secondary
    #: (non-default ports AND a configured ``instance_id``).
    explicit_secondary: bool = False


def _candidate_dirs(settings) -> list:
    """Where a bundled or downloaded Qdrant binary may live, in priority order."""
    dirs = []
    # 1. NEXT TO THE RUNNING EXECUTABLE — where the installer actually puts it.
    #
    # This looked only in `_get_app_dir()`, under a comment saying "the
    # installer ships it here". It does not: `app_dir()` is the DATA directory
    # (`%LOCALAPPDATA%\RAGTools`), and the bundle — engine included — installs
    # to the PROGRAM directory (`%LOCALAPPDATA%\Programs\RAGTools\bin`). So the
    # engine shipped correctly and was invisible, and a packaged upgrade
    # adopted `embedded` while a perfectly good `qdrant.exe` sat beside the
    # binary doing the looking.
    try:
        here = Path(sys.executable).resolve().parent
        dirs += [here / "bin", here, here / "qdrant"]
    except Exception:
        pass
    # 2. The data directory, which is where a future first-run download would
    #    land — a different location for a different reason.
    try:
        from ragtools.config import _get_app_dir  # type: ignore
        app_dir = _get_app_dir()
        if app_dir:
            dirs += [Path(app_dir), Path(app_dir) / "bin", Path(app_dir) / "qdrant"]
    except Exception:
        pass
    # 2. Beside the interpreter / project root (dev checkouts).
    dirs.append(Path(sys.prefix) / "bin")
    # 3. A managed cache under the data dir (first-run download target).
    try:
        dirs.append(Path(settings.data_dir) / "bin")
    except Exception:
        pass
    return dirs


def find_qdrant_binary(settings) -> Optional[str]:
    """Locate the Qdrant executable, or ``None`` if there is none.

    An explicitly configured path that does not exist raises rather than being
    silently ignored — a misconfiguration must not look like "unsupported".
    """
    explicit = getattr(settings, "qdrant_binary", None)
    if explicit:
        if Path(explicit).is_file():
            return str(explicit)
        raise ManagedUnavailable(
            f"qdrant_binary is set to {explicit!r} but that file was not found"
        )

    for d in _candidate_dirs(settings):
        try:
            candidate = Path(d) / _BINARY_NAME
            if candidate.is_file():
                return str(candidate)
        except Exception:
            continue

    on_path = shutil.which("qdrant")
    if on_path:
        return on_path
    return None


def plan_managed_startup(settings) -> ManagedPlan:
    """Decide whether to start a managed engine. Spawns nothing."""
    mode = (getattr(settings, "storage_backend", "embedded") or "embedded").strip().lower()

    if mode == "embedded":
        return ManagedPlan(False, "storage_backend is 'embedded'; nothing to start")
    if mode == "external":
        return ManagedPlan(
            False,
            "storage_backend is 'external'; the server is managed outside ragtools",
        )
    if mode != "managed":
        return ManagedPlan(False, f"unknown storage_backend {mode!r}",
                           fallback_to_embedded=True)

    # `resolve_qdrant_asset` is a pure (system, machine) -> asset function and
    # stays that way; only the HOST lookup belongs behind the seam.
    asset = resolve_qdrant_asset(host_system(), host_machine())
    if asset is None:
        return ManagedPlan(
            False,
            f"no Qdrant build exists for this platform "
            f"({host_system()}/{host_machine()}); falling back to embedded",
            fallback_to_embedded=True,
        )

    binary = find_qdrant_binary(settings)
    if not binary:
        return ManagedPlan(
            False,
            "managed mode requested but no Qdrant binary was found; "
            "falling back to embedded",
            fallback_to_embedded=True,
        )

    from ragtools.service.engine_ownership import (
        engine_identity,
        resolve_engine_ports,
    )

    http_port, grpc_port, explicit_secondary = resolve_engine_ports(
        settings, default_http=DEFAULT_HTTP_PORT, default_grpc=DEFAULT_GRPC_PORT)
    identity = engine_identity(settings)
    storage = str(Path(settings.data_dir) / "qdrant-server")
    return ManagedPlan(
        should_start=True,
        reason=f"managed Qdrant {PINNED_QDRANT_VERSION} from {binary}",
        binary=binary,
        http_port=http_port,
        grpc_port=grpc_port,
        storage_path=storage,
        url=f"http://127.0.0.1:{http_port}",
        api_key=identity.get("api_key"),
        instance_id=identity.get("instance_id"),
        explicit_secondary=explicit_secondary,
    )


def start_managed_qdrant(settings, plan: Optional[ManagedPlan] = None):
    """Execute a plan: write the config, spawn, health-gate, verify the version.

    Returns ``(supervisor, url)`` on success or ``(None, None)`` when managed
    mode is not in play or could not start — the caller then uses embedded and
    surfaces the reason.
    """
    import time

    import httpx
    import yaml

    from ragtools.service.engine_ownership import (
        EngineClaim,
        NotOurEngine,
        inspect_port,
        verify_ownership,
        write_manifest,
    )
    from ragtools.storage_managed import QdrantSupervisor, generate_qdrant_config

    plan = plan or plan_managed_startup(settings)
    if not plan.should_start:
        logger.info("Managed Qdrant not started: %s", plan.reason)
        return None, None

    # RESOLVE THE PORT BEFORE SPAWNING ANYTHING.
    #
    # This is the ordering that prevents the incident, and it also makes "a
    # failed secondary cannot kill the canonical engine" true by construction:
    # a refusal happens here, so there is no failed child to clean up and no
    # cleanup path to get wrong. Adoption is decided from OUR MANIFEST, never
    # from the fact that a port answered.
    verdict = inspect_port(settings, plan.http_port)
    if verdict.action == "refuse":
        logger.error(
            "Managed Qdrant refused: %s. This installation will use embedded "
            "storage instead of writing into a store it does not own. Run one "
            "canonical service per machine, or give this instance explicit "
            "qdrant_http_port/qdrant_grpc_port and instance_id.", verdict.reason)
        return None, None
    if verdict.action == "reattach" and verdict.claim is not None:
        logger.info("Managed Qdrant: %s", verdict.reason)
        supervisor = QdrantSupervisor(
            binary_path=plan.binary or verdict.claim.executable,
            storage_path=verdict.claim.storage_path,
            http_port=verdict.claim.http_port,
            grpc_port=verdict.claim.grpc_port,
            pinned_version=PINNED_QDRANT_VERSION,
            api_key=plan.api_key,
            http_get=httpx.get,
            sleep=time.sleep,
        )
        try:
            supervisor.verify_version()
            return supervisor, f"http://127.0.0.1:{verdict.claim.http_port}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not reattach to our own engine (%s); using "
                           "embedded", exc)
            return None, None

    storage = Path(plan.storage_path or "")
    storage.mkdir(parents=True, exist_ok=True)
    cfg = generate_qdrant_config(
        storage_path=str(storage),
        http_port=plan.http_port,
        grpc_port=plan.grpc_port,
        snapshots_path=str(storage.parent / "qdrant-snapshots"),
        api_key=plan.api_key,
    )
    cfg_path = storage.parent / "qdrant-config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    supervisor = QdrantSupervisor(
        binary_path=plan.binary or "",
        storage_path=str(storage),
        http_port=plan.http_port,
        grpc_port=plan.grpc_port,
        config_path=str(cfg_path),
        api_key=plan.api_key,
        # Without this the engine has nowhere to write, and under the windowed
        # launcher "nowhere" is a pipe with no reader — every log line it emits
        # fails, which is why the v3.2.0 crash left no evidence at all.
        data_dir=str(settings.data_dir),
        http_get=httpx.get,
        sleep=time.sleep,
    )
    try:
        proc = supervisor.start()
        supervisor.wait_ready(timeout=60)
        claim = EngineClaim(
            instance_id=plan.instance_id or "unknown",
            pid=int(getattr(proc, "pid", 0) or 0),
            executable=str(plan.binary or ""),
            storage_path=str(storage),
            http_port=plan.http_port,
            grpc_port=plan.grpc_port,
            started_at=time.time(),
        )
        verify_ownership(claim, proc=proc)
        version = supervisor.verify_version()
        write_manifest(settings, claim)
        # The lifecycle record. Every field here answers a question that was
        # unanswerable while the v3.2.0 engine was dying: which process, which
        # binary, which store, and where its own account of itself went.
        # `getattr`, not attribute access: an injected double is a legitimate
        # supervisor here, and a diagnostic line must never be the reason the
        # engine "fails to start".
        log_path = getattr(supervisor, "log_path", None)
        log_error = getattr(supervisor, "log_error", "") or "no engine log"
        logger.info("Managed Qdrant %s ready on %s (pid=%s instance=%s exe=%s "
                    "storage=%s log=%s)",
                    version, plan.url, claim.pid, claim.instance_id,
                    claim.executable, claim.storage_path,
                    log_path or f"UNAVAILABLE ({log_error})")
        return supervisor, plan.url
    except NotOurEngine as exc:
        # Distinguished from a start failure on purpose: "the engine that is
        # running belongs to someone else" and "our engine failed to start" want
        # different words, and collapsing them is what produced a silent
        # adoption in the first place.
        logger.error("Managed Qdrant ownership check failed: %s. Using embedded "
                     "storage rather than another installation's store.", exc)
        _stop_quietly(supervisor)
        return None, None
    except Exception as exc:  # noqa: BLE001 — must never block service startup
        logger.warning("Managed Qdrant failed to start (%s); using embedded", exc)
        _stop_quietly(supervisor)
        return None, None


def _stop_quietly(supervisor) -> None:
    """Stop the child WE spawned. Never reaches for anything else."""
    try:
        supervisor.stop()
    except Exception:  # noqa: BLE001
        pass
