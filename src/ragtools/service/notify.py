"""Desktop toast notifications for RAG Tools.

Surfaces important events (crashes, long-running-operation completions,
scale warnings) to the user without requiring the admin panel to be open.

Design
------
Pure control flow (dedup, opt-out, platform select) lives in
`DesktopNotifier`. The OS-specific delivery is an injectable backend, so
tests never fire real toasts and the module is import-safe on systems
without the toast library.

Platform support
----------------
Windows 10+ : `winotify` (optional dep, `pip install ragtools[notifications]`)
macOS       : `osascript display notification` (built-in, no click handler)
Linux/other : log-only fallback

Windows AUMID
-------------
To get a *clean* notification header ("RAGTools" + app logo, instead of
the verbose default "Python.exe") we register an AUMID in
`HKCU\\Software\\Classes\\AppUserModelId\\RAGTools` once, on first
dispatch. The registration is idempotent — writing the same values on a
subsequent run is a no-op.

Cooldown
--------
Each kind has its own per-kind timestamp. The default cooldown is
`settings.notification_cooldown_seconds` (30s). Helpers that are known to
fire repeatedly (scale warnings during re-indexing) pass a longer
`cooldown_seconds` override so the action center doesn't fill up.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional, Protocol

from ragtools.config import Settings

logger = logging.getLogger("ragtools.notify")


# --- Identity ---

AUMID = "RAGTools"
APP_NAME = "RAGTools"


def _resolve_logo_path() -> Optional[str]:
    """Absolute path to the app logo PNG used for the AUMID IconUri.

    In dev mode this is `src/ragtools/service/static/logo.png`. In a
    PyInstaller bundle the static folder is unpacked alongside the module,
    so the same relative walk works.
    """
    try:
        candidate = Path(__file__).parent / "static" / "logo.png"
        if candidate.is_file():
            return str(candidate.resolve())
    except Exception as e:
        logger.debug("Logo path resolution failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Windows AUMID registration — makes toasts show "RAGTools" + logo in the
# header, instead of the verbose "Python.exe" / "TaqaTechno.RAGTools" that
# Windows infers from sys.executable.
# ---------------------------------------------------------------------------


_AUMID_REGISTERED = False


def ensure_aumid_registered() -> bool:
    """Register the AUMID once per process. Returns True on success or
    when it was already registered. False on any error (never raises)."""
    global _AUMID_REGISTERED
    if _AUMID_REGISTERED:
        return True
    if sys.platform != "win32":
        _AUMID_REGISTERED = True  # avoid re-entry on non-Windows
        return True

    try:
        import winreg  # type: ignore[import-not-found]

        key_path = fr"Software\Classes\AppUserModelId\{AUMID}"
        logo = _resolve_logo_path()
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            if logo:
                winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, logo)
        _AUMID_REGISTERED = True
        logger.info("Registered AUMID %s (icon=%s)", AUMID, bool(logo))
        return True
    except Exception as e:
        logger.warning("AUMID registration failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Backend protocol — testable seam
# ---------------------------------------------------------------------------


class ToastBackend(Protocol):
    """Abstract toast renderer. Impls write to the OS; tests collect calls."""

    def send(
        self,
        title: str,
        message: str,
        deep_link: Optional[str] = None,
    ) -> None: ...


class _LoggingBackend:
    """Fallback backend that only writes to the log. Used when no OS toast
    mechanism is available, or when the notifications extra is not installed.
    """

    def send(self, title: str, message: str, deep_link: Optional[str] = None) -> None:
        logger.info("[toast-fallback] %s — %s (link=%s)", title, message, deep_link)


class _WindowsBackend:
    """winotify-based backend for Windows 10+.

    Does NOT pass an `icon=` to Notification — the app logo comes from the
    AUMID registration (ensure_aumid_registered) which lands in the toast
    header the same way WhatsApp's badge does. This keeps the toast body
    clean and predictable across platforms.
    """

    def send(self, title: str, message: str, deep_link: Optional[str] = None) -> None:
        try:
            from winotify import Notification, audio  # type: ignore[import-not-found]
        except Exception as e:
            logger.info("winotify not available, falling back to log-only: %s", e)
            _LoggingBackend().send(title, message, deep_link)
            return

        # Register the AUMID so the header shows "RAGTools" + our icon.
        ensure_aumid_registered()

        try:
            toast = Notification(
                app_id=AUMID,
                title=title,
                msg=message,
                duration="long",
            )
            toast.set_audio(audio.Default, loop=False)
            if deep_link:
                toast.add_actions(label="Open admin panel", launch=deep_link)
            toast.show()
        except Exception as e:
            logger.warning("Windows toast failed: %s", e)


class _MacBackend:
    """osascript-based backend for macOS. No click action (osascript can't do it)."""

    def send(self, title: str, message: str, deep_link: Optional[str] = None) -> None:
        osa = shutil.which("osascript")
        if not osa:
            _LoggingBackend().send(title, message, deep_link)
            return
        safe_title = title.replace('"', '\\"')
        safe_msg = message.replace('"', '\\"')
        script = f'display notification "{safe_msg}" with title "{safe_title}"'
        try:
            subprocess.run(
                [osa, "-e", script],
                check=False,
                timeout=5,
                capture_output=True,
            )
        except Exception as e:
            logger.warning("macOS notification failed: %s", e)


def default_backend() -> ToastBackend:
    """Pick the backend appropriate for the current platform."""
    if sys.platform == "win32":
        return _WindowsBackend()
    if sys.platform == "darwin":
        return _MacBackend()
    return _LoggingBackend()


# ---------------------------------------------------------------------------
# Notifier — opt-out + cooldown logic, no I/O of its own
# ---------------------------------------------------------------------------


@dataclass
class DesktopNotifier:
    """Send toasts with per-kind cooldown and config-driven opt-out.

    ``backend`` defaults to ``None`` and is resolved lazily in
    ``__post_init__`` via the module-level ``default_backend`` symbol. This
    lets tests monkeypatch ``notify_module.default_backend`` and have it
    take effect for instances created *after* the patch.
    """

    settings: Settings
    backend: Optional[ToastBackend] = None
    clock: Callable[[], float] = field(default=time.monotonic)
    _last_sent: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.backend is None:
            self.backend = default_backend()

    def notify(
        self,
        kind: str,
        title: str,
        message: str,
        deep_link: Optional[str] = None,
        cooldown_seconds: Optional[float] = None,
    ) -> bool:
        """Send a toast for the given `kind` unless opted out or throttled.

        Args:
            kind: stable identifier used for cooldown bucketing.
            title: short bold line shown first.
            message: body text.
            deep_link: optional URL wired to the toast's "Open admin panel" action.
            cooldown_seconds: override for this call; defaults to
                ``settings.notification_cooldown_seconds``. Non-urgent
                lifecycle events pass a longer value (e.g. 1 h for scale
                warnings) so re-indexing cycles don't spam the action center.

        Returns True if a toast was dispatched, False otherwise. Never raises.
        """
        try:
            if not self.settings.desktop_notifications:
                logger.debug("Notifications disabled, skipping toast for %s", kind)
                return False

            now = self.clock()
            cooldown = (
                cooldown_seconds
                if cooldown_seconds is not None
                else self.settings.notification_cooldown_seconds
            )
            last = self._last_sent.get(kind)
            if last is not None and (now - last) < cooldown:
                logger.debug(
                    "Toast for %s suppressed (cooldown: %.1fs remaining)",
                    kind, cooldown - (now - last),
                )
                return False

            self.backend.send(title, message, deep_link=deep_link)
            self._last_sent[kind] = now
            return True
        except Exception as e:
            logger.warning("DesktopNotifier.notify failed (swallowed): %s", e)
            return False


# Backwards-compatible alias (tests, older imports).
CrashNotifier = DesktopNotifier


# ---------------------------------------------------------------------------
# Shared process-wide notifier — so cooldowns span successive fires from
# different call sites (e.g. two consecutive indexing runs both wanting to
# raise a scale warning share the same dedup state).
# ---------------------------------------------------------------------------


_shared: Optional[DesktopNotifier] = None


def get_shared_notifier(settings: Settings) -> DesktopNotifier:
    """Return a process-wide DesktopNotifier. Created lazily."""
    global _shared
    if _shared is None or _shared.settings is not settings:
        _shared = DesktopNotifier(settings=settings)
    return _shared


def reset_shared_notifier() -> None:
    """Drop the cached notifier. For tests."""
    global _shared
    _shared = None


# ---------------------------------------------------------------------------
# Convenience helpers — one-liner wrappers per lifecycle event
# ---------------------------------------------------------------------------


def _admin_url(settings: Settings) -> str:
    return f"http://{settings.service_host}:{settings.service_port}/"


def _format_count(n: int) -> str:
    """Human-friendly count for toast bodies — "1.6k" instead of "1,612"."""
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n/1000:.1f}k".replace(".0k", "k")
    return f"{n//1000}k"


def notify_service_crashed(
    settings: Settings,
    exception_message: str,
    notifier: Optional[DesktopNotifier] = None,
) -> bool:
    """Called right after a fatal service exit was recorded."""
    n = notifier or get_shared_notifier(settings)
    truncated = exception_message if len(exception_message) <= 200 else exception_message[:197] + "..."
    return n.notify(
        kind="service_crash",
        title="RAG service crashed",
        message=f"{truncated}\nClick to open the admin panel for details.",
        deep_link=_admin_url(settings),
    )


def notify_supervisor_gave_up(
    settings: Settings,
    reason: str,
    notifier: Optional[DesktopNotifier] = None,
) -> bool:
    """Called when the supervisor exhausted its restart budget."""
    n = notifier or get_shared_notifier(settings)
    return n.notify(
        kind="supervisor_gave_up",
        title="RAG service stopped — auto-restart disabled",
        message=f"{reason}\nManual restart required.",
        deep_link=_admin_url(settings),
    )


def notify_project_indexed(
    settings: Settings,
    project_id: str,
    files: int,
    chunks: int,
    notifier: Optional[DesktopNotifier] = None,
) -> bool:
    """Called when a newly-added project's initial auto-index completes.

    Each project gets its own cooldown bucket so adding two folders in a
    row produces two toasts, not one.
    """
    n = notifier or get_shared_notifier(settings)
    if files == 0:
        msg = f"Project '{project_id}' added, but no Markdown files were found."
    else:
        msg = f"Project '{project_id}' is ready to search — {_format_count(files)} files, {_format_count(chunks)} chunks."
    return n.notify(
        kind=f"project_indexed:{project_id}",
        title="Project indexed",
        message=msg,
        deep_link=_admin_url(settings),
    )


def notify_rebuild_complete(
    settings: Settings,
    files: int,
    chunks: int,
    notifier: Optional[DesktopNotifier] = None,
) -> bool:
    """Called when `Rebuild Knowledge Base` finishes."""
    n = notifier or get_shared_notifier(settings)
    return n.notify(
        kind="rebuild_complete",
        title="Knowledge base rebuilt",
        message=f"Full re-index complete — {_format_count(files)} files, {_format_count(chunks)} chunks.",
        deep_link=_admin_url(settings),
    )


def notify_watcher_gave_up(
    settings: Settings,
    error: str,
    retries: int,
    notifier: Optional[DesktopNotifier] = None,
) -> bool:
    """Called when the file watcher exhausts its restart budget.

    Same severity as ``notify_supervisor_gave_up`` — user needs to know
    their `.md` changes are no longer being auto-indexed, otherwise they
    find out hours later when search results are stale.
    """
    n = notifier or get_shared_notifier(settings)
    truncated = error if len(error) <= 180 else error[:177] + "..."
    return n.notify(
        kind="watcher_gave_up",
        title="File watcher stopped — changes are no longer being indexed",
        message=(
            f"After {retries} retries: {truncated}\n"
            "Use Rebuild or restart the service to recover."
        ),
        deep_link=_admin_url(settings),
    )


def notify_scale_warning(
    settings: Settings,
    level: str,
    message: str,
    notifier: Optional[DesktopNotifier] = None,
) -> bool:
    """Called once when the collection crosses the 20k soft limit.

    The 1-hour cooldown prevents the watcher's periodic re-indexing from
    re-firing the same warning every few minutes.
    """
    n = notifier or get_shared_notifier(settings)
    title = (
        "Knowledge base is approaching Qdrant's local limit"
        if level == "approaching" else
        "Knowledge base exceeds Qdrant's local limit"
    )
    return n.notify(
        kind=f"scale_warning:{level}",
        title=title,
        message=message,
        deep_link=_admin_url(settings),
        cooldown_seconds=3600.0,  # 1h — longer than any single indexing run
    )
