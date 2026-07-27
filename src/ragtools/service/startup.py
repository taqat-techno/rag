"""Service autostart — a thin delegate over the platform adapter.

This module used to be 205 lines of Windows: a Startup-folder path, a generated
``.vbs`` launcher, ``wscript``, and a ``schtasks`` fallback. There was no Linux
or macOS path at all, so "autostart" simply did not exist on two of the three
first-class platforms.

All of that now lives in :mod:`ragtools.platform`, one implementation per OS.
What remains here is the product-level shape of the registration — which command
to run, under what name, with what delay — plus the public functions the CLI,
service boot and health check already import, so no caller had to change.

The legacy constants stay on purpose: the upgrade engine needs the names of the
registrations previous versions created in order to remove them.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from ragtools.config import Settings
from ragtools.platform import (
    KIND_SERVICE,
    AutostartSpec,
    PlatformUnsupported,
    adapter,
    assert_single_registration,
    background_executable,
)

logger = logging.getLogger("ragtools.service")

#: Registration names created by superseded versions. Referenced by the upgrade
#: engine's cleanup; never created here.
TASK_NAME = "RAGTools Service"
STARTUP_FILENAME = "RAGTools.vbs"

#: Wait after login before starting. The encoder takes tens of seconds to load;
#: racing the rest of the login makes both slower.
DEFAULT_DELAY_SECONDS = 30

#: What autostart starts. Registering "start at login" means the installed
#: profile — a login-time service on a developer's dev data root is not what
#: anyone asked for, and leaving it implicit made the answer depend on the OS.
AUTOSTART_PROFILE = "installed"


def _service_argv(settings: Settings) -> list[str]:
    """The command the OS should run at login.

    A packaged build launches its own executable; a source checkout launches the
    running interpreter with ``-m``, so a developer's registration points at
    their checkout rather than at an installed copy that may not exist.

    The packaged executable is resolved through ``background_executable`` rather
    than used directly: on Windows ``sys.executable`` is the console-subsystem
    ``rag.exe``, and naming it in a scheduled task is what put a terminal window
    on the desktop at every login in v3.0.0.
    """
    from ragtools.config import is_packaged

    host = settings.service_host
    port = str(settings.service_port)
    # `--profile` rather than only an environment variable: a Windows scheduled
    # task has nowhere to put one, so the profile declared below was honoured on
    # Linux and macOS and silently dropped on Windows — where a source checkout
    # then falls back to `dev` and the autostarted service comes up on a
    # different data root than the one that registered it. An argument is
    # carried by every mechanism.
    tail = ["--host", host, "--port", port, "--profile", AUTOSTART_PROFILE]
    if is_packaged():
        return [background_executable(sys.executable), "service", "run", *tail]
    return [sys.executable, "-m", "ragtools.cli", "service", "run", *tail]


def _spec(settings: Settings, delay_seconds: int | None = None) -> AutostartSpec:
    delay = getattr(settings, "startup_delay", DEFAULT_DELAY_SECONDS)
    return AutostartSpec(
        name=TASK_NAME,
        kind=KIND_SERVICE,
        argv=_service_argv(settings),
        description="RAG Tools — local knowledge-base service",
        delay_seconds=delay if delay_seconds is None else delay_seconds,
        # Kept for the mechanisms that CAN express it (systemd, launchd); the
        # argv flag is what makes it true everywhere, including Windows.
        environment={"RAG_PROFILE": AUTOSTART_PROFILE},
    )


def install_task(settings: Settings, delay_seconds: int | None = None) -> bool:
    """Register the service to start at login. Idempotent."""
    try:
        registration = adapter().install_autostart(_spec(settings, delay_seconds))
    except PlatformUnsupported as exc:
        logger.warning("Autostart unavailable: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error("Autostart registration failed: %s", exc)
        return False
    logger.info("Registered service autostart: %s", registration.describe())
    return True


def uninstall_task() -> bool:
    """Remove the service registration, including superseded mechanisms.

    Idempotent: removing nothing is success, so an interrupted uninstall can be
    re-run without stranding the machine half-configured.
    """
    try:
        impl = adapter()
    except PlatformUnsupported:
        return True
    removed = []
    for registration in impl.find_autostart(KIND_SERVICE):
        removed.extend(impl.remove_autostart(registration.name))
    for registration in removed:
        logger.info("Removed service autostart: %s", registration.describe())
    return True


def is_task_installed() -> bool:
    """Whether a current (non-legacy) registration exists."""
    try:
        found = adapter().find_autostart(KIND_SERVICE)
    except PlatformUnsupported:
        return False
    return any(not r.legacy for r in found)


def get_task_info() -> dict | None:
    """Registration detail for ``rag doctor`` and ``/api/diagnostics``.

    Reports duplicates and superseded entries rather than the first match. The
    development machine ran a scheduled task *and* two Startup-folder scripts
    simultaneously, and every "is it installed?" check answered yes — which is
    why nothing ever cleaned them up.
    """
    try:
        impl = adapter()
        found = impl.find_autostart(KIND_SERVICE)
    except PlatformUnsupported:
        return None
    if not found:
        return None

    problem = ""
    try:
        assert_single_registration(found, KIND_SERVICE)
    except Exception as exc:  # noqa: BLE001 — the message IS the finding
        problem = str(exc)

    current = [r for r in found if not r.legacy]
    primary = current[0] if current else found[0]
    return {
        "task_name": primary.name,
        "status": "Installed" if current else "Legacy only",
        "location": str(primary.path) if primary.path else primary.target,
        "method": primary.mechanism,
        "platform": impl.name,
        "registrations": [r.describe() for r in found],
        "legacy": [r.describe() for r in found if r.legacy],
        "problem": problem,
    }


def legacy_startup_folder() -> Path | None:
    """Legacy Startup-folder location, for cleanup only. Nothing writes here."""
    try:
        return getattr(adapter(), "_startup", None)
    except PlatformUnsupported:
        return None
