"""Tests for service process management, especially stale PID handling.

The field-report incident showed a stale PID file surviving a hard service
crash. These tests pin the desired behavior:
  - A PID file pointing at a dead PID must be treated as "not running"
  - The stale file must be removed on read so downstream code sees truth
  - service_status() must report running=False when the PID is dead
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from ragtools.config import Settings
from ragtools.service import process as proc_mod


def _make_settings(tmp_path: Path) -> Settings:
    """Build a Settings that routes pid/state/qdrant paths through tmp_path."""
    return Settings(
        qdrant_path=str(tmp_path / "qdrant"),
        state_db=str(tmp_path / "state.db"),
    )


def _write_pid_file(settings: Settings, pid: int) -> Path:
    path = proc_mod.get_pid_file_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid))
    return path


def _find_dead_pid() -> int:
    """Return a PID that is highly unlikely to be alive.

    Uses a very high number that's extremely unlikely to be assigned by the
    OS right now. On Windows, PIDs are multiples of 4 and typically well
    below 4 million; 3_999_999 is safely dead.
    """
    candidate = 3_999_999
    # Walk down in case we're wildly unlucky
    while candidate > 1000 and proc_mod._process_alive(candidate):
        candidate -= 4
    if proc_mod._process_alive(candidate):
        pytest.skip("Could not find a dead PID on this system")
    return candidate


def test_read_pid_raw_returns_value_without_liveness_check(tmp_path):
    """_read_pid_raw gives you the on-disk value regardless of liveness."""
    settings = _make_settings(tmp_path)
    dead_pid = _find_dead_pid()
    _write_pid_file(settings, dead_pid)

    assert proc_mod._read_pid_raw(settings) == dead_pid


def test_read_pid_returns_none_for_dead_pid(tmp_path):
    """_read_pid must NOT return a PID whose process is gone."""
    settings = _make_settings(tmp_path)
    dead_pid = _find_dead_pid()
    _write_pid_file(settings, dead_pid)

    assert proc_mod._read_pid(settings) is None


def test_read_pid_self_heals_stale_file(tmp_path):
    """Stale PID file must be removed on read — the core field-report fix."""
    settings = _make_settings(tmp_path)
    pid_path = _write_pid_file(settings, _find_dead_pid())
    assert pid_path.exists()

    proc_mod._read_pid(settings)

    assert not pid_path.exists(), (
        "Stale PID file should have been self-cleaned by _read_pid."
    )


def test_read_pid_returns_alive_pid(tmp_path):
    """_read_pid must return our own PID (which is alive)."""
    settings = _make_settings(tmp_path)
    my_pid = os.getpid()
    _write_pid_file(settings, my_pid)

    result = proc_mod._read_pid(settings)

    assert result == my_pid
    # And the file must still exist (the process is alive)
    assert proc_mod.get_pid_file_path(settings).exists()


def test_read_pid_handles_missing_file(tmp_path):
    settings = _make_settings(tmp_path)
    assert proc_mod._read_pid(settings) is None


def test_read_pid_handles_malformed_file(tmp_path):
    settings = _make_settings(tmp_path)
    pid_path = proc_mod.get_pid_file_path(settings)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("not a number")

    # Must not raise; must return None
    assert proc_mod._read_pid(settings) is None


def test_service_status_reports_not_running_for_stale_pid(tmp_path):
    """End-to-end field-report scenario: stale PID + no HTTP server = not running."""
    settings = _make_settings(tmp_path)
    _write_pid_file(settings, _find_dead_pid())
    # Use a port that is very unlikely to have anything running on it.
    # If it does, the test would report running=True and be inconclusive,
    # not fail-incorrectly.
    settings = Settings(
        qdrant_path=str(tmp_path / "qdrant"),
        state_db=str(tmp_path / "state.db"),
        service_port=59999,
    )
    _write_pid_file(settings, _find_dead_pid())

    status = proc_mod.service_status(settings)

    assert status == {"running": False}
    # Stale file should be gone
    assert not proc_mod.get_pid_file_path(settings).exists()


def test_clean_stale_pid_is_idempotent(tmp_path):
    """Calling _clean_stale_pid twice must be safe."""
    settings = _make_settings(tmp_path)
    _write_pid_file(settings, _find_dead_pid())

    proc_mod._clean_stale_pid(settings)
    proc_mod._clean_stale_pid(settings)  # must not raise

    assert not proc_mod.get_pid_file_path(settings).exists()


# ---------------------------------------------------------------------------
# stop_service escalation — field-report fix. The old 30s poll starved
# external callers (tray apps, watchdog scripts) that wrapped stop_service
# in subprocess.run(timeout=10) and hit their own deadline before the
# internal force-kill ever fired.
# ---------------------------------------------------------------------------


def test_stop_service_graceful_wait_is_short(tmp_path):
    """The polling window between HTTP-accept and force-kill escalation
    must be short enough for external callers with ~15s timeouts."""
    # 6 seconds is what we pin (see _GRACEFUL_SHUTDOWN_WAIT_SECONDS).
    # Intentionally hard-coded so regressions that bump it to 30 break here.
    assert proc_mod._GRACEFUL_SHUTDOWN_WAIT_SECONDS <= 10


def test_stop_service_escalates_to_force_kill_when_graceful_times_out(tmp_path, monkeypatch):
    """If /api/shutdown returns 200 but the process never exits, stop_service
    must escalate to _force_kill rather than hang forever. Mocks the HTTP
    shutdown + PID liveness so we can observe the escalation path without
    spawning a real service."""
    settings = Settings(
        qdrant_path=str(tmp_path / "q"),
        state_db=str(tmp_path / "s.db"),
        service_host="127.0.0.1",
        service_port=59999,
    )

    # Write a PID file that _read_pid will see as alive — use our own PID.
    _write_pid_file(settings, os.getpid())

    # Fake httpx: shutdown succeeds with 200.
    class _FakeResp:
        status_code = 200

    import httpx as _httpx_mod
    monkeypatch.setattr(
        _httpx_mod, "post",
        lambda *a, **k: _FakeResp(),
    )

    # Shrink the poll window so the test isn't slow.
    monkeypatch.setattr(proc_mod, "_GRACEFUL_SHUTDOWN_WAIT_SECONDS", 1)

    # Stub force-kill so we can observe it was called and return True.
    force_kill_called = {"n": 0}

    def _fake_force_kill(s):
        force_kill_called["n"] += 1
        proc_mod.get_pid_file_path(s).unlink(missing_ok=True)
        return True

    monkeypatch.setattr(proc_mod, "_force_kill", _fake_force_kill)

    # Our own PID stays alive (we are running), so stop_service polls
    # ``_GRACEFUL_SHUTDOWN_WAIT_SECONDS`` then must call force_kill.
    assert proc_mod.stop_service(settings) is True
    assert force_kill_called["n"] == 1


def test_stop_service_returns_true_on_fast_graceful_exit(tmp_path, monkeypatch):
    """If the PID disappears during the poll window, stop_service returns
    True without invoking force-kill."""
    settings = Settings(
        qdrant_path=str(tmp_path / "q"),
        state_db=str(tmp_path / "s.db"),
        service_host="127.0.0.1",
        service_port=59999,
    )

    # Write a *dead* PID so _read_pid() immediately returns None.
    _write_pid_file(settings, _find_dead_pid())

    class _FakeResp:
        status_code = 200

    import httpx as _httpx_mod
    monkeypatch.setattr(
        _httpx_mod, "post",
        lambda *a, **k: _FakeResp(),
    )

    force_kill_called = {"n": 0}

    def _fake_force_kill(s):
        force_kill_called["n"] += 1
        return True

    monkeypatch.setattr(proc_mod, "_force_kill", _fake_force_kill)

    assert proc_mod.stop_service(settings) is True
    assert force_kill_called["n"] == 0, "force_kill must not fire for a clean graceful exit"
