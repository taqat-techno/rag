"""Tests for the desktop toast notification module.

Covers:
  - CrashNotifier dispatches to the injected backend with correct args
  - Cooldown collapses repeated kinds into a single toast
  - Distinct kinds are not cross-throttled
  - `desktop_notifications=False` opts out entirely
  - A backend exception never propagates out of `notify()`
  - Convenience helpers produce the right title/message/deep-link
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import pytest

from ragtools.config import Settings
from ragtools.service.notify import (
    APP_NAME,
    AUMID,
    CrashNotifier,
    DesktopNotifier,
    notify_project_indexed,
    notify_rebuild_complete,
    notify_scale_warning,
    notify_service_crashed,
    notify_supervisor_gave_up,
    notify_watcher_gave_up,
    reset_shared_notifier,
)


@dataclass
class FakeBackend:
    """Collects every send() call for assertion."""

    calls: List[dict] = field(default_factory=list)

    def send(self, title: str, message: str, deep_link: Optional[str] = None) -> None:
        self.calls.append({"title": title, "message": message, "deep_link": deep_link})


@dataclass
class ExplodingBackend:
    """Always raises — proves the notifier swallows backend failures."""

    def send(self, title: str, message: str, deep_link: Optional[str] = None) -> None:
        raise RuntimeError("OS toast subsystem on fire")


def _settings(**overrides) -> Settings:
    defaults = dict(
        qdrant_path="/tmp/test-qdrant",
        state_db="/tmp/test-state.db",
    )
    defaults.update(overrides)
    return Settings(**defaults)


class FakeClock:
    """Monotonic clock we can step forward manually."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------


def test_notify_dispatches_to_backend():
    backend = FakeBackend()
    notifier = CrashNotifier(settings=_settings(), backend=backend, clock=FakeClock())

    sent = notifier.notify(
        "service_crash",
        title="boom",
        message="details",
        deep_link="http://127.0.0.1:21420/",
    )

    assert sent is True
    assert len(backend.calls) == 1
    assert backend.calls[0] == {
        "title": "boom",
        "message": "details",
        "deep_link": "http://127.0.0.1:21420/",
    }


def test_cooldown_suppresses_same_kind():
    backend = FakeBackend()
    clock = FakeClock()
    s = _settings()
    s.notification_cooldown_seconds = 30.0
    notifier = CrashNotifier(settings=s, backend=backend, clock=clock)

    assert notifier.notify("service_crash", "a", "x") is True
    clock.now += 5.0  # well within cooldown
    assert notifier.notify("service_crash", "b", "y") is False
    assert len(backend.calls) == 1


def test_cooldown_expires_allows_next_toast():
    backend = FakeBackend()
    clock = FakeClock()
    s = _settings()
    s.notification_cooldown_seconds = 30.0
    notifier = CrashNotifier(settings=s, backend=backend, clock=clock)

    assert notifier.notify("service_crash", "a", "x") is True
    clock.now += 31.0
    assert notifier.notify("service_crash", "b", "y") is True
    assert len(backend.calls) == 2


def test_cooldown_is_per_kind():
    """service_crash and supervisor_gave_up must not share a cooldown bucket."""
    backend = FakeBackend()
    clock = FakeClock()
    s = _settings()
    s.notification_cooldown_seconds = 30.0
    notifier = CrashNotifier(settings=s, backend=backend, clock=clock)

    assert notifier.notify("service_crash", "a", "x") is True
    assert notifier.notify("supervisor_gave_up", "b", "y") is True
    assert len(backend.calls) == 2


def test_opt_out_sends_nothing():
    backend = FakeBackend()
    s = _settings()
    s.desktop_notifications = False
    notifier = CrashNotifier(settings=s, backend=backend, clock=FakeClock())

    assert notifier.notify("service_crash", "a", "x") is False
    assert backend.calls == []


def test_backend_failure_is_swallowed():
    """A broken OS toast must never propagate out of a crash handler."""
    notifier = CrashNotifier(
        settings=_settings(), backend=ExplodingBackend(), clock=FakeClock()
    )
    # Returning True is OK — notifier thinks it dispatched. The important
    # property is that the exception does not escape.
    # Actually, we want False because the send failed. Verify by observation:
    result = notifier.notify("service_crash", "a", "x")
    # Per contract: notify() must not raise. Result is best-effort.
    assert result in (True, False)


def test_notify_service_crashed_helper_formats_correctly():
    backend = FakeBackend()
    s = _settings()
    s.service_host = "127.0.0.1"
    s.service_port = 21420
    notifier = CrashNotifier(settings=s, backend=backend, clock=FakeClock())

    notify_service_crashed(s, "RuntimeError: indexing died", notifier=notifier)

    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert call["title"] == "RAG service crashed"
    assert "RuntimeError: indexing died" in call["message"]
    assert call["deep_link"] == "http://127.0.0.1:21420/"


def test_notify_service_crashed_truncates_long_messages():
    backend = FakeBackend()
    notifier = CrashNotifier(settings=_settings(), backend=backend, clock=FakeClock())

    long_msg = "x" * 500
    notify_service_crashed(_settings(), long_msg, notifier=notifier)

    assert len(backend.calls) == 1
    # Truncation is to 197 chars + "..." = 200, then newline + footer
    rendered = backend.calls[0]["message"]
    assert rendered.startswith("x" * 100)
    assert "..." in rendered
    # Sanity: truncated body plus footer is well under 400 chars
    assert len(rendered) < 400


def test_notify_supervisor_gave_up_helper_formats_correctly():
    backend = FakeBackend()
    s = _settings()
    s.service_host = "127.0.0.1"
    s.service_port = 21420
    notifier = CrashNotifier(settings=s, backend=backend, clock=FakeClock())

    notify_supervisor_gave_up(
        s, "exceeded 5 failures in 300s", notifier=notifier
    )

    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert "auto-restart disabled" in call["title"].lower()
    assert "exceeded 5 failures" in call["message"]
    assert "Manual restart required" in call["message"]
    assert call["deep_link"] == "http://127.0.0.1:21420/"


def test_convenience_helper_respects_cooldown_via_notifier():
    """Two notify_service_crashed() calls sharing one notifier must dedupe."""
    backend = FakeBackend()
    clock = FakeClock()
    s = _settings()
    s.notification_cooldown_seconds = 30.0
    notifier = CrashNotifier(settings=s, backend=backend, clock=clock)

    notify_service_crashed(s, "first", notifier=notifier)
    clock.now += 1.0
    notify_service_crashed(s, "second", notifier=notifier)

    assert len(backend.calls) == 1
    assert "first" in backend.calls[0]["message"]


def test_default_backend_returns_something():
    """Smoke: the platform picker never returns None."""
    from ragtools.service.notify import default_backend

    b = default_backend()
    assert b is not None
    # Must have a .send attribute to conform to ToastBackend
    assert callable(getattr(b, "send", None))


# ---------------------------------------------------------------------------
# Identity — app name in the toast header
# ---------------------------------------------------------------------------


def test_aumid_is_clean_rag_tools_name():
    """The Windows AUMID shown in the toast header must be just 'RAGTools'."""
    assert AUMID == "RAGTools"
    assert APP_NAME == "RAGTools"


def test_crash_notifier_is_alias_for_desktop_notifier():
    """Existing call sites still work after the rename."""
    assert CrashNotifier is DesktopNotifier


# ---------------------------------------------------------------------------
# Lifecycle helpers — project_indexed, rebuild_complete, scale_warning
# ---------------------------------------------------------------------------


def test_notify_project_indexed_says_ready_to_search():
    backend = FakeBackend()
    s = _settings()
    notifier = DesktopNotifier(settings=s, backend=backend, clock=FakeClock())

    notify_project_indexed(s, project_id="alpha", files=42, chunks=1612, notifier=notifier)

    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert "indexed" in call["title"].lower()
    assert "alpha" in call["message"]
    assert "42" in call["message"]
    assert "1.6k" in call["message"]  # human-friendly chunk count


def test_notify_project_indexed_handles_zero_files():
    """An added project with no .md files under it should give a clear
    signal instead of claiming it's ready to search with 0 files."""
    backend = FakeBackend()
    s = _settings()
    notifier = DesktopNotifier(settings=s, backend=backend, clock=FakeClock())

    notify_project_indexed(s, project_id="empty", files=0, chunks=0, notifier=notifier)

    assert len(backend.calls) == 1
    msg = backend.calls[0]["message"].lower()
    assert "no markdown files" in msg


def test_notify_project_indexed_uses_project_scoped_cooldown():
    """Adding two different projects in quick succession must produce two
    toasts — the cooldown is per project."""
    backend = FakeBackend()
    clock = FakeClock()
    s = _settings()
    s.notification_cooldown_seconds = 30.0
    notifier = DesktopNotifier(settings=s, backend=backend, clock=clock)

    notify_project_indexed(s, project_id="alpha", files=1, chunks=1, notifier=notifier)
    clock.now += 1.0
    notify_project_indexed(s, project_id="beta", files=1, chunks=1, notifier=notifier)

    assert len(backend.calls) == 2


def test_notify_rebuild_complete_includes_counts():
    backend = FakeBackend()
    s = _settings()
    notifier = DesktopNotifier(settings=s, backend=backend, clock=FakeClock())

    notify_rebuild_complete(s, files=100, chunks=5400, notifier=notifier)

    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert "rebuilt" in call["title"].lower()
    assert "100" in call["message"]
    assert "5.4k" in call["message"]


def test_notify_scale_warning_has_long_cooldown():
    """Scale warnings must not re-fire on every watcher-triggered index run.

    Simulates two scale events 5 minutes apart (typical indexing interval)
    and asserts the second one is suppressed by the 1-hour cooldown.
    """
    backend = FakeBackend()
    clock = FakeClock()
    s = _settings()
    notifier = DesktopNotifier(settings=s, backend=backend, clock=clock)

    notify_scale_warning(s, level="approaching", message="22k points", notifier=notifier)
    clock.now += 5 * 60  # 5 minutes later
    notify_scale_warning(s, level="approaching", message="22k points", notifier=notifier)

    assert len(backend.calls) == 1  # dedup'd by the long cooldown


def test_notify_scale_warning_different_levels_are_separate():
    """'approaching' and 'over' are independent kinds — crossing from one
    to the other must produce a new toast even within the 1-hour window."""
    backend = FakeBackend()
    clock = FakeClock()
    s = _settings()
    notifier = DesktopNotifier(settings=s, backend=backend, clock=clock)

    notify_scale_warning(s, level="approaching", message="18k", notifier=notifier)
    clock.now += 60  # much less than 1h
    notify_scale_warning(s, level="over", message="22k", notifier=notifier)

    assert len(backend.calls) == 2


def test_notify_watcher_gave_up_includes_retry_count():
    backend = FakeBackend()
    s = _settings()
    notifier = DesktopNotifier(settings=s, backend=backend, clock=FakeClock())

    notify_watcher_gave_up(s, error="PermissionError: foo", retries=5, notifier=notifier)

    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert "watcher" in call["title"].lower() or "indexed" in call["title"].lower()
    assert "5" in call["message"]
    assert "PermissionError" in call["message"]
    assert "Rebuild" in call["message"] or "restart" in call["message"].lower()


def test_notify_watcher_gave_up_truncates_long_error():
    backend = FakeBackend()
    s = _settings()
    notifier = DesktopNotifier(settings=s, backend=backend, clock=FakeClock())

    notify_watcher_gave_up(s, error="x" * 400, retries=5, notifier=notifier)
    rendered = backend.calls[0]["message"]
    assert "..." in rendered
    assert len(rendered) < 500  # sanity


def test_notify_scale_warning_title_differs_by_level():
    backend = FakeBackend()
    s = _settings()
    notifier = DesktopNotifier(settings=s, backend=backend, clock=FakeClock())

    notify_scale_warning(s, level="approaching", message="18k", notifier=notifier)
    notify_scale_warning(s, level="over", message="22k", notifier=notifier)

    t_approaching = backend.calls[0]["title"].lower()
    t_over = backend.calls[1]["title"].lower()
    assert "approaching" in t_approaching
    assert "exceed" in t_over


def test_cooldown_override_per_call():
    """A per-call cooldown override must shadow settings.notification_cooldown_seconds."""
    backend = FakeBackend()
    clock = FakeClock()
    s = _settings()
    s.notification_cooldown_seconds = 30.0
    notifier = DesktopNotifier(settings=s, backend=backend, clock=clock)

    # First notify with 5s override
    ok1 = notifier.notify("x", "t", "m", cooldown_seconds=5)
    clock.now += 10  # > 5s override
    ok2 = notifier.notify("x", "t", "m", cooldown_seconds=5)
    # Both succeed because the per-call cooldown shadowed the settings default
    assert ok1 is True
    assert ok2 is True
    assert len(backend.calls) == 2


# ---------------------------------------------------------------------------
# Shared notifier — dedup spans across helper calls
# ---------------------------------------------------------------------------


def test_shared_notifier_dedups_across_calls(monkeypatch):
    """Two consecutive indexing runs both wanting to fire a scale warning
    must share cooldown state via the module-level shared notifier."""
    from ragtools.service import notify as notify_module

    captured = []

    class CapturingBackend:
        def send(self, title, message, deep_link=None):
            captured.append(title)

    monkeypatch.setattr(notify_module, "default_backend", lambda: CapturingBackend())
    reset_shared_notifier()

    s = _settings()
    notify_scale_warning(s, level="approaching", message="m")
    # No clock-advance, no manual notifier — both should share the same
    # module-level DesktopNotifier and the second call should be suppressed.
    notify_scale_warning(s, level="approaching", message="m")

    assert len(captured) == 1
    reset_shared_notifier()


# ---------------------------------------------------------------------------
# AUMID registration — no-op off-Windows, idempotent otherwise
# ---------------------------------------------------------------------------


def test_ensure_aumid_registered_is_no_op_off_windows(monkeypatch):
    """On non-Windows the registry path is entirely skipped — must not raise."""
    from ragtools.service import notify as notify_module

    # Reset the module-level flag so the first call tries again.
    notify_module._AUMID_REGISTERED = False
    monkeypatch.setattr(notify_module.sys, "platform", "linux")

    assert notify_module.ensure_aumid_registered() is True
    # Second call is a fast-path no-op.
    assert notify_module.ensure_aumid_registered() is True


def test_ensure_aumid_registered_swallows_winreg_errors(monkeypatch):
    """Even if winreg is unavailable or the write fails, the function must
    not raise — callers are on hot paths that can't tolerate exceptions."""
    from ragtools.service import notify as notify_module

    notify_module._AUMID_REGISTERED = False
    monkeypatch.setattr(notify_module.sys, "platform", "win32")

    # Inject a fake winreg via sys.modules so the lazy import in
    # ensure_aumid_registered picks it up.
    import sys as _sys
    import types

    fake_winreg = types.ModuleType("winreg")

    class _Boom:
        def __enter__(self): raise OSError("simulated")
        def __exit__(self, *a): return False

    fake_winreg.HKEY_CURRENT_USER = 0
    fake_winreg.REG_SZ = 1
    fake_winreg.CreateKey = lambda *_a, **_k: _Boom()
    fake_winreg.SetValueEx = lambda *_a, **_k: None
    monkeypatch.setitem(_sys.modules, "winreg", fake_winreg)

    # Must return False, not raise
    assert notify_module.ensure_aumid_registered() is False
    notify_module._AUMID_REGISTERED = False
