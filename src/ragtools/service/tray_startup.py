"""Tray autostart — a thin delegate over the platform adapter.

Sibling of :mod:`ragtools.service.startup`, and separate from it on purpose: a
headless machine has a service and no tray, so the two registrations have
different lifetimes and must be independently installable and removable.

Was Windows-only (a second ``.vbs`` in the Startup folder). The per-OS
mechanism now lives in :mod:`ragtools.platform`; what stays here is the
product's answer to "should there even be a tray on this machine".
"""

from __future__ import annotations

import logging
import sys

from ragtools.config import Settings
from ragtools.platform import (
    KIND_TRAY,
    AutostartSpec,
    PlatformUnsupported,
    adapter,
)

logger = logging.getLogger("ragtools.service")

#: Created by superseded versions; referenced by upgrade cleanup, never written.
TRAY_STARTUP_FILENAME = "RAGTools-Tray.vbs"


def _tray_argv() -> list[str]:
    from ragtools.config import is_packaged

    if is_packaged():
        return [sys.executable, "tray"]
    return [sys.executable, "-m", "ragtools.cli", "tray"]


def install_tray_task(settings: Settings) -> bool:
    """Register the tray to start at login — where a desktop session exists.

    A headless install must remain fully supported *without* the tray, so this
    refuses rather than registering something that will fail at every login.
    """
    try:
        impl = adapter()
    except PlatformUnsupported as exc:
        logger.warning("Tray autostart unavailable: %s", exc)
        return False

    if not impl.has_desktop_session():
        logger.info(
            "Tray autostart skipped: no desktop session on this machine. "
            "The service runs headless without it."
        )
        return False

    try:
        registration = impl.install_autostart(AutostartSpec(
            name="RAGTools Tray",
            kind=KIND_TRAY,
            argv=_tray_argv(),
            description="RAG Tools — status tray icon",
            # Shorter than the service delay: the tray only needs to outwait
            # the shell, and it shows "starting" while the service loads.
            delay_seconds=10,
            environment={"RAG_PROFILE": "installed"},
        ))
    except Exception as exc:  # noqa: BLE001
        logger.error("Tray autostart registration failed: %s", exc)
        return False
    logger.info("Registered tray autostart: %s", registration.describe())
    return True


def uninstall_tray_task() -> bool:
    """Remove the tray registration, including superseded mechanisms."""
    try:
        impl = adapter()
    except PlatformUnsupported:
        return True
    removed = []
    for registration in impl.find_autostart(KIND_TRAY):
        removed.extend(impl.remove_autostart(registration.name))
    for registration in removed:
        logger.info("Removed tray autostart: %s", registration.describe())
    return True


def is_tray_task_installed() -> bool:
    try:
        found = adapter().find_autostart(KIND_TRAY)
    except PlatformUnsupported:
        return False
    return any(not r.legacy for r in found)
