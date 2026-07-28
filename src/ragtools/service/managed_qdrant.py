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
import os
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

    http_port = int(os.environ.get("RAG_QDRANT_HTTP_PORT", DEFAULT_HTTP_PORT))
    grpc_port = int(os.environ.get("RAG_QDRANT_GRPC_PORT", DEFAULT_GRPC_PORT))
    storage = str(Path(settings.data_dir) / "qdrant-server")
    return ManagedPlan(
        should_start=True,
        reason=f"managed Qdrant {PINNED_QDRANT_VERSION} from {binary}",
        binary=binary,
        http_port=http_port,
        grpc_port=grpc_port,
        storage_path=storage,
        url=f"http://127.0.0.1:{http_port}",
    )


def start_managed_qdrant(settings, plan: Optional[ManagedPlan] = None):
    """Execute a plan: write the config, spawn, health-gate, verify the version.

    Returns ``(supervisor, url)`` on success or ``(None, None)`` when managed
    mode is not in play or could not start — the caller then uses embedded and
    surfaces the reason.
    """
    import httpx
    import yaml

    from ragtools.storage_managed import QdrantSupervisor, generate_qdrant_config

    plan = plan or plan_managed_startup(settings)
    if not plan.should_start:
        logger.info("Managed Qdrant not started: %s", plan.reason)
        return None, None

    storage = Path(plan.storage_path)
    storage.mkdir(parents=True, exist_ok=True)
    cfg = generate_qdrant_config(
        storage_path=str(storage),
        http_port=plan.http_port,
        grpc_port=plan.grpc_port,
        snapshots_path=str(storage.parent / "qdrant-snapshots"),
    )
    cfg_path = storage.parent / "qdrant-config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    supervisor = QdrantSupervisor(
        binary_path=plan.binary,
        storage_path=str(storage),
        http_port=plan.http_port,
        grpc_port=plan.grpc_port,
        config_path=str(cfg_path),
        http_get=httpx.get,
        sleep=__import__("time").sleep,
    )
    try:
        supervisor.start()
        supervisor.wait_ready(timeout=60)
        version = supervisor.verify_version()
        logger.info("Managed Qdrant %s ready on %s", version, plan.url)
        return supervisor, plan.url
    except Exception as exc:  # noqa: BLE001 — must never block service startup
        logger.warning("Managed Qdrant failed to start (%s); using embedded", exc)
        try:
            supervisor.stop()
        except Exception:
            pass
        return None, None
