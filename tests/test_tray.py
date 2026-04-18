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


def test_tray_startup_script_hides_window(tmp_path):
    """shell.Run with ``0`` as the second arg means 'hidden window'.
    This prevents a console flash at login."""
    from ragtools.service.tray_startup import _build_tray_script
    settings = _settings(tmp_path)
    script = _build_tray_script(settings)
    # The second arg of shell.Run is the window style — 0 is hidden.
    assert "shell.Run" in script
    assert ", 0, False" in script  # hidden + don't wait


def test_tray_startup_script_includes_tray_command(tmp_path):
    from ragtools.service.tray_startup import _build_tray_script
    settings = _settings(tmp_path)
    script = _build_tray_script(settings)
    assert "tray" in script  # invokes ``rag tray``


# ---------------------------------------------------------------------------
# Linux clipboard fallback chain (v2.5.1 — was the xclip hardcode)
# ---------------------------------------------------------------------------


def test_linux_clipboard_uses_first_available_tool(tmp_path, monkeypatch):
    """On Linux, _on_copy_url must walk wl-copy → xclip → xsel and pick
    whichever is on PATH. Missing tools must not raise; if none are found,
    the action logs a warning and returns cleanly."""
    import subprocess
    from ragtools import tray as tray_mod

    # Force the Linux branch
    monkeypatch.setattr(tray_mod.sys, "platform", "linux")

    app = TrayApp(_settings(tmp_path))

    # Prime shutil.which so only xclip is "available"
    import shutil
    real_which = shutil.which

    def fake_which(tool):
        return "/usr/bin/xclip" if tool == "xclip" else None

    monkeypatch.setattr(shutil, "which", fake_which)

    # Capture subprocess.run calls
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: calls.append((argv, kw)))

    app._on_copy_url()

    assert len(calls) == 1
    assert calls[0][0][0] == "xclip"
    assert calls[0][0][1:] == ["-selection", "clipboard"]


def test_linux_clipboard_prefers_wl_copy_on_wayland(tmp_path, monkeypatch):
    """If wl-copy is available (Wayland session), it takes precedence over
    xclip/xsel — xclip won't work on Wayland without XWayland fallback."""
    import subprocess
    from ragtools import tray as tray_mod

    monkeypatch.setattr(tray_mod.sys, "platform", "linux")
    app = TrayApp(_settings(tmp_path))

    import shutil
    monkeypatch.setattr(shutil, "which",
                        lambda tool: "/usr/bin/wl-copy" if tool == "wl-copy" else "/usr/bin/xclip")

    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: calls.append(argv))

    app._on_copy_url()

    assert len(calls) == 1
    assert calls[0][0] == "wl-copy"   # preferred even if xclip is also present


def test_linux_clipboard_logs_warning_when_nothing_available(tmp_path, monkeypatch, caplog):
    """When no clipboard tool is present (minimal/headless Linux), the
    tool must log a warning — no exception, no silent failure."""
    import logging
    import subprocess
    from ragtools import tray as tray_mod

    monkeypatch.setattr(tray_mod.sys, "platform", "linux")
    app = TrayApp(_settings(tmp_path))

    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: None)

    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ran.append(a))

    with caplog.at_level(logging.WARNING):
        app._on_copy_url()

    assert ran == []  # no external process launched
    assert any("no clipboard tool" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Config Linux arm (v2.5.1 — was returning None)
# ---------------------------------------------------------------------------


def test_get_app_dir_has_linux_arm(monkeypatch):
    """Linux installed-mode data dir should honour XDG_DATA_HOME with a
    sensible ~/.local/share fallback. Was previously returning None."""
    from pathlib import Path as _P
    import ragtools.config as cfg

    monkeypatch.setattr(cfg.sys, "platform", "linux")

    # With XDG_DATA_HOME set
    monkeypatch.setenv("XDG_DATA_HOME", "/var/lib/demo/data")
    result = cfg._get_app_dir()
    assert result == _P("/var/lib/demo/data") / "RAGTools"

    # Without XDG_DATA_HOME — fall back to ~/.local/share
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    result = cfg._get_app_dir()
    assert result == _P.home() / ".local" / "share" / "RAGTools"


def test_get_app_dir_none_for_unknown_platform(monkeypatch):
    """Backwards-compat: anything that isn't win32/darwin/linux still
    returns None (dev-mode CWD fallback). Not a regression."""
    import ragtools.config as cfg
    monkeypatch.setattr(cfg.sys, "platform", "freebsd13")
    assert cfg._get_app_dir() is None
