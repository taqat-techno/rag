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
    WatchdogAction,
    WatchdogResult,
    _build_schtasks_delete_args,
    _build_schtasks_install_args,
    _build_schtasks_query_args,
    _parse_schtasks_list_output,
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


def test_install_calls_subprocess_when_on_windows(monkeypatch):
    """Monkeypatch the sys.platform check to simulate Windows so we can
    exercise the install path on any CI runner."""
    monkeypatch.setattr("ragtools.service.watchdog.sys", type("X", (), {"platform": "win32", "executable": "python"}))
    calls: List[List[str]] = []

    def runner(argv):
        calls.append(argv)
        return FakeProc(returncode=0)

    ok = install_watchdog_task(_settings(), interval_minutes=15, runner=runner)
    assert ok is True
    assert len(calls) == 1
    assert calls[0][0] == "schtasks"
    assert "/create" in calls[0]


def test_install_returns_false_on_failure(monkeypatch):
    monkeypatch.setattr("ragtools.service.watchdog.sys", type("X", (), {"platform": "win32", "executable": "python"}))

    def runner(argv):
        return FakeProc(returncode=1, stderr="ERROR: Access denied")

    ok = install_watchdog_task(_settings(), runner=runner)
    assert ok is False


def test_install_skipped_on_non_windows(monkeypatch):
    monkeypatch.setattr("ragtools.service.watchdog.sys", type("X", (), {"platform": "linux"}))
    called = []

    def runner(argv):
        called.append(argv)
        return FakeProc()

    ok = install_watchdog_task(_settings(), runner=runner)
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
