"""Tests for the Task Scheduler watchdog.

The watchdog has three layers:
  1. decide_action — pure (alive → NOTHING, dead → START)
  2. schtasks argv builders — pure string assembly
  3. install/uninstall/check — thin wrappers over subprocess + start_service

Tests exercise every layer without touching Task Scheduler or spawning a
real service. Subprocess + start_service are injected via Callable params.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import pytest

from ragtools.config import Settings
from ragtools.service.watchdog import (
    DEFAULT_INTERVAL_MINUTES,
    TASK_NAME,
    WATCHDOG_VBS_FILENAME,
    WatchdogAction,
    WatchdogResult,
    _build_schtasks_delete_args,
    _build_schtasks_install_args,
    _build_schtasks_query_args,
    _build_watchdog_vbs,
    _parse_schtasks_list_output,
    _watchdog_vbs_path,
    _wscript_path,
    decide_action,
    get_watchdog_info,
    install_watchdog_task,
    is_watchdog_installed,
    run_check,
    uninstall_watchdog_task,
)


@dataclass
class FakeProc:
    """Stand-in for subprocess.CompletedProcess."""
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _settings() -> Settings:
    return Settings(qdrant_path="/tmp/q", state_db="/tmp/s.db")


def _settings_in(tmp_path) -> Settings:
    """Settings with the data dir routed through pytest's tmp_path.

    Required for any test that exercises ``install_watchdog_task`` since it
    now writes ``RAGTools-Watchdog.vbs`` next to the PID files.
    """
    return Settings(
        qdrant_path=str(tmp_path / "qdrant"),
        state_db=str(tmp_path / "state.db"),
    )


# ---------------------------------------------------------------------------
# Pure layer — decide_action
# ---------------------------------------------------------------------------


def test_decide_action_alive_is_nothing():
    assert decide_action(is_alive=True) == WatchdogAction.NOTHING


def test_decide_action_dead_is_start():
    assert decide_action(is_alive=False) == WatchdogAction.START


# ---------------------------------------------------------------------------
# Argv builders — pure
# ---------------------------------------------------------------------------


def test_install_args_contain_expected_flags():
    args = _build_schtasks_install_args(
        task_name="TestTask",
        interval_minutes=15,
        user="DOMAIN\\tester",
        command_parts=["rag.exe", "service", "watchdog", "check"],
    )
    # The exact argv order matters to schtasks.
    assert args[0] == "schtasks"
    assert args[1] == "/create"
    assert "/tn" in args
    assert args[args.index("/tn") + 1] == "TestTask"
    assert "/sc" in args and args[args.index("/sc") + 1] == "minute"
    assert "/mo" in args and args[args.index("/mo") + 1] == "15"
    assert "/rl" in args and args[args.index("/rl") + 1] == "limited"
    assert "/ru" in args and args[args.index("/ru") + 1] == "DOMAIN\\tester"
    assert "/f" in args  # force-overwrite idempotency


def test_install_args_tr_value_is_single_string():
    """schtasks /tr takes ONE string, not argv. Multi-word commands must be
    joined so quoting survives."""
    args = _build_schtasks_install_args(
        command_parts=["C:\\Program Files\\foo\\rag.exe", "service", "watchdog", "check"],
    )
    tr = args[args.index("/tr") + 1]
    # Path containing a space must be wrapped in quotes.
    assert '"C:\\Program Files\\foo\\rag.exe"' in tr
    assert "service watchdog check" in tr


def test_install_args_reject_invalid_interval():
    with pytest.raises(ValueError, match="interval_minutes"):
        _build_schtasks_install_args(interval_minutes=0)


def test_delete_args_shape():
    args = _build_schtasks_delete_args("Foo")
    assert args == ["schtasks", "/delete", "/tn", "Foo", "/f"]


def test_query_args_shape():
    args = _build_schtasks_query_args("Foo")
    assert args[:4] == ["schtasks", "/query", "/tn", "Foo"]
    assert "LIST" in args and "/v" in args


def test_parse_list_output_extracts_key_fields():
    sample = (
        "Folder: \\\n"
        "HostName: BOX\n"
        "TaskName: \\RAGTools Watchdog\n"
        "Next Run Time: 4/18/2026 2:30:00 AM\n"
        "Status: Ready\n"
        "Last Run Time: 4/18/2026 2:15:00 AM\n"
        "Last Result: 0\n"
        "Task To Run: rag.exe service watchdog check\n"
    )
    fields = _parse_schtasks_list_output(sample)
    assert fields["Status"] == "Ready"
    assert fields["Last Result"] == "0"
    assert fields["Task To Run"] == "rag.exe service watchdog check"


# ---------------------------------------------------------------------------
# Install / uninstall / is_installed — subprocess mocked
# ---------------------------------------------------------------------------


def test_install_calls_subprocess_when_on_windows(monkeypatch, tmp_path):
    """Monkeypatch the sys.platform check to simulate Windows so we can
    exercise the install path on any CI runner."""
    monkeypatch.setattr("ragtools.service.watchdog.sys", type("X", (), {"platform": "win32", "executable": "python"}))
    calls: List[List[str]] = []

    def runner(argv):
        calls.append(argv)
        return FakeProc(returncode=0)

    ok = install_watchdog_task(_settings_in(tmp_path), interval_minutes=15, runner=runner)
    assert ok is True
    assert len(calls) == 1
    assert calls[0][0] == "schtasks"
    assert "/create" in calls[0]


def test_install_returns_false_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("ragtools.service.watchdog.sys", type("X", (), {"platform": "win32", "executable": "python"}))

    def runner(argv):
        return FakeProc(returncode=1, stderr="ERROR: Access denied")

    ok = install_watchdog_task(_settings_in(tmp_path), runner=runner)
    assert ok is False


def test_install_skipped_on_non_windows(monkeypatch, tmp_path):
    monkeypatch.setattr("ragtools.service.watchdog.sys", type("X", (), {"platform": "linux"}))
    called = []

    def runner(argv):
        called.append(argv)
        return FakeProc()

    ok = install_watchdog_task(_settings_in(tmp_path), runner=runner)
    assert ok is False
    assert called == []  # subprocess never invoked


def test_uninstall_treats_missing_task_as_success(monkeypatch):
    monkeypatch.setattr("ragtools.service.watchdog.sys", type("X", (), {"platform": "win32"}))

    def runner(argv):
        return FakeProc(returncode=1, stderr="ERROR: The system cannot find the file specified.")

    # Should still return True: a missing task is the end state we want.
    assert uninstall_watchdog_task(runner=runner) is True


def test_uninstall_returns_true_on_clean_removal(monkeypatch):
    monkeypatch.setattr("ragtools.service.watchdog.sys", type("X", (), {"platform": "win32"}))
    assert uninstall_watchdog_task(runner=lambda argv: FakeProc(returncode=0)) is True


def test_uninstall_returns_false_on_real_error(monkeypatch):
    monkeypatch.setattr("ragtools.service.watchdog.sys", type("X", (), {"platform": "win32"}))

    def runner(argv):
        return FakeProc(returncode=1, stderr="ERROR: Unexpected failure")

    assert uninstall_watchdog_task(runner=runner) is False


def test_is_watchdog_installed_checks_query_rc(monkeypatch):
    monkeypatch.setattr("ragtools.service.watchdog.sys", type("X", (), {"platform": "win32"}))
    assert is_watchdog_installed(runner=lambda argv: FakeProc(returncode=0)) is True
    assert is_watchdog_installed(runner=lambda argv: FakeProc(returncode=1)) is False


def test_is_watchdog_installed_false_on_non_windows(monkeypatch):
    monkeypatch.setattr("ragtools.service.watchdog.sys", type("X", (), {"platform": "linux"}))
    assert is_watchdog_installed(runner=lambda argv: FakeProc(returncode=0)) is False


def test_get_watchdog_info_returns_parsed_fields(monkeypatch):
    monkeypatch.setattr("ragtools.service.watchdog.sys", type("X", (), {"platform": "win32"}))
    stdout = (
        "TaskName: \\RAGTools Watchdog\n"
        "Status: Ready\n"
        "Next Run Time: 4/18/2026 2:30:00 AM\n"
        "Last Run Time: 4/18/2026 2:15:00 AM\n"
        "Last Result: 0\n"
        "Task To Run: rag.exe service watchdog check\n"
    )

    def runner(argv):
        return FakeProc(returncode=0, stdout=stdout)

    info = get_watchdog_info(runner=runner)
    assert info is not None
    assert info["task_name"] == TASK_NAME
    assert info["status"] == "Ready"
    assert info["last_result"] == "0"


def test_get_watchdog_info_none_when_not_installed(monkeypatch):
    monkeypatch.setattr("ragtools.service.watchdog.sys", type("X", (), {"platform": "win32"}))
    assert get_watchdog_info(runner=lambda argv: FakeProc(returncode=1)) is None


# ---------------------------------------------------------------------------
# run_check — probe + start_service injected
# ---------------------------------------------------------------------------


def test_run_check_alive_does_nothing():
    calls = []
    result = run_check(
        _settings(),
        probe=lambda s: True,
        starter=lambda s: calls.append("boom") or 0,
    )
    assert result.action == WatchdogAction.NOTHING
    assert calls == []


def test_run_check_dead_calls_starter_and_reports_pid():
    result = run_check(
        _settings(),
        probe=lambda s: False,
        starter=lambda s: 12345,
    )
    assert result.action == WatchdogAction.START
    assert result.started_pid == 12345


def test_run_check_already_running_is_not_an_error():
    """If start_service raises RuntimeError('already running'), that's not a
    failure — it's evidence the probe/start race was won by someone else."""

    def raising_starter(s):
        raise RuntimeError("Service already running (PID 99)")

    result = run_check(
        _settings(),
        probe=lambda s: False,
        starter=raising_starter,
    )
    assert result.action == WatchdogAction.ALREADY_STARTING
    assert "99" in result.note


def test_run_check_swallows_unexpected_starter_exceptions():
    """Task Scheduler must never see a non-zero exit. So the watchdog
    must survive any exception from start_service."""

    def exploding_starter(s):
        raise Exception("unexpected")

    result = run_check(
        _settings(),
        probe=lambda s: False,
        starter=exploding_starter,
    )
    assert result.action == WatchdogAction.START  # intent was to start
    assert "unexpected" in result.note


# ---------------------------------------------------------------------------
# Silent VBS launcher — pure rendering + install side-effect
# ---------------------------------------------------------------------------


def test_watchdog_vbs_hides_window():
    """shell.Run with style 0 hides the console; window MUST be hidden so
    Task Scheduler firings don't flash a conhost window every interval."""
    vbs = _build_watchdog_vbs(["rag.exe", "service", "watchdog", "check"])
    assert "shell.Run" in vbs
    assert ", 0, False" in vbs


def test_watchdog_vbs_quotes_paths_with_spaces():
    """Embedded ``"`` in the rendered command string must be doubled per
    VBS string-literal rules so the command survives shell.Run parsing."""
    vbs = _build_watchdog_vbs(
        ["C:\\Program Files\\RAGTools\\rag.exe", "service", "watchdog", "check"]
    )
    # The path with a space must end up wrapped in doubled quotes inside
    # the VBS literal: ""C:\Program Files\RAGTools\rag.exe""
    assert '""C:\\Program Files\\RAGTools\\rag.exe""' in vbs
    assert "service watchdog check" in vbs


def test_watchdog_vbs_path_is_sibling_of_pid_files(tmp_path):
    """The VBS lives in the same data dir as service.pid / supervisor.pid /
    tray.pid — never in the install dir, never in the Startup folder."""
    settings = _settings_in(tmp_path)
    p = _watchdog_vbs_path(settings)
    assert p.name == WATCHDOG_VBS_FILENAME
    assert p.parent == tmp_path  # sibling of service.pid
    assert WATCHDOG_VBS_FILENAME == "RAGTools-Watchdog.vbs"  # name is part of contract


def test_install_writes_vbs_and_schtasks_calls_wscript(monkeypatch, tmp_path):
    """End-to-end install: VBS must land on disk AND schtasks must point
    at wscript.exe + the VBS, not the bare console exe."""
    monkeypatch.setattr(
        "ragtools.service.watchdog.sys",
        type("X", (), {"platform": "win32", "executable": "python"}),
    )

    captured: List[List[str]] = []

    def runner(argv):
        captured.append(argv)
        return FakeProc(returncode=0)

    settings = _settings_in(tmp_path)
    ok = install_watchdog_task(settings, runner=runner)
    assert ok is True

    # 1. VBS file actually written
    vbs_path = _watchdog_vbs_path(settings)
    assert vbs_path.exists(), f"VBS launcher should have been written to {vbs_path}"
    body = vbs_path.read_text(encoding="utf-8")
    assert "shell.Run" in body
    assert ", 0, False" in body

    # 2. The schtasks /tr value runs wscript + VBS, NOT bare rag.exe
    args = captured[0]
    tr_value = args[args.index("/tr") + 1]
    assert "wscript.exe" in tr_value.lower()
    assert WATCHDOG_VBS_FILENAME in tr_value
    # Strict negative: the rendered task action must not run rag.exe directly.
    assert "rag.exe" not in tr_value.lower(), (
        f"Task action still flashes a console window: {tr_value}"
    )


def test_install_failure_still_writes_vbs_but_returns_false(monkeypatch, tmp_path):
    """A schtasks failure should still leave the VBS on disk (cheap, idempotent
    on next install) but the function must return False so the caller knows."""
    monkeypatch.setattr(
        "ragtools.service.watchdog.sys",
        type("X", (), {"platform": "win32", "executable": "python"}),
    )

    settings = _settings_in(tmp_path)
    ok = install_watchdog_task(
        settings,
        runner=lambda argv: FakeProc(returncode=1, stderr="ERROR: Access denied"),
    )
    assert ok is False
    # VBS is harmless; we wrote it before schtasks ran.
    assert _watchdog_vbs_path(settings).exists()


def test_uninstall_cleans_up_vbs_when_settings_provided(monkeypatch, tmp_path):
    """When the caller passes settings, the sidecar VBS is removed alongside
    the schtasks delete. Without settings, the sidecar is left in place."""
    monkeypatch.setattr(
        "ragtools.service.watchdog.sys",
        type("X", (), {"platform": "win32"}),
    )

    settings = _settings_in(tmp_path)
    vbs_path = _watchdog_vbs_path(settings)
    vbs_path.parent.mkdir(parents=True, exist_ok=True)
    vbs_path.write_text("' stub", encoding="utf-8")
    assert vbs_path.exists()

    ok = uninstall_watchdog_task(
        runner=lambda argv: FakeProc(returncode=0),
        settings=settings,
    )
    assert ok is True
    assert not vbs_path.exists(), "VBS sidecar should be removed on uninstall"


def test_wscript_path_uses_system32():
    """Task Scheduler at LIMITED runlevel can't trust PATH. The absolute
    System32 path is the canonical wscript.exe location on every Windows."""
    p = _wscript_path().lower()
    assert p.endswith("wscript.exe")
    assert "system32" in p
