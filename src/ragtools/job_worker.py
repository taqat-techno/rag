"""Background job worker (Phase 2 / W2).

Owns the execution of long operations. Replaces two patterns that made the UI
lie:

* ``threading.Timer(3.0, _run)`` fire-and-forget — no identity, no progress, and
  a failure that only ever reached an in-memory log line;
* fully synchronous ``/api/index`` — the HTTP request blocked for the whole
  index, so the result was lost if the tab closed.

**Concurrency is 1 by design.** Indexing contends on the shared encoder and the
vector store; running two at once would slow both and make progress meaningless.

The worker guarantees that a claimed job always reaches a terminal state — a
raising handler becomes ``failed`` with the message, never a silently dead
thread.

Plan: docs/planning/RAG_MODERNIZATION_MASTER_PLAN.md  (Phase 2 -> G2, D2/D3)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from ragtools.runtime_store import JobState, RuntimeStore

logger = logging.getLogger("ragtools.jobs")

#: Progress is flushed at most this often, or when it moves this fraction.
_MIN_INTERVAL_S = 0.5
_MIN_FRACTION = 0.05


class Cancelled(Exception):
    """Raised by a handler when it observes ``ctx.should_cancel()``."""


class JobContext:
    """What a handler is given: throttled progress, cancellation, verification.

    ``verified`` is set by destructive handlers to record that the effect was
    confirmed (e.g. the point count actually dropped) — the service must never
    report a destructive success it did not verify.
    """

    Cancelled = Cancelled

    def __init__(self, store: RuntimeStore, job_id: str, total: Optional[int] = None):
        self._store = store
        self._job_id = job_id
        self._last_write = 0.0
        self._last_done = -1
        self._total = total
        self._pending: Optional[tuple] = None
        self.verified: Optional[bool] = None

    def progress(self, done: int, total: Optional[int] = None,
                 phase: Optional[str] = None) -> None:
        """Record progress, throttled. Cheap to call in a tight loop."""
        if total is not None:
            self._total = total
        self._pending = (done, phase)
        now = time.monotonic()
        moved_enough = True
        if self._total:
            moved_enough = abs(done - self._last_done) >= max(1, int(self._total * _MIN_FRACTION))
        if (now - self._last_write) < _MIN_INTERVAL_S and not moved_enough:
            return
        self._last_write = now
        self._last_done = done
        self._pending = None
        self._store.update_progress(self._job_id, done=done, total=self._total, phase=phase)

    def finalize_progress(self) -> None:
        """Flush a throttled-away final tick so the terminal value is accurate."""
        if self._pending is None:
            return
        done, phase = self._pending
        self._pending = None
        self._last_done = done
        self._store.update_progress(self._job_id, done=done, total=self._total,
                                    phase=phase, emit=False)

    def flush_progress(self, done: int, total: Optional[int] = None,
                       phase: Optional[str] = None) -> None:
        """Unconditional write — used for the final value."""
        self._store.update_progress(self._job_id, done=done,
                                    total=total if total is not None else self._total,
                                    phase=phase)

    def should_cancel(self) -> bool:
        return self._store.is_cancel_requested(self._job_id)

    def check_cancel(self) -> None:
        """Raise :class:`Cancelled` if cancellation was requested."""
        if self.should_cancel():
            raise Cancelled()


class JobWorker:
    """Single-threaded job runner draining :class:`RuntimeStore`."""

    def __init__(self, store: RuntimeStore, handlers: dict, *,
                 poll_interval: float = 0.25):
        self._store = store
        self._handlers: dict = dict(handlers)
        self._poll = poll_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        #: The job the worker thread is inside RIGHT NOW, or None. Read by
        #: `stop` from another thread, so it is guarded.
        self._current: Optional[str] = None
        #: The job `stop` asked to end, so `_run` can tell "the service shut
        #: down under me" from "a user cancelled me" — different states, and
        #: the UI means different things by them.
        self._interrupted: Optional[str] = None
        self._state_lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------

    def register(self, kind: str, handler: Callable) -> None:
        self._handlers[kind] = handler

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        with self._state_lock:
            self._interrupted = None
        self._thread = threading.Thread(target=self._loop, name="rag-job-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Idempotent. Asks the loop to exit AND asks the running job to stop.

        It used to only do the first, which meant "stop" was a request the
        running job never heard: the loop checks ``_stop`` between jobs, so a
        job already in a handler ran on regardless, and the join simply gave up
        after ``timeout``. Its caller (:func:`ragtools.service.app.stop_runtime`)
        then closed the runtime store out from under it — a release build died
        exactly there.

        Requesting cancellation is what makes the drain real: a cooperative
        handler observes it at its next ``ctx.check_cancel()`` and unwinds
        through a path that finishes the job **while the store is still open**,
        so the outcome is recorded instead of lost. A handler that cannot be
        interrupted still cannot be, which is why the store also has to survive
        being closed under one — see :meth:`RuntimeStore.close`.

        A thread still alive after ``timeout`` is REPORTED, naming the job. It
        was silent, and silence is how "the store closed under a running job"
        stayed invisible until Windows refused to delete the file.
        """
        self._stop.set()
        with self._state_lock:
            job_id = self._current
            self._interrupted = job_id
        if job_id is not None:
            try:
                self._store.request_cancel(job_id)
            except Exception:  # noqa: BLE001 — teardown must not raise
                logger.exception("could not ask job %s to stop", job_id)
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
            if t.is_alive():
                with self._state_lock:
                    still = self._current
                logger.warning(
                    "job worker did not stop within %.1fs; job %s is still "
                    "running and its store is about to be closed", timeout, still)
        self._thread = None

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- loop -----------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._store.claim_next()
            except Exception:
                logger.exception("job claim failed")
                job = None
            if job is None:
                self._stop.wait(self._poll)
                continue
            self._run(job)

    def _run(self, job) -> None:
        handler = self._handlers.get(job.kind)
        if handler is None:
            self._finish(job, JobState.FAILED,
                         error=f"no handler registered for job kind {job.kind!r}")
            return

        with self._state_lock:
            self._current = job.id
        ctx = JobContext(self._store, job.id)
        try:
            result = handler(job, ctx)
            if not isinstance(result, dict):
                result = {"result": result} if result is not None else {}
            # Throttling may have dropped the last tick; the terminal progress
            # value must always be accurate.
            ctx.finalize_progress()
            self._finish(job, JobState.SUCCEEDED, result=result,
                         verified=ctx.verified)
        except Cancelled:
            with self._state_lock:
                by_shutdown = self._interrupted == job.id
            if by_shutdown:
                # NOT "cancelled": nobody asked for this job to stop, the
                # service went away underneath it. `interrupted` is the state
                # `recover_interrupted` already uses for exactly this, so the
                # UI does not have to learn a new word for it.
                self._finish(job, JobState.INTERRUPTED,
                             error="the service shut down while this job was running")
            else:
                self._finish(job, JobState.CANCELLED,
                             result={"note": "cancelled by request"})
        except Exception as exc:            # noqa: BLE001 — must never lose the job
            logger.exception("job %s (%s) failed", job.id, job.kind)
            self._finish(job, JobState.FAILED, error=f"{type(exc).__name__}: {exc}")
        finally:
            with self._state_lock:
                self._current = None

    def _finish(self, job, state: str, **fields) -> None:
        """Record the terminal state, and REPORT it if that is impossible.

        The store no longer raises when it has been closed, but it is not the
        only way recording can fail (a full disk, a corrupt file), and this
        runs on a daemon thread with nobody to catch anything. An exception
        escaping here kills the worker and hands its traceback every frame
        beneath it — which is how a leaked `IndexState` handle outlived the
        test that opened it and pinned `state.db` on Windows.

        Logging is not swallowing: the job, its kind, the state it reached and
        the error it was carrying all reach the log. What must not happen is
        the failure to *write down* an outcome becoming a second, louder
        failure that destroys the thread.
        """
        try:
            self._store.finish(job.id, state, **fields)
        except Exception:  # noqa: BLE001 — see docstring
            logger.exception(
                "job %s (%s) ended as %s but the outcome could not be "
                "recorded%s", job.id, job.kind, state,
                f": {fields['error']}" if fields.get("error") else "")
