"""Tests for the tray module.

Everything covered here is pure logic: state classification, the poll
loop, menu assembly, PID single-instance, and the tray-startup script
builder. The actual pystray glue (``TrayApp.run()``) isn't covered — it
opens a live system tray which is impractical to assert against.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import pytest

from ragtools.config import Settings
from ragtools.tray import (
    MenuCallbacks,
    MenuItem,
    ProbeResult,
    TrayApp,
    TrayState,
    _tray_pid_path,
    build_menu_items,
    classify_state,
    poll_loop,
)
from ragtools.tray_icons import color_for


def _noop() -> None: ...


def _fake_callbacks() -> MenuCallbacks:
    return MenuCallbacks(
        on_open_admin=_noop,
        on_copy_url=_noop,
        on_restart=_noop,
        on_stop=_noop,
        on_open_logs=_noop,
        on_open_backups=_noop,
        on_quit=_noop,
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        qdrant_path=str(tmp_path / "qdrant"),
        state_db=str(tmp_path / "state.db"),
    )


# ---------------------------------------------------------------------------
# classify_state
# ---------------------------------------------------------------------------


def test_classify_healthy_from_ok_probe():
    s = classify_state(
        ProbeResult(ok=True, collection="markdown_kb"),
        service_pid_alive=True,
        seconds_since_last_healthy=0,
    )
    assert s.kind == "healthy"
    assert "markdown_kb" in s.detail


def test_classify_healthy_detail_without_collection():
    s = classify_state(
        ProbeResult(ok=True, collection=""),
        service_pid_alive=True,
        seconds_since_last_healthy=0,
    )
    assert s.kind == "healthy"
    assert s.detail == "ready"


def test_classify_starting_when_pid_alive_and_never_healthy():
    s = classify_state(
        ProbeResult(ok=False, error="connection refused"),
        service_pid_alive=True,
        seconds_since_last_healthy=None,
    )
    assert s.kind == "starting"


def test_classify_starting_when_pid_alive_recently_healthy():
    """Within the cold-start grace window, transient failures read as starting."""
    s = classify_state(
        ProbeResult(ok=False),
        service_pid_alive=True,
        seconds_since_last_healthy=5.0,
    )
    assert s.kind == "starting"


def test_classify_unreachable_after_grace_period():
    """After the grace window, a live-PID-but-dead-port state is 'hung'."""
    s = classify_state(
        ProbeResult(ok=False),
        service_pid_alive=True,
        seconds_since_last_healthy=120.0,
    )
    assert s.kind == "unreachable"


def test_classify_down_when_no_pid():
    s = classify_state(
        ProbeResult(ok=False, error="connection refused"),
        service_pid_alive=False,
        seconds_since_last_healthy=None,
    )
    assert s.kind == "down"


def test_classify_down_outranks_starting_grace():
    """If the PID is dead, grace-period logic shouldn't invent 'starting'."""
    s = classify_state(
        ProbeResult(ok=False),
        service_pid_alive=False,
        seconds_since_last_healthy=5.0,
    )
    assert s.kind == "down"


# ---------------------------------------------------------------------------
# poll_loop
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@dataclass
class ScriptedProbe:
    """Probe function that returns a scripted sequence of results."""
    results: List[ProbeResult]
    calls: int = 0

    def __call__(self) -> ProbeResult:
        i = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[i]


def _run_loop_n_times(
    probe: ScriptedProbe,
    pid_alive: bool,
    n: int,
    captured: List[TrayState],
) -> None:
    """Drive poll_loop for exactly ``n`` iterations, capturing state changes."""
    stop_event = threading.Event()
    clock = FakeClock()

    def pid_fn() -> bool:
        return pid_alive

    def on_state_change(state: TrayState) -> None:
        captured.append(state)

    original = probe.__call__

    def probe_counter() -> ProbeResult:
        result = original()
        if probe.calls >= n:
            stop_event.set()
        return result

    # Replace with a proxy so we can stop deterministically.
    proxy_probe = type(probe)(results=probe.results)

    def tick_probe() -> ProbeResult:
        r = proxy_probe()
        if proxy_probe.calls >= n:
            stop_event.set()
        return r

    # Use interval=0 so the loop doesn't actually sleep between ticks.
    poll_loop(
        probe_fn=tick_probe,
        pid_fn=pid_fn,
        on_state_change=on_state_change,
        stop_event=stop_event,
        interval=0.0,
        clock=clock,
    )


def test_poll_loop_emits_initial_state():
    captured: List[TrayState] = []
    probe = ScriptedProbe(results=[ProbeResult(ok=True, collection="kb")])
    _run_loop_n_times(probe, pid_alive=True, n=1, captured=captured)
    assert len(captured) == 1
    assert captured[0].kind == "healthy"


def test_poll_loop_deduplicates_same_state():
    """Three identical ok probes → only one state-change callback."""
    captured: List[TrayState] = []
    probe = ScriptedProbe(results=[
        ProbeResult(ok=True, collection="kb"),
        ProbeResult(ok=True, collection="kb"),
        ProbeResult(ok=True, collection="kb"),
    ])
    _run_loop_n_times(probe, pid_alive=True, n=3, captured=captured)
    assert len(captured) == 1


def test_poll_loop_emits_on_transition_healthy_to_down():
    captured: List[TrayState] = []
    probe = ScriptedProbe(results=[
        ProbeResult(ok=True, collection="kb"),
        ProbeResult(ok=False, error="boom"),
    ])

    # Need pid_alive to switch too — use a flipping pid_fn.
    pid_states = [True, False]
    call_counter = {"n": 0}

    def pid_fn() -> bool:
        idx = min(call_counter["n"], len(pid_states) - 1)
        call_counter["n"] += 1
        return pid_states[idx]

    stop = threading.Event()

    def tick_probe() -> ProbeResult:
        r = probe()
        if probe.calls >= 2:
            stop.set()
        return r

    def on_state_change(state: TrayState) -> None:
        captured.append(state)

    poll_loop(
        probe_fn=tick_probe,
        pid_fn=pid_fn,
        on_state_change=on_state_change,
        stop_event=stop,
        interval=0.0,
    )
    kinds = [s.kind for s in captured]
    assert kinds == ["healthy", "down"]


def test_poll_loop_swallows_handler_exceptions():
    """A broken on_state_change must not kill the poll loop."""
    stop = threading.Event()
    probe = ScriptedProbe(results=[
        ProbeResult(ok=True, collection="a"),
        ProbeResult(ok=True, collection="b"),  # different → triggers handler
    ])

    def tick_probe() -> ProbeResult:
        r = probe()
        if probe.calls >= 2:
            stop.set()
        return r

    def bad_handler(_state: TrayState) -> None:
        raise RuntimeError("kaboom")

    poll_loop(
        probe_fn=tick_probe,
        pid_fn=lambda: True,
        on_state_change=bad_handler,
        stop_event=stop,
        interval=0.0,
    )  # must return normally


# ---------------------------------------------------------------------------
# build_menu_items
# ---------------------------------------------------------------------------


def test_menu_has_header_separators_and_quit():
    items = build_menu_items(TrayState("healthy", "ready"), _fake_callbacks())
    labels = [i.label for i in items if not i.separator]
    assert labels[0].startswith("RAGTools")
    assert "Open admin panel" in labels
    assert "Restart service" in labels
    assert "Stop service" in labels
    assert "Quit tray" in labels
    assert any(i.separator for i in items)


def test_menu_disables_open_admin_when_not_healthy():
    items = build_menu_items(TrayState("down", "service is not running"), _fake_callbacks())
    open_admin = next(i for i in items if i.label == "Open admin panel")
    assert open_admin.enabled is False


def test_menu_enables_open_admin_when_healthy():
    items = build_menu_items(TrayState("healthy", "ready"), _fake_callbacks())
    open_admin = next(i for i in items if i.label == "Open admin panel")
    assert open_admin.enabled is True
    # Open admin is the default item (fired on left-click)
    assert open_admin.default is True


def test_menu_disables_stop_when_service_already_down():
    items = build_menu_items(TrayState("down", "x"), _fake_callbacks())
    stop = next(i for i in items if i.label == "Stop service")
    assert stop.enabled is False


def test_menu_restart_is_always_enabled():
    """Restart works in every state — it's a 'ensure running' action."""
    for kind in ("healthy", "starting", "down", "unreachable", "unknown"):
        items = build_menu_items(TrayState(kind, "x"), _fake_callbacks())
        restart = next(i for i in items if i.label == "Restart service")
        assert restart.enabled is True


def test_menu_quit_is_always_enabled():
    for kind in ("healthy", "starting", "down", "unreachable", "unknown"):
        items = build_menu_items(TrayState(kind, "x"), _fake_callbacks())
        quit_item = next(i for i in items if i.label == "Quit tray")
        assert quit_item.enabled is True


def test_menu_header_shows_detail_text():
    items = build_menu_items(TrayState("starting", "starting up…"), _fake_callbacks())
    assert "starting up" in items[0].label
    assert items[0].enabled is False  # header is non-clickable


# ---------------------------------------------------------------------------
# Icon palette — verifies the three-color scheme without rendering
# ---------------------------------------------------------------------------


def test_healthy_and_starting_are_different_colors():
    assert color_for("healthy") != color_for("starting")


def test_down_and_unreachable_share_a_color():
    """Both 'process dead' and 'process hung' are red — same urgency."""
    assert color_for("down") == color_for("unreachable")


def test_unknown_kind_falls_back_to_gray():
    assert color_for("this-is-not-a-real-state") == color_for("unknown")


# ---------------------------------------------------------------------------
# Icon generation — only runs if Pillow is installed
# ---------------------------------------------------------------------------


@pytest.fixture
def pillow_available():
    try:
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        pytest.skip("Pillow not installed; skipping icon-render test")


def test_generate_icon_returns_image_for_each_state(pillow_available):
    from ragtools.tray_icons import generate_icon
    for kind in ("healthy", "starting", "down", "unreachable", "unknown"):
        img = generate_icon(kind)
        assert img.size == (64, 64)
        assert img.mode == "RGBA"


def test_generate_icon_embeds_status_color_in_badge_corner(pillow_available):
    """The bottom-right corner of the rendered icon must contain pixels of
    the state's colour — that's the status badge overlay. Catches regressions
    where the logo completely hides the indicator."""
    from ragtools.tray_icons import color_for, generate_icon

    size = 64
    img = generate_icon("healthy", size)
    # Sample the centre of where the badge should sit: bottom-right quadrant.
    badge_center = (int(size * 0.82), int(size * 0.82))
    r, g, b, a = img.getpixel(badge_center)
    expected = color_for("healthy")
    # Allow a small tolerance for anti-aliased edges we might sample.
    for channel, target in zip((r, g, b), expected):
        assert abs(channel - target) <= 40, (
            f"badge colour at {badge_center} = {(r, g, b)}, expected ~{expected}"
        )


def test_generate_icon_fallback_when_logo_missing(pillow_available, monkeypatch):
    """If the logo file is absent, the tray must still render a plain
    coloured circle — the critical path is availability, not branding."""
    import ragtools.tray_icons as icons

    # Clear the cache and force the loader to return None.
    icons._logo_cache.clear()
    monkeypatch.setattr(icons, "_load_logo", lambda _size: None)

    img = icons.generate_icon("down", 64)
    assert img.size == (64, 64)
    # Centre pixel should be the state colour (solid fill circle).
    r, g, b, a = img.getpixel((32, 32))
    expected = icons.color_for("down")
    for channel, target in zip((r, g, b), expected):
        assert abs(channel - target) <= 40

    icons._logo_cache.clear()


def test_palette_uses_tailwind_500_colors():
    """Pin the exact palette so a drift (e.g. someone tweaking 'healthy'
    toward lime) shows up as a test failure instead of a silent rebrand."""
    from ragtools.tray_icons import color_for
    assert color_for("healthy") == (34, 197, 94)
    assert color_for("starting") == (234, 179, 8)
    assert color_for("down") == (239, 68, 68)
    assert color_for("unreachable") == color_for("down")
    assert color_for("unknown") == (148, 163, 184)


# ---------------------------------------------------------------------------
# TrayApp single-instance — PID file plumbing
# ---------------------------------------------------------------------------


def test_tray_pid_path_is_next_to_service_pid(tmp_path):
    settings = _settings(tmp_path)
    pid_path = _tray_pid_path(settings)
    assert pid_path.name == "tray.pid"
    assert pid_path.parent == Path(settings.qdrant_path).parent


def test_tray_app_acquire_writes_pid_file(tmp_path):
    settings = _settings(tmp_path)
    app = TrayApp(settings)
    try:
        assert app._acquire_single_instance() is True
        pid_path = _tray_pid_path(settings)
        assert pid_path.exists()
        import os
        assert int(pid_path.read_text().strip()) == os.getpid()
    finally:
        app._release_single_instance()


def test_tray_app_acquire_refuses_when_live_tray_exists(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    pid_path = _tray_pid_path(settings)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("12345")

    # Pretend that PID is alive.
    from ragtools.service import process as proc_mod
    monkeypatch.setattr(proc_mod, "_process_alive", lambda pid: pid == 12345)

    app = TrayApp(settings)
    assert app._acquire_single_instance() is False
    # Must not have overwritten the existing PID file.
    assert pid_path.read_text().strip() == "12345"


def test_tray_app_acquire_overwrites_stale_pid(tmp_path, monkeypatch):
    """A PID file pointing at a dead process is stale and should be replaced."""
    settings = _settings(tmp_path)
    pid_path = _tray_pid_path(settings)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("99999")

    from ragtools.service import process as proc_mod
    monkeypatch.setattr(proc_mod, "_process_alive", lambda pid: False)

    app = TrayApp(settings)
    try:
        assert app._acquire_single_instance() is True
        import os
        assert int(pid_path.read_text().strip()) == os.getpid()
    finally:
        app._release_single_instance()


def test_tray_app_release_removes_pid_file(tmp_path):
    settings = _settings(tmp_path)
    app = TrayApp(settings)
    app._acquire_single_instance()
    pid_path = _tray_pid_path(settings)
    assert pid_path.exists()
    app._release_single_instance()
    assert not pid_path.exists()


# ---------------------------------------------------------------------------
# Startup-script builder (tray_startup.py)
# ---------------------------------------------------------------------------


def test_tray_autostart_launches_no_console_shim(tmp_path):
    """The tray must not flash a console at login.

    The old design achieved that with a `.vbs` whose `shell.Run(..., 0, False)`
    hid the window. The window style is a process-creation concern, so the shim
    is gone and the adapter registers the command directly — but the invariant
    it protected is unchanged and now asserted on the registration.
    """
    from ragtools.platform import KIND_TRAY
    from ragtools.service import tray_startup

    argv = tray_startup._tray_argv()
    assert "tray" in argv, "the registration must actually launch the tray"
    assert not any(str(a).lower().endswith(".vbs") for a in argv)
    assert not any("wscript" in str(a).lower() for a in argv)


def test_tray_autostart_waits_for_the_shell_before_launching(tmp_path):
    """Without a delay the tray races explorer's systray initialisation and
    `Shell_NotifyIcon` fails silently — the icon simply never appears. Too long
    and its absence reads as broken. The old VBS slept; the registration now
    carries the delay, and the policy window is the same.
    """
    from ragtools.platform import KIND_TRAY
    from ragtools.service import tray_startup

    captured = {}

    class _Adapter:
        name = "test"

        def has_desktop_session(self):
            return True

        def install_autostart(self, spec):
            captured["spec"] = spec
            from ragtools.platform import Registration

            return Registration(spec.name, spec.kind, "test", " ".join(spec.argv))

    # Patch the name `tray_startup` resolves — it imported `adapter` directly,
    # so patching the package attribute would leave the real adapter in play
    # and this test would register a scheduled task on the developer's machine.
    saved = tray_startup.adapter
    tray_startup.adapter = lambda: _Adapter()
    try:
        assert tray_startup.install_tray_task(_settings(tmp_path)) is True
    finally:
        tray_startup.adapter = saved

    spec = captured["spec"]
    assert spec.kind == KIND_TRAY
    assert 10 <= spec.delay_seconds <= 60


def test_tray_autostart_is_refused_without_a_desktop_session(tmp_path):
    """Headless installs are fully supported WITHOUT a tray. Registering one
    there would fail at every login instead of simply not existing."""
    from ragtools.service import tray_startup

    class _Headless:
        name = "test"

        def has_desktop_session(self):
            return False

        def install_autostart(self, spec):      # pragma: no cover - must not run
            raise AssertionError("registered a tray on a headless machine")

    saved = tray_startup.adapter
    tray_startup.adapter = lambda: _Headless()
    try:
        assert tray_startup.install_tray_task(_settings(tmp_path)) is False
    finally:
        tray_startup.adapter = saved


# ---------------------------------------------------------------------------
# Tray file-logging — invisible runtime needs a sink
# ---------------------------------------------------------------------------


def test_configure_tray_logging_creates_log_file(tmp_path):
    """When the tray is launched silently from the autostart VBS, stdout/
    stderr go nowhere. ``_configure_tray_logging`` must attach a rotating
    file handler so any failure reaches disk."""
    import logging
    from ragtools.tray import _configure_tray_logging

    settings = _settings(tmp_path)
    _configure_tray_logging(settings)

    tray_logger = logging.getLogger("ragtools.tray")
    tray_logger.info("hello from the test")
    # Force flush — RotatingFileHandler writes synchronously per record but
    # closing+reopening guarantees the bytes are on disk for the assertion.
    for h in tray_logger.handlers:
        try:
            h.flush()
        except Exception:
            pass

    log_path = tmp_path / "logs" / "tray.log"
    assert log_path.exists(), f"tray.log should exist at {log_path}"
    body = log_path.read_text(encoding="utf-8")
    assert "hello from the test" in body
    assert "ragtools.tray" in body

    # Cleanup so other tests aren't polluted by our handler.
    for h in list(tray_logger.handlers):
        if getattr(h, "_ragtools_tray_handler", False):
            tray_logger.removeHandler(h)
            h.close()


def test_configure_tray_logging_is_idempotent(tmp_path):
    """Calling twice in the same process must not stack handlers — otherwise
    every log line gets written N times after a tray re-init."""
    import logging
    from ragtools.tray import _configure_tray_logging

    settings = _settings(tmp_path)
    _configure_tray_logging(settings)
    _configure_tray_logging(settings)
    _configure_tray_logging(settings)

    tray_logger = logging.getLogger("ragtools.tray")
    flagged = [
        h for h in tray_logger.handlers
        if getattr(h, "_ragtools_tray_handler", False)
    ]
    assert len(flagged) == 1

    # Cleanup
    for h in flagged:
        tray_logger.removeHandler(h)
        h.close()


def test_configure_tray_logging_swallows_unwritable_dir(tmp_path, monkeypatch):
    """If the data dir is unwritable for any reason, the tray must still
    boot — logging setup must never crash the tray."""
    import logging
    from ragtools.tray import _configure_tray_logging

    # Make `mkdir` raise to simulate a permissions failure.
    from pathlib import Path as _P
    real_mkdir = _P.mkdir

    def boom(self, *args, **kwargs):
        if "logs" in str(self):
            raise PermissionError("simulated")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(_P, "mkdir", boom)

    settings = _settings(tmp_path)
    _configure_tray_logging(settings)  # must not raise

    # No flagged handler should have been attached.
    tray_logger = logging.getLogger("ragtools.tray")
    flagged = [
        h for h in tray_logger.handlers
        if getattr(h, "_ragtools_tray_handler", False)
    ]
    assert flagged == []


# ---------------------------------------------------------------------------
# Linux clipboard fallback chain (v2.5.1 — was the xclip hardcode)
# ---------------------------------------------------------------------------


def test_linux_clipboard_uses_first_available_tool(tmp_path, monkeypatch):
    """wl-copy → xclip → xsel, skipping whatever is not installed, and never
    raising when none of them are."""
    import subprocess

    from ragtools.platform.linux import LinuxAdapter

    monkeypatch.setattr("shutil.which",
                        lambda tool: "/usr/bin/xclip" if tool == "xclip" else None)
    calls = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv) or _Done())

    assert LinuxAdapter(home=tmp_path).copy_text("http://x") is True
    assert calls and calls[0][0] == "xclip"


def test_linux_clipboard_prefers_wl_copy_on_wayland(monkeypatch, tmp_path):
    """xclip may exist on a Wayland session and silently do nothing, so wl-copy
    must be tried first. The chain moved behind the platform adapter; the
    invariant did not."""
    import subprocess

    from ragtools.platform.linux import LinuxAdapter

    monkeypatch.setattr(
        "shutil.which",
        lambda tool: "/usr/bin/wl-copy" if tool == "wl-copy" else "/usr/bin/xclip",
    )
    calls = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv) or _Done())

    assert LinuxAdapter(home=tmp_path).copy_text("http://x") is True
    assert len(calls) == 1
    assert calls[0][0] == "wl-copy"


def test_linux_clipboard_reports_when_no_tool_exists(monkeypatch, tmp_path):
    """A minimal or headless box has no clipboard tool at all. The adapter must
    say no rather than pretend, and launch no process doing it."""
    import subprocess

    from ragtools.platform.linux import LinuxAdapter

    monkeypatch.setattr("shutil.which", lambda _: None)
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ran.append(a))

    assert LinuxAdapter(home=tmp_path).copy_text("http://x") is False
    assert ran == []


def test_tray_warns_when_the_clipboard_is_unavailable(tmp_path, caplog):
    """The user-visible half: "nothing happened" versus "install wl-copy"."""
    import logging

    from ragtools import tray as tray_mod

    app = TrayApp(_settings(tmp_path))

    class _NoClipboard:
        def copy_text(self, text):
            return False

    import ragtools.platform as platform_mod

    saved = platform_mod.adapter
    platform_mod.adapter = lambda: _NoClipboard()
    try:
        with caplog.at_level(logging.WARNING):
            app._on_copy_url()
    finally:
        platform_mod.adapter = saved

    assert any("no clipboard tool" in r.message.lower() for r in caplog.records)


def test_get_app_dir_honours_xdg_on_linux(monkeypatch, tmp_path):
    """Linux installed-mode root follows XDG_DATA_HOME with a ~/.local/share
    fallback. Asserted at the adapter now that config delegates to it."""
    from pathlib import Path as _P

    from ragtools.platform.linux import LinuxAdapter

    monkeypatch.setenv("XDG_DATA_HOME", "/var/lib/demo/data")
    assert LinuxAdapter(home=tmp_path).app_dir() == _P("/var/lib/demo/data") / "RAGTools"

    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert LinuxAdapter(home=tmp_path).app_dir() == tmp_path / ".local" / "share" / "RAGTools"


def test_get_app_dir_none_for_unknown_platform(monkeypatch):
    """Anything that is not windows/linux/darwin still yields None so callers
    fall back to the dev-mode CWD layout instead of inventing a path."""
    import ragtools.config as cfg
    import ragtools.platform as platform_mod
    from ragtools.platform import PlatformUnsupported

    def _refuse():
        raise PlatformUnsupported("freebsd13")

    monkeypatch.setattr(platform_mod, "adapter", _refuse)
    assert cfg._get_app_dir() is None
