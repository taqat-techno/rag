"""Periodic maintenance owned by the service, not the operating system.

The design this replaces put a `RAGTools Watchdog` task in Task Scheduler that
woke every fifteen minutes to probe a service that was almost always healthy,
flashed a console each time, and had no Linux or macOS counterpart. Restart-on-
failure belongs to the init system; *maintenance* belongs here, where the locks
and the state already are.

The behaviours worth pinning are the ones that go wrong quietly: a task that
waits on the index mutex and stalls, a task that fails without anyone noticing,
and a table that grows a second copy of an entry after a refactor.
"""

from __future__ import annotations

import pytest

from ragtools.service.maintenance import (
    DAY,
    HOUR,
    LOCK_INDEX,
    LOCK_NONE,
    MINUTE,
    WEEK,
    MaintenanceScheduler,
    Task,
    build_default_tasks,
)


class Clock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _counter():
    calls = []
    return calls, lambda: calls.append(1)


# --- scheduling -----------------------------------------------------------


def test_a_startup_task_runs_immediately():
    """The most likely moment to find an interrupted job is right after the
    crash that interrupted it."""
    clock = Clock()
    calls, action = _counter()
    sched = MaintenanceScheduler(
        [Task("recover", HOUR, action, run_at_startup=True)], clock=clock)

    sched.tick()
    assert len(calls) == 1


def test_a_non_startup_task_waits_for_its_first_interval():
    clock = Clock()
    calls, action = _counter()
    sched = MaintenanceScheduler([Task("daily", DAY, action)], clock=clock)

    sched.tick()
    assert calls == []

    clock.advance(DAY)
    sched.tick()
    assert len(calls) == 1


def test_a_task_does_not_run_twice_inside_one_interval():
    clock = Clock()
    calls, action = _counter()
    sched = MaintenanceScheduler(
        [Task("hourly", HOUR, action, run_at_startup=True)], clock=clock)

    sched.tick()
    clock.advance(HOUR / 2)
    sched.tick()
    assert len(calls) == 1

    clock.advance(HOUR / 2)
    sched.tick()
    assert len(calls) == 2


def test_a_week_of_ticks_produces_the_expected_number_of_runs():
    """Injected time means a week of scheduling costs microseconds, so the
    cadence is actually verified rather than assumed."""
    clock = Clock()
    calls, action = _counter()
    sched = MaintenanceScheduler(
        [Task("daily", DAY, action, run_at_startup=True)], clock=clock)

    for _ in range(7 * 24):
        sched.tick()
        clock.advance(HOUR)

    assert len(calls) == 7


# --- locking --------------------------------------------------------------


def test_an_index_task_skips_rather_than_waits_while_indexing():
    """Waiting turns a routine sweep into a stall on a machine that is already
    busy doing the thing the user asked for."""
    clock = Clock()
    calls, action = _counter()
    busy = {"held": True}
    sched = MaintenanceScheduler(
        [Task("reconcile", DAY, action, LOCK_INDEX, run_at_startup=True)],
        clock=clock, lock_held=lambda: busy["held"])

    sched.tick()
    assert calls == []
    assert sched.tasks[0].skips == 1


def test_a_skipped_task_is_re_armed_not_lost():
    """The next tick is minutes away, not months — a skip must not consume the
    interval."""
    clock = Clock()
    calls, action = _counter()
    busy = {"held": True}
    sched = MaintenanceScheduler(
        [Task("reconcile", DAY, action, LOCK_INDEX, run_at_startup=True)],
        clock=clock, lock_held=lambda: busy["held"])

    sched.tick()
    busy["held"] = False
    sched.tick()
    assert len(calls) == 1


def test_a_lock_free_task_runs_while_indexing():
    """A storage probe during an index is exactly when its answer matters."""
    clock = Clock()
    calls, action = _counter()
    sched = MaintenanceScheduler(
        [Task("probe", MINUTE, action, LOCK_NONE, run_at_startup=True)],
        clock=clock, lock_held=lambda: True)

    sched.tick()
    assert len(calls) == 1


# --- failure handling -----------------------------------------------------


def test_one_failing_task_does_not_stop_the_others():
    clock = Clock()
    calls, action = _counter()

    def _boom():
        raise RuntimeError("storage unreachable")

    sched = MaintenanceScheduler([
        Task("bad", MINUTE, _boom, run_at_startup=True),
        Task("good", MINUTE, action, run_at_startup=True),
    ], clock=clock)

    sched.tick()
    assert len(calls) == 1


def test_a_failure_is_recorded_with_its_reason():
    """A task that fails silently is worse than one that never ran, because the
    machine looks healthy."""
    clock = Clock()

    def _boom():
        raise RuntimeError("storage unreachable")

    sched = MaintenanceScheduler(
        [Task("probe", MINUTE, _boom, run_at_startup=True)], clock=clock)
    sched.tick()

    task = sched.tasks[0]
    assert task.last_ok is False
    assert task.failures == 1
    assert "storage unreachable" in task.last_error


def test_a_failing_task_makes_the_service_degraded():
    clock = Clock()

    def _boom():
        raise RuntimeError("nope")

    sched = MaintenanceScheduler(
        [Task("probe", MINUTE, _boom, run_at_startup=True)], clock=clock)
    sched.tick()

    health = sched.health()
    assert health["degraded"] is True
    assert health["issues"] == ["maintenance:probe"]


def test_a_recovering_task_clears_the_degraded_state():
    clock = Clock()
    state = {"fail": True}

    def _flaky():
        if state["fail"]:
            raise RuntimeError("transient")

    sched = MaintenanceScheduler(
        [Task("probe", MINUTE, _flaky, run_at_startup=True)], clock=clock)
    sched.tick()
    assert sched.health()["degraded"] is True

    state["fail"] = False
    clock.advance(MINUTE)
    sched.tick()
    assert sched.health()["degraded"] is False


def test_a_task_that_has_never_run_is_not_degraded():
    """Not-yet-due is not unhealthy; reporting it as such would make every
    fresh start look broken."""
    sched = MaintenanceScheduler([Task("weekly", WEEK, lambda: None)], clock=Clock())
    assert sched.health()["degraded"] is False


# --- the table ------------------------------------------------------------


def test_a_duplicate_task_name_is_refused():
    """Two entries with one name is how "run once daily" silently becomes
    "run twice daily"."""
    sched = MaintenanceScheduler([Task("probe", MINUTE, lambda: None)])
    with pytest.raises(ValueError, match="duplicate maintenance task"):
        sched.add(Task("probe", DAY, lambda: None))


def test_the_default_table_contains_no_keepalive():
    """Nothing here exists to prove the service is alive — the service running
    IS that proof. A probe-and-restart task is what the init system does."""
    class _Owner:
        def get_status(self): return {}
        def storage_reachable(self): return True
        def sync_frameworks(self, refresh=False): return []

    names = {t.name for t in build_default_tasks(_Owner())}
    assert not any("keepalive" in n or "watchdog" in n or "restart" in n for n in names)


def test_the_default_table_covers_the_planned_actions():
    class _Owner:
        def get_status(self): return {}
        def storage_reachable(self): return True
        def sync_frameworks(self, refresh=False): return []

    tasks = {t.name: t for t in build_default_tasks(_Owner())}
    assert {"stale-job-recovery", "storage-probe",
            "count-reconciliation", "framework-refresh"} <= set(tasks)

    # Contended work must not block indexing; cheap probes need no lock.
    assert tasks["count-reconciliation"].lock == LOCK_INDEX
    assert tasks["framework-refresh"].lock == LOCK_INDEX
    assert tasks["storage-probe"].lock == LOCK_NONE

    # Recovery runs at startup — that is when interrupted jobs are found.
    assert tasks["stale-job-recovery"].run_at_startup is True


def test_the_framework_refresh_is_periodic_because_the_watcher_does_not_do_it():
    """Framework corpora are keyed by build identity, so refreshing them is a
    deliberate periodic act rather than a reaction to a file change."""
    class _Owner:
        def get_status(self): return {}
        def storage_reachable(self): return True
        def sync_frameworks(self, refresh=False):
            assert refresh is True, "a refresh that reuses the corpus is a no-op"
            return []

    task = {t.name: t for t in build_default_tasks(_Owner())}["framework-refresh"]
    assert task.interval == WEEK
    task.action()
