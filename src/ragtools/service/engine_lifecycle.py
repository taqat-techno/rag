"""The managed engine, watched for its whole life rather than only its birth.

v3.2.0 shipped a class called ``QdrantSupervisor`` that supervised nothing after
startup. Its handle was assigned once at ``app.py:192`` and next read at
``app.py:252`` — the shutdown branch. In between, the engine could die and did:
twice, on two machines, under opposite workloads. The service carried on
answering ``/health`` while every storage operation failed, and no log line
anywhere recorded the death.

This module is the missing half. It owns one child process for its whole life:

**Death is observed, not discovered.** A waiter thread blocks in ``proc.wait()``,
so the exit code arrives the instant it exists rather than being inferred later
from a refused connection. Polling a socket tells you the engine is *unreachable*;
waiting on the child tells you it is *gone*, and which code it went with.

**Intent is recorded before it is acted on.** A stop we asked for and a crash look
identical from an exit code. So ``request_stop`` sets the flag *first*; the waiter
reads it and stays quiet. Without that ordering, shutting the service down starts
a restart storm.

**The manifest is invalidated the moment the child is seen to exit**, closing the
window in which a dead pid is still vouched for — the input a later startup
consults when deciding whether to adopt whatever now holds the port.

**Restarts are bounded, and the bound is loud.** An unexplained crash must not
become an unexplained restart loop. Attempts are capped, backoff is exponential,
and exhaustion is a reported state rather than silence.

**One owner.** The outer service supervisor restarts the *service*; this restarts
the *engine*. Neither reaches into the other's job, and nothing else in the
product spawns or terminates an engine.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("ragtools.service")

# --- states ---------------------------------------------------------------

STARTING = "starting"
READY = "ready"
UNHEALTHY = "unhealthy"
CRASHED = "crashed"
RESTARTING = "restarting"
RESTART_EXHAUSTED = "restart_exhausted"
STOPPING = "stopping"
STOPPED = "stopped"

#: States in which storage must be treated as unusable by callers.
DOWN_STATES = frozenset({CRASHED, RESTARTING, RESTART_EXHAUSTED, STOPPING, STOPPED})

#: How many times the engine is restarted automatically before the product stops
#: trying and says so. Deliberately small: a crash that repeats four times in
#: minutes is a condition a person has to look at, and a machine that keeps
#: relaunching a failing database server is burning a disk, not recovering.
MAX_RESTARTS = 3

#: Backoff between automatic restarts. Same shape as the relayout retry policy —
#: one idea, one implementation, so the two cannot drift apart.
BACKOFF_SECONDS = (2.0, 15.0, 60.0)


@dataclass
class EngineEvent:
    """One thing that happened to the engine, for the activity log and /health."""

    at: float
    state: str
    detail: str = ""
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    attempt: int = 0


@dataclass
class EngineStatus:
    """The single storage snapshot every endpoint reads.

    Nothing on a request path may reach the engine to build this — that is the
    62-second ``/api/status`` stall. It is maintained here and read cheaply.
    """

    state: str = STOPPED
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    restart_attempt: int = 0
    max_restarts: int = MAX_RESTARTS
    started_at: Optional[float] = None
    died_at: Optional[float] = None
    detail: str = ""
    log_path: Optional[str] = None
    log_error: str = ""
    history: list = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.state in (READY, UNHEALTHY)

    @property
    def uptime(self) -> Optional[float]:
        if self.started_at is None or self.state not in (READY, UNHEALTHY):
            return None
        return max(0.0, time.time() - self.started_at)

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "restart_attempt": self.restart_attempt,
            "max_restarts": self.max_restarts,
            "uptime_seconds": self.uptime,
            "died_at": self.died_at,
            "detail": self.detail,
            "log_path": self.log_path,
            "log_error": self.log_error,
            "available": self.available,
            "history": [
                {"at": e.at, "state": e.state, "detail": e.detail, "pid": e.pid,
                 "exit_code": e.exit_code, "attempt": e.attempt}
                for e in self.history[-10:]
            ],
        }


class EngineLifecycle:
    """Owns one managed engine: start, watch, restart, stop.

    ``starter`` and ``stopper`` are injected so the whole state machine is
    testable without spawning a database server — which is the only way the
    crash paths get tested at all.
    """

    def __init__(
        self,
        settings,
        *,
        starter: Optional[Callable] = None,
        stopper: Optional[Callable] = None,
        on_state_change: Optional[Callable] = None,
        max_restarts: int = MAX_RESTARTS,
        backoff: tuple = BACKOFF_SECONDS,
        sleep: Callable = time.sleep,
        clock: Callable = time.time,
        poll_interval: float = 5.0,
    ):
        self._poll_interval = poll_interval
        self._settings = settings
        self._starter = starter or _default_starter
        self._stopper = stopper or _default_stopper
        self._on_state_change = on_state_change
        self._max_restarts = max_restarts
        self._backoff = backoff or (0.0,)
        self._sleep = sleep
        self._clock = clock

        self._lock = threading.RLock()
        self._status = EngineStatus(state=STOPPED, max_restarts=max_restarts)
        self._supervisor = None
        self._url: Optional[str] = None
        self._watcher: Optional[threading.Thread] = None
        #: Set BEFORE any deliberate stop, so the waiter can tell a shutdown we
        #: asked for from a crash. Ordering is the whole point.
        self._stopping = threading.Event()

    # --- reporting ---------------------------------------------------------

    @property
    def status(self) -> EngineStatus:
        with self._lock:
            return self._status

    @property
    def url(self) -> Optional[str]:
        return self._url

    @property
    def supervisor(self):
        return self._supervisor

    def _set_state(self, state: str, *, detail: str = "", pid=None,
                   exit_code=None, attempt: int = 0) -> None:
        with self._lock:
            self._status.state = state
            self._status.detail = detail
            if pid is not None:
                self._status.pid = pid
            if exit_code is not None:
                self._status.exit_code = exit_code
            if attempt:
                self._status.restart_attempt = attempt
            self._status.history.append(EngineEvent(
                at=self._clock(), state=state, detail=detail,
                pid=self._status.pid, exit_code=exit_code, attempt=attempt))
            snapshot = self._status
        if self._on_state_change is not None:
            try:
                self._on_state_change(snapshot)
            except Exception:  # noqa: BLE001 — reporting must not break the machine
                logger.exception("engine state-change callback failed")

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> tuple:
        """Start the engine and arm the watcher. Returns ``(supervisor, url)``."""
        self._stopping.clear()
        self._set_state(STARTING, detail="starting the managed engine")
        try:
            supervisor, url = self._starter(self._settings)
        except Exception as exc:  # noqa: BLE001 — the service always boots
            logger.warning("Managed engine could not be started (%s)", exc)
            self._set_state(STOPPED, detail=f"the managed engine raised: {exc}")
            return None, None
        if supervisor is None:
            self._set_state(STOPPED, detail="the managed engine did not start; "
                                            "see the service log for the refusal")
            return None, None

        self._supervisor = supervisor
        self._url = url
        proc = getattr(supervisor, "proc", None)
        # A reattached engine has no child handle, so its pid comes from the
        # manifest — the same record that vouched for it in the first place.
        pid = getattr(proc, "pid", None) or self._manifest_pid()
        with self._lock:
            self._status.pid = pid
            self._status.started_at = self._clock()
            self._status.exit_code = None
            self._status.died_at = None
            self._status.log_path = getattr(supervisor, "log_path", None)
            self._status.log_error = getattr(supervisor, "log_error", "") or ""
        self._set_state(READY, detail=f"managed engine ready on {url}")
        self._arm()
        return supervisor, url

    def _arm(self) -> None:
        """Watch the engine, by whichever means this process actually has.

        A spawned child is waited on. A REATTACHED engine has no child handle —
        we did not start it — so it is polled by pid instead. Leaving that case
        unwatched would preserve the v3.2.0 hole for exactly the path a service
        restart takes, and "we cannot wait on it" is not a reason to stop
        watching, only a reason to watch differently.
        """
        proc = getattr(self._supervisor, "proc", None)
        target = self._watch if proc is not None else self._poll_reattached
        if proc is None:
            with self._lock:
                pid = self._status.pid
            if not pid:
                logger.warning("Managed engine: reattached, but no pid is "
                               "recorded; it cannot be watched this run")
                return
            logger.info("Managed engine: reattached to pid %s; watching by pid "
                        "(no child handle to wait on)", pid)
        self._watcher = threading.Thread(
            target=target, name="qdrant-engine-watch", daemon=True)
        self._watcher.start()

    def _poll_reattached(self) -> None:
        """Watch an engine we did not spawn. Coarser, and far better than blind."""
        from ragtools.service.engine_ownership import process_alive

        with self._lock:
            pid = self._status.pid
        while not self._stopping.wait(self._poll_interval):
            if not pid or process_alive(pid):
                continue
            if self._stopping.is_set():
                return
            # No exit code is available for a process we did not spawn — say so
            # rather than inventing one.
            if not self._handle_death(None):
                return
            proc = getattr(self._supervisor, "proc", None)
            if proc is not None:
                self._watch()      # we own the replacement; wait on it properly
                return
            with self._lock:
                pid = self._status.pid

    def _watch(self) -> None:
        """Wait for the child, then decide whether that was expected."""
        while not self._stopping.is_set():
            proc = getattr(self._supervisor, "proc", None)
            if proc is None:
                return
            try:
                code = proc.wait()
            except Exception as exc:  # noqa: BLE001 — a wait we cannot do is not a crash
                logger.warning("Managed engine: could not wait on the child (%s); "
                               "stopping the watcher", exc)
                return

            if self._stopping.is_set():
                return                      # we asked for this

            if not self._handle_death(code):
                return

    def _handle_death(self, code) -> bool:
        """Record an unexpected exit and restart if the budget allows.

        Returns True when a restart succeeded and watching should continue.
        """
        with self._lock:
            pid = self._status.pid
            self._status.died_at = self._clock()
            uptime = self._status.uptime
        # No exit code exists for a process we did not spawn. Say that, rather
        # than printing "exit code None" as though one had been observed.
        how = (f"exit code {code}" if code is not None
               else "no exit code — this engine was reattached, not spawned by us")
        detail = f"the managed engine exited unexpectedly (pid {pid}, {how})"
        logger.error(
            "Managed Qdrant DIED: pid=%s exit_code=%s uptime=%.0fs. %s. Storage "
            "is unavailable until it is restarted.",
            pid, code, uptime or 0.0,
            f"Its own output is in {self._status.log_path}"
            if self._status.log_path else
            "No engine log was available for this run")
        self._set_state(CRASHED, detail=detail, exit_code=code)
        self._invalidate_manifest()
        _log_activity("error", "storage", detail)

        for attempt in range(1, self._max_restarts + 1):
            if self._stopping.is_set():
                return False
            delay = self._backoff[min(attempt, len(self._backoff)) - 1]
            self._set_state(
                RESTARTING,
                detail=f"restarting the managed engine in {delay:.0f}s "
                       f"(attempt {attempt} of {self._max_restarts})",
                attempt=attempt)
            self._sleep(delay)
            if self._stopping.is_set():
                return False

            # GUARDED. `plan_managed_startup` can raise (a configured binary
            # that has gone missing, an unreadable data dir), and an exception
            # escaping here would kill the watcher thread — silently, because
            # under the windowed launcher there is nowhere for a thread
            # traceback to go. That would restore the exact v3.2.0 condition
            # this class exists to end: an engine nobody is watching.
            try:
                supervisor, url = self._starter(self._settings)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Managed engine restart attempt %d raised: %s",
                               attempt, exc)
                supervisor, url = None, None
            if supervisor is not None:
                self._supervisor = supervisor
                self._url = url
                proc = getattr(supervisor, "proc", None)
                with self._lock:
                    self._status.pid = getattr(proc, "pid", None)
                    self._status.started_at = self._clock()
                    self._status.exit_code = None
                    self._status.log_path = getattr(supervisor, "log_path", None)
                self._set_state(
                    READY, detail=f"managed engine restarted on {url} after "
                                  f"{attempt} attempt(s)", attempt=attempt)
                _log_activity("success", "storage",
                              f"Managed engine restarted (attempt {attempt})")
                return proc is not None

            logger.warning("Managed engine restart attempt %d of %d failed",
                           attempt, self._max_restarts)

        exhausted = (f"the managed engine could not be restarted after "
                     f"{self._max_restarts} attempts; storage stays unavailable "
                     f"until the service is restarted")
        self._set_state(RESTART_EXHAUSTED, detail=exhausted,
                        attempt=self._max_restarts)
        logger.error("Managed Qdrant: %s", exhausted)
        _log_activity("error", "storage", exhausted)
        return False

    def _manifest_pid(self):
        """The pid our own record vouches for, when we have no child handle."""
        try:
            from ragtools.service.engine_ownership import read_manifest

            claim = read_manifest(self._settings)
            return claim.pid if claim else None
        except Exception:  # noqa: BLE001
            return None

    def _invalidate_manifest(self) -> None:
        """A dead engine must stop being vouched for, immediately."""
        try:
            from ragtools.service import engine_ownership

            engine_ownership.clear_manifest(self._settings)
            logger.info("Managed engine: ownership manifest invalidated "
                        "(the recorded engine is gone)")
        except Exception:  # noqa: BLE001
            logger.exception("could not invalidate the engine manifest")

    def request_stop(self) -> str:
        """Stop the engine deliberately. Idempotent.

        The flag is set BEFORE the stop so the waiter never reads a deliberate
        exit as a crash and starts restarting during shutdown.
        """
        self._stopping.set()
        self._set_state(STOPPING, detail="stopping the managed engine")
        outcome = ""
        try:
            outcome = self._stopper(self._settings, self._supervisor)
        except Exception as exc:  # noqa: BLE001
            outcome = f"stop failed: {exc}"
            logger.exception("managed engine stop failed")
        self._supervisor = None
        self._set_state(STOPPED, detail=outcome or "stopped")
        return outcome


# --- default wiring -------------------------------------------------------


def _default_starter(settings):
    from ragtools.service.managed_qdrant import start_managed_qdrant

    return start_managed_qdrant(settings)


def _default_stopper(settings, supervisor) -> str:
    from ragtools.service.engine_ownership import read_manifest, release

    return release(settings, read_manifest(settings),
                   proc=getattr(supervisor, "proc", None))


def _log_activity(level: str, source: str, message: str) -> None:
    try:
        from ragtools.service.activity import log_activity

        log_activity(level, source, message)
    except Exception:  # noqa: BLE001 — never let reporting break the machine
        pass
