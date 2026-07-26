"""Periodic maintenance, owned by the service — not by the operating system.

The OS's only scheduled job is "start me at login". Everything periodic lives
here, inside the process that already holds the locks and the state.

That inversion is the point. The previous design put a ``RAGTools Watchdog``
task in Task Scheduler that woke every fifteen minutes to probe a service which
was almost always healthy, flashing a console each time, and had no counterpart
on Linux or macOS. Restart-on-failure is a native capability of all three
schedulers; *maintenance* is not, and it needs things only the service has —
the index mutex, the job store, the live storage handle.

Design rules, each earned:

* **One owner.** A task runs in the service process, so two of them cannot race
  by construction. There is no second process to get out of step.
* **Never block on a lock.** A maintenance task that waits for the index mutex
  turns a routine sweep into a stall. It skips and re-arms — the next tick is
  minutes away, not months.
* **Failure is durable and visible.** A task that fails silently is worse than
  one that never ran, because the machine looks healthy.
* **No keepalive.** Nothing here exists to prove the service is alive; the
  service running *is* that proof.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("ragtools.maintenance")

MINUTE = 60.0
HOUR = 60 * MINUTE
DAY = 24 * HOUR
WEEK = 7 * DAY

#: Lock policy. `index` tasks contend with indexing and must skip rather than
#: wait; `none` tasks are cheap and independent.
LOCK_INDEX = "index"
LOCK_NONE = "none"


@dataclass
class Task:
    """One periodic action."""

    name: str
    interval: float
    action: Callable[[], object]
    lock: str = LOCK_NONE
    #: Run once at startup as well as on the interval. Recovery tasks want this:
    #: the most likely moment to find an interrupted job is just after the crash
    #: that interrupted it.
    run_at_startup: bool = False
    description: str = ""

    # runtime state
    #: When the scheduler first saw this task. A task that is not a startup task
    #: has to measure its first interval from SOMETHING; without this it waits
    #: on `last_run`, which is only set by running, so it could never run at all.
    armed_at: Optional[float] = None
    last_run: Optional[float] = None
    last_ok: Optional[bool] = None
    last_error: str = ""
    runs: int = 0
    failures: int = 0
    skips: int = 0

    def due(self, now: float) -> bool:
        if self.last_run is None:
            if self.run_at_startup:
                return True
            if self.armed_at is None:
                return False        # armed on this tick; due one interval later
            return (now - self.armed_at) >= self.interval
        return (now - self.last_run) >= self.interval

    @property
    def healthy(self) -> bool:
        """A task that has never run is not unhealthy — it is not yet due."""
        return self.last_ok is not False


class MaintenanceScheduler:
    """Runs the task table. Pure enough to test without sleeping.

    ``clock`` and ``lock_held`` are injected, so a week of scheduling is a few
    microseconds in the suite and no test has to wait for a real interval.
    """

    def __init__(
        self,
        tasks: Optional[list[Task]] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        lock_held: Optional[Callable[[], bool]] = None,
    ):
        self._tasks = list(tasks or [])
        self._clock = clock
        self._lock_held = lock_held or (lambda: False)

    @property
    def tasks(self) -> list[Task]:
        return list(self._tasks)

    def add(self, task: Task) -> None:
        if any(t.name == task.name for t in self._tasks):
            # Two tasks with one name is how a "run once daily" becomes "run
            # twice daily" after a refactor nobody re-read.
            raise ValueError(f"duplicate maintenance task {task.name!r}")
        self._tasks.append(task)

    def due(self) -> list[Task]:
        now = self._clock()
        for task in self._tasks:
            if task.armed_at is None:
                task.armed_at = now
        return [t for t in self._tasks if t.due(now)]

    def tick(self) -> list[Task]:
        """Run whatever is due. Returns the tasks that actually executed."""
        ran: list[Task] = []
        for task in self.due():
            if task.lock == LOCK_INDEX and self._lock_held():
                # Skip, do not wait. Re-armed on the next tick.
                task.skips += 1
                logger.debug("maintenance %s skipped: index busy", task.name)
                continue
            self._run(task)
            ran.append(task)
        return ran

    def _run(self, task: Task) -> None:
        task.last_run = self._clock()
        task.runs += 1
        try:
            task.action()
        except Exception as exc:  # noqa: BLE001 — one bad task must not stop the rest
            task.last_ok = False
            task.failures += 1
            task.last_error = str(exc)
            logger.error("maintenance %s failed: %s", task.name, exc)
        else:
            task.last_ok = True
            task.last_error = ""

    # --- reporting -------------------------------------------------------

    def health(self) -> dict:
        """What ``/health`` and the tray need. A failing task is a degraded
        service, not a log line nobody reads."""
        failing = [t for t in self._tasks if t.last_ok is False]
        return {
            "tasks": [
                {
                    "name": t.name,
                    "interval_seconds": t.interval,
                    "runs": t.runs,
                    "failures": t.failures,
                    "skips": t.skips,
                    "last_ok": t.last_ok,
                    "last_error": t.last_error,
                }
                for t in self._tasks
            ],
            "degraded": bool(failing),
            "issues": [f"maintenance:{t.name}" for t in failing],
        }


def build_default_tasks(owner, *, runtime=None) -> list[Task]:
    """The product's maintenance table.

    Deliberately small. Every entry earns its place by doing something the
    service cannot do inline, and nothing here probes the service to see whether
    it is running.
    """
    def _recover_stale_jobs():
        if runtime is None:
            return
        # The most likely moment to find an interrupted job is right after the
        # crash that interrupted it, hence run_at_startup.
        runtime.recover_interrupted()

    def _reconcile_counts():
        owner.get_status()

    def _storage_probe():
        owner.storage_reachable()

    def _refresh_frameworks():
        # Framework corpora are not watcher-refreshed: they are keyed by build
        # identity, so a refresh is a deliberate, periodic act rather than a
        # reaction to a file change.
        owner.sync_frameworks(refresh=True)

    return [
        Task("stale-job-recovery", HOUR, _recover_stale_jobs, LOCK_NONE,
             run_at_startup=True,
             description="requeue or terminate jobs interrupted by a restart"),
        Task("storage-probe", MINUTE, _storage_probe, LOCK_NONE,
             run_at_startup=True,
             description="is the vector store actually reachable"),
        Task("count-reconciliation", DAY, _reconcile_counts, LOCK_INDEX,
             description="state DB and Qdrant agree on chunk counts"),
        Task("framework-refresh", WEEK, _refresh_frameworks, LOCK_INDEX,
             description="re-import declared dependency corpora"),
    ]
