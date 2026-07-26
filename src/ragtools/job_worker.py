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

    # -- lifecycle ------------------------------------------------------

    def register(self, kind: str, handler: Callable) -> None:
        self._handlers[kind] = handler

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="rag-job-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Idempotent. Does not abort a running handler — it asks the loop to
        exit after the current job."""
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
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
            self._store.finish(job.id, JobState.FAILED,
                               error=f"no handler registered for job kind {job.kind!r}")
            return

        ctx = JobContext(self._store, job.id)
        try:
            result = handler(job, ctx)
            if not isinstance(result, dict):
                result = {"result": result} if result is not None else {}
            # Throttling may have dropped the last tick; the terminal progress
            # value must always be accurate.
            ctx.finalize_progress()
            self._store.finish(job.id, JobState.SUCCEEDED, result=result,
                               verified=ctx.verified)
        except Cancelled:
            self._store.finish(job.id, JobState.CANCELLED,
                               result={"note": "cancelled by request"})
        except Exception as exc:            # noqa: BLE001 — must never lose the job
            logger.exception("job %s (%s) failed", job.id, job.kind)
            self._store.finish(job.id, JobState.FAILED, error=f"{type(exc).__name__}: {exc}")
