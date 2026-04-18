"""Tests for the supervisor that babysits the RAG service.

Split into two sections:

1. Pure-policy tests — exercise SupervisorPolicy without any subprocesses.
   These pin the restart budget, the rolling window, and the backoff math.

2. Loop-integration tests — drive run_supervisor_loop() with a fake
   Popen object so we can simulate clean exits, crashes, and
   give-up conditions deterministically without actually spawning a
   Python child.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import pytest

from ragtools.service.supervisor import (
    SupervisorPolicy,
    run_supervisor_loop,
)


# ---------------------------------------------------------------------------
# Section 1 — Policy (pure)
# ---------------------------------------------------------------------------


def test_policy_default_values():
    p = SupervisorPolicy()
    assert p.max_failures == 5
    assert p.window_seconds == 300.0
    assert p.base_backoff == 2.0
    assert p.max_backoff == 32.0


def test_policy_no_failures_means_no_give_up():
    p = SupervisorPolicy()
    assert p.recent_failures(now=1000.0) == 0
    assert p.should_give_up(now=1000.0) is False


def test_policy_single_failure_doesnt_give_up():
    p = SupervisorPolicy(max_failures=5)
    p.record_failure(now=1000.0)
    assert p.recent_failures(now=1000.0) == 1
    assert not p.should_give_up(now=1000.0)


def test_policy_gives_up_at_threshold():
    p = SupervisorPolicy(max_failures=3)
    p.record_failure(now=100.0)
    p.record_failure(now=101.0)
    p.record_failure(now=102.0)
    assert p.should_give_up(now=102.5)


def test_policy_window_evicts_old_failures():
    p = SupervisorPolicy(max_failures=3, window_seconds=60)
    p.record_failure(now=100.0)
    p.record_failure(now=101.0)
    # 100 seconds later, those two are outside the 60s window.
    assert p.recent_failures(now=200.0) == 0
    assert not p.should_give_up(now=200.0)


def test_policy_mixed_inside_and_outside_window():
    p = SupervisorPolicy(max_failures=3, window_seconds=60)
    p.record_failure(now=100.0)  # expires at 160
    p.record_failure(now=150.0)  # still in window at t=200
    p.record_failure(now=195.0)  # definitely in window at t=200
    assert p.recent_failures(now=200.0) == 2
    assert not p.should_give_up(now=200.0)


def test_backoff_grows_exponentially_up_to_max():
    p = SupervisorPolicy(base_backoff=2, max_backoff=32)
    # No failures → no wait
    assert p.next_backoff(now=0) == 0.0
    # 1 failure → base_backoff
    p.record_failure(now=0)
    assert p.next_backoff(now=0) == 2.0
    # 2nd failure → 4s
    p.record_failure(now=0)
    assert p.next_backoff(now=0) == 4.0
    # 3rd → 8
    p.record_failure(now=0)
    assert p.next_backoff(now=0) == 8.0
    # 4th → 16
    p.record_failure(now=0)
    assert p.next_backoff(now=0) == 16.0
    # 5th → 32 (capped)
    p.record_failure(now=0)
    assert p.next_backoff(now=0) == 32.0


def test_backoff_capped_at_max_backoff():
    p = SupervisorPolicy(base_backoff=2, max_backoff=10)
    for i in range(20):
        p.record_failure(now=i)
    # recent_failures might equal 20; backoff would be 2 * 2**19 = massive,
    # but it must be capped
    assert p.next_backoff(now=20) == 10.0


# ---------------------------------------------------------------------------
# Section 2 — Loop integration with a fake Popen
# ---------------------------------------------------------------------------


@dataclass
class FakePopen:
    """Minimal stand-in for subprocess.Popen for deterministic tests."""
    pid: int
    wait_returns: int = 0
    waited: bool = False

    def wait(self) -> int:
        self.waited = True
        return self.wait_returns


@dataclass
class FakeClock:
    """Injectable clock whose value we control."""
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


@dataclass
class SleepRecorder:
    """Injectable sleeper that records every sleep duration instead of waiting."""
    durations: List[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)


def test_loop_returns_zero_on_clean_child_exit(tmp_path):
    spawned = []

    def spawn():
        p = FakePopen(pid=12345, wait_returns=0)
        spawned.append(p)
        return p

    rc = run_supervisor_loop(
        spawn_child=spawn,
        policy=SupervisorPolicy(),
        marker_path=tmp_path / "gave_up.json",
        clock=FakeClock(10.0),
        sleeper=SleepRecorder(),
    )

    assert rc == 0
    assert len(spawned) == 1  # only spawned once
    assert not (tmp_path / "gave_up.json").exists()


def test_loop_respawns_on_crash_then_succeeds(tmp_path):
    """One crash (code=1), then clean exit (code=0). Supervisor must exit 0."""
    call_count = {"n": 0}

    def spawn():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return FakePopen(pid=1, wait_returns=137)  # crash
        return FakePopen(pid=2, wait_returns=0)  # clean

    sleeper = SleepRecorder()
    rc = run_supervisor_loop(
        spawn_child=spawn,
        policy=SupervisorPolicy(max_failures=5, base_backoff=1, max_backoff=1),
        marker_path=tmp_path / "gave_up.json",
        clock=FakeClock(10.0),
        sleeper=sleeper,
    )

    assert rc == 0
    assert call_count["n"] == 2
    assert sleeper.durations == [1.0]  # one backoff before second spawn
    assert not (tmp_path / "gave_up.json").exists()


def test_loop_gives_up_after_max_failures(tmp_path):
    """Five consecutive crashes → supervisor writes marker and returns 1."""
    spawns = []

    def spawn():
        p = FakePopen(pid=len(spawns) + 1, wait_returns=1)
        spawns.append(p)
        return p

    sleeper = SleepRecorder()
    rc = run_supervisor_loop(
        spawn_child=spawn,
        policy=SupervisorPolicy(
            max_failures=5, window_seconds=3600, base_backoff=1, max_backoff=1,
        ),
        marker_path=tmp_path / "gave_up.json",
        clock=FakeClock(0),
        sleeper=sleeper,
    )

    assert rc == 1
    # Spawned exactly max_failures times, then gave up
    assert len(spawns) == 5
    marker = tmp_path / "gave_up.json"
    assert marker.exists()
    payload = json.loads(marker.read_text())
    assert payload["max_failures"] == 5
    assert payload["window_seconds"] == 3600
    assert "exceeded" in payload["reason"]


def test_loop_on_start_hook_is_invoked_per_spawn(tmp_path):
    calls = []

    def spawn():
        return FakePopen(pid=999, wait_returns=0)

    def on_start(p):
        calls.append(p.pid)

    rc = run_supervisor_loop(
        spawn_child=spawn,
        policy=SupervisorPolicy(),
        marker_path=tmp_path / "gave_up.json",
        on_start=on_start,
        clock=FakeClock(0),
        sleeper=SleepRecorder(),
    )

    assert rc == 0
    assert calls == [999]


def test_loop_failing_on_start_hook_does_not_kill_supervisor(tmp_path):
    """A bug in the on_start hook must not crash the supervisor itself."""
    def spawn():
        return FakePopen(pid=999, wait_returns=0)

    def explosive_hook(_p):
        raise RuntimeError("hook blew up")

    rc = run_supervisor_loop(
        spawn_child=spawn,
        policy=SupervisorPolicy(),
        marker_path=tmp_path / "gave_up.json",
        on_start=explosive_hook,
        clock=FakeClock(0),
        sleeper=SleepRecorder(),
    )
    assert rc == 0  # child exited cleanly; hook error did not propagate


def test_loop_window_expiry_resets_failure_count(tmp_path):
    """Failures outside the rolling window do not count toward giving up."""

    clock = FakeClock(0)
    call_count = {"n": 0}

    def spawn():
        call_count["n"] += 1
        # First 3 calls crash, then clean.
        if call_count["n"] <= 3:
            return FakePopen(pid=call_count["n"], wait_returns=1)
        return FakePopen(pid=call_count["n"], wait_returns=0)

    def ticking_sleep(seconds):
        # Advance the clock so that each failure "ages out" of the window
        # before the next one is recorded.
        clock.value += 1_000_000  # much larger than window_seconds

    rc = run_supervisor_loop(
        spawn_child=spawn,
        policy=SupervisorPolicy(
            max_failures=3, window_seconds=60, base_backoff=1, max_backoff=1,
        ),
        marker_path=tmp_path / "gave_up.json",
        clock=clock,
        sleeper=ticking_sleep,
    )

    assert rc == 0
    assert call_count["n"] == 4
    assert not (tmp_path / "gave_up.json").exists()


# ---------------------------------------------------------------------------
# Section 3 — Notification hooks (on_crash, on_give_up)
# ---------------------------------------------------------------------------


def test_on_crash_fires_on_each_crash_with_exit_code_and_failure_count(tmp_path):
    """Every crash must call on_crash with (exit_code, recent_failures)."""
    crashes = []
    call_count = {"n": 0}

    def spawn():
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return FakePopen(pid=call_count["n"], wait_returns=42)
        return FakePopen(pid=call_count["n"], wait_returns=0)

    rc = run_supervisor_loop(
        spawn_child=spawn,
        policy=SupervisorPolicy(max_failures=5, base_backoff=0, max_backoff=0),
        marker_path=tmp_path / "gave_up.json",
        clock=FakeClock(0),
        sleeper=SleepRecorder(),
        on_crash=lambda code, fails: crashes.append((code, fails)),
    )

    assert rc == 0
    # Two crashes, each observed by the hook.
    assert crashes == [(42, 1), (42, 2)]


def test_on_crash_hook_exception_does_not_kill_supervisor(tmp_path):
    """A broken notification hook must never stop the supervisor loop."""
    call_count = {"n": 0}

    def spawn():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return FakePopen(pid=1, wait_returns=1)
        return FakePopen(pid=2, wait_returns=0)

    def explosive_hook(code, fails):
        raise RuntimeError("toast subsystem on fire")

    rc = run_supervisor_loop(
        spawn_child=spawn,
        policy=SupervisorPolicy(base_backoff=0, max_backoff=0),
        marker_path=tmp_path / "gave_up.json",
        clock=FakeClock(0),
        sleeper=SleepRecorder(),
        on_crash=explosive_hook,
    )
    assert rc == 0  # supervisor survived and recovered


def test_on_give_up_fires_with_reason_string(tmp_path):
    """When the supervisor exhausts its budget, on_give_up gets the reason."""
    recorded = []

    def spawn():
        return FakePopen(pid=1, wait_returns=1)

    rc = run_supervisor_loop(
        spawn_child=spawn,
        policy=SupervisorPolicy(
            max_failures=3, window_seconds=3600, base_backoff=0, max_backoff=0,
        ),
        marker_path=tmp_path / "gave_up.json",
        clock=FakeClock(0),
        sleeper=SleepRecorder(),
        on_give_up=lambda reason: recorded.append(reason),
    )

    assert rc == 1
    assert len(recorded) == 1
    assert "exceeded" in recorded[0]
    assert "3" in recorded[0]  # max_failures


def test_on_give_up_hook_exception_is_swallowed(tmp_path):
    """The supervisor must still exit 1 and write the marker even if the hook raises."""

    def spawn():
        return FakePopen(pid=1, wait_returns=1)

    def bad_give_up_hook(reason):
        raise RuntimeError("toast on fire")

    rc = run_supervisor_loop(
        spawn_child=spawn,
        policy=SupervisorPolicy(
            max_failures=2, window_seconds=3600, base_backoff=0, max_backoff=0,
        ),
        marker_path=tmp_path / "gave_up.json",
        clock=FakeClock(0),
        sleeper=SleepRecorder(),
        on_give_up=bad_give_up_hook,
    )
    assert rc == 1
    assert (tmp_path / "gave_up.json").exists()  # marker still written


def test_hooks_are_optional(tmp_path):
    """Omitting both hooks must keep the existing behaviour."""

    def spawn():
        return FakePopen(pid=1, wait_returns=0)

    rc = run_supervisor_loop(
        spawn_child=spawn,
        policy=SupervisorPolicy(),
        marker_path=tmp_path / "gave_up.json",
        clock=FakeClock(0),
        sleeper=SleepRecorder(),
    )
    assert rc == 0
