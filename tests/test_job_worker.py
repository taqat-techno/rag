"""Phase 2 (W2) — the background job worker.

Replaces the `threading.Timer(3.0, ...)` fire-and-forget pattern (and the fully
synchronous `/api/index`) with a supervised worker that owns execution, reports
progress, honours cancellation, and always drives the job to a terminal state —
even when the handler raises.

Concurrency is deliberately 1: indexing contends on the encoder and the vector
store, so serial execution keeps progress meaningful and avoids self-contention.

Plan: docs/planning/RAG_MODERNIZATION_MASTER_PLAN.md  (Phase 2 -> G2, D2/D3)
"""

import threading
import time

import pytest

from ragtools.runtime_store import JobState, RuntimeStore
from ragtools.job_worker import JobWorker


@pytest.fixture
def store(tmp_path):
    s = RuntimeStore(str(tmp_path / "runtime.db"), instance_id="test")
    yield s
    s.close()


def _wait_for(store, job_id, states, timeout=30.0):
    """Wait for a job to reach one of ``states``, or fail saying exactly that.

    Two defects, both paid for on a release candidate:

    The old version **returned the job anyway** on timeout, in whatever state it
    happened to be in. The caller then asserted on that state, so a slow runner
    surfaced as ``assert 'running' == 'succeeded'`` — a scheduling failure
    wearing the costume of a state-machine bug. It reads like a product defect
    and is not one, and diagnosing it cost a full CI cycle.

    And 5 seconds was too tight. The throttling test performs 200 progress
    writes against SQLite: comfortable locally, not on a contended shared
    runner. A timeout generous enough never to fire spuriously costs nothing
    when the code is correct, because the wait returns the moment the state is
    reached — only a genuine hang pays the full 30s.
    """
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = store.get_job(job_id)
        if last and last.state in states:
            return last
        time.sleep(0.02)
    raise AssertionError(
        f"job {job_id} did not reach {states} within {timeout}s — last observed "
        f"state {getattr(last, 'state', None)!r}. This is a TIMEOUT, not a "
        "wrong-state bug."
    )


def test_worker_runs_a_job_to_success(store):
    def handler(job, ctx):
        return {"files": 3}

    w = JobWorker(store, {"index": handler}, poll_interval=0.01)
    w.start()
    try:
        job = store.submit("index", {})
        done = _wait_for(store, job.id, JobState.TERMINAL)
        assert done.state == JobState.SUCCEEDED
        assert done.result == {"files": 3}
    finally:
        w.stop()


def test_handler_exception_becomes_a_failed_job_not_a_lost_thread(store):
    """The old detached timers swallowed failures into a log line."""
    def handler(job, ctx):
        raise RuntimeError("qdrant exploded")

    w = JobWorker(store, {"index": handler}, poll_interval=0.01)
    w.start()
    try:
        job = store.submit("index", {})
        done = _wait_for(store, job.id, JobState.TERMINAL)
        assert done.state == JobState.FAILED
        assert "exploded" in done.error
    finally:
        w.stop()


def test_unknown_kind_fails_cleanly(store):
    w = JobWorker(store, {}, poll_interval=0.01)
    w.start()
    try:
        job = store.submit("nonexistent", {})
        done = _wait_for(store, job.id, JobState.TERMINAL)
        assert done.state == JobState.FAILED
        assert "handler" in done.error.lower()
    finally:
        w.stop()


def test_progress_is_reported_and_throttled(store):
    """A 37k-file index must not write a row per file."""
    def handler(job, ctx):
        for i in range(200):
            ctx.progress(done=i + 1, total=200, phase="embed")
        return {"n": 200}

    w = JobWorker(store, {"index": handler}, poll_interval=0.01)
    w.start()
    try:
        job = store.submit("index", {})
        done = _wait_for(store, job.id, JobState.TERMINAL)
        assert done.state == JobState.SUCCEEDED
        # Final progress is always flushed, regardless of throttling.
        assert done.progress_done == 200
        progress_events = [e for e in store.events_after(0) if e.type == "job.progress"]
        assert len(progress_events) < 50, "progress was not throttled"
    finally:
        w.stop()


def test_cancellation_stops_the_handler_cooperatively(store):
    started = threading.Event()

    def handler(job, ctx):
        started.set()
        for i in range(10_000):
            if ctx.should_cancel():
                raise ctx.Cancelled()
            time.sleep(0.001)
        return {}

    w = JobWorker(store, {"index": handler}, poll_interval=0.01)
    w.start()
    try:
        job = store.submit("index", {})
        assert started.wait(3.0)
        store.request_cancel(job.id)
        done = _wait_for(store, job.id, JobState.TERMINAL)
        assert done.state == JobState.CANCELLED
    finally:
        w.stop()


def test_jobs_run_one_at_a_time(store):
    """Serial by design — concurrent indexing would contend on the encoder."""
    concurrent = []
    lock = threading.Lock()
    live = {"n": 0}

    def handler(job, ctx):
        with lock:
            live["n"] += 1
            concurrent.append(live["n"])
        time.sleep(0.05)
        with lock:
            live["n"] -= 1
        return {}

    w = JobWorker(store, {"index": handler}, poll_interval=0.01)
    w.start()
    try:
        ids = [store.submit("index", {"i": i}).id for i in range(4)]
        for jid in ids:
            _wait_for(store, jid, JobState.TERMINAL, timeout=8.0)
        assert max(concurrent) == 1
    finally:
        w.stop()


def test_destructive_handler_can_record_verification(store):
    def handler(job, ctx):
        ctx.verified = True
        return {"points_before": 10, "points_after": 0}

    w = JobWorker(store, {"purge": handler}, poll_interval=0.01)
    w.start()
    try:
        job = store.submit("purge", {"project": "p"})
        done = _wait_for(store, job.id, JobState.TERMINAL)
        assert done.state == JobState.SUCCEEDED
        assert done.verified is True
    finally:
        w.stop()


def test_stop_is_idempotent_and_joins(store):
    w = JobWorker(store, {}, poll_interval=0.01)
    w.start()
    w.stop()
    w.stop()
    assert not w.is_alive()


# --- shutdown while a job is running ----------------------------------------
#
# `stop()` set an Event the loop only reads BETWEEN jobs, so a job already
# inside a handler never heard it; the join then simply gave up after its
# timeout and the caller (`app.stop_runtime`) closed the runtime store anyway.
# On a loaded CI runner that is what happened, and the running job died on
# `sqlite3.ProgrammingError: Cannot operate on a closed database` — twice, the
# second time from the handler recording the first, which escaped the thread.


def test_stopping_the_worker_ends_the_running_job_instead_of_abandoning_it(store):
    """The drain, and it is a drain because the outcome gets WRITTEN DOWN.

    The job reaching a terminal state in the store is the proof that it ended
    while the store was still open — which is the whole property, since the
    caller closes the store the moment `stop()` returns.
    """
    inside = threading.Event()

    def handler(job, ctx):
        inside.set()
        for _ in range(20_000):        # ~100s if nothing interrupts it
            ctx.check_cancel()
            time.sleep(0.005)
        return {"ran_to_completion": True}

    w = JobWorker(store, {"index": handler}, poll_interval=0.01)
    w.start()
    job = store.submit("index", {})
    assert inside.wait(10), "the handler never started"

    began = time.time()
    w.stop(timeout=30.0)
    elapsed = time.time() - began

    assert elapsed < 10.0, (
        f"stop() took {elapsed:.1f}s — it waited out the join instead of "
        "asking the running job to stop")
    done = store.get_job(job.id)
    assert done.state == JobState.INTERRUPTED, (
        f"the job was left in {done.state!r} when the service shut down under "
        "it; nothing recorded that it had ended")
    assert "shut down" in (done.error or ""), done.error


def test_a_user_cancel_during_a_shutdown_is_still_reported_as_cancelled(store):
    """`interrupted` and `cancelled` mean different things to the person
    reading the job list, so the worker must not collapse them."""
    inside = threading.Event()

    def handler(job, ctx):
        inside.set()
        for _ in range(20_000):
            ctx.check_cancel()
            time.sleep(0.005)
        return {}

    w = JobWorker(store, {"index": handler}, poll_interval=0.01)
    w.start()
    try:
        job = store.submit("index", {})
        assert inside.wait(10)
        store.request_cancel(job.id)
        done = _wait_for(store, job.id, JobState.TERMINAL)
        assert done.state == JobState.CANCELLED
    finally:
        w.stop()


def test_a_job_that_outlives_the_join_does_not_kill_the_worker_thread(tmp_path):
    """The CI failure itself: a handler that cannot be interrupted in time.

    Cancellation makes the ordinary case correct; it cannot make it certain,
    because a handler is free to sit in a long uninterruptible section. So the
    store has to survive being closed under one — and the thread has to survive
    the store being closed.

    The assertion is that NOTHING escaped the thread. An exception here kills
    the worker and hands its traceback every frame beneath it, which is how a
    leaked `IndexState` outlived the test that opened it and pinned `state.db`.
    """
    store = RuntimeStore(str(tmp_path / "runtime.db"), instance_id="test")
    inside = threading.Event()
    release = threading.Event()
    reached_the_end = threading.Event()

    def handler(job, ctx):
        inside.set()
        release.wait(30)
        # Every store call the real index handler makes on its next tick.
        ctx.check_cancel()
        ctx.progress(done=1, total=1, phase="late")
        reached_the_end.set()
        return {"late": True}

    escaped = []
    previous_hook = threading.excepthook
    threading.excepthook = escaped.append
    try:
        w = JobWorker(store, {"index": handler}, poll_interval=0.01)
        w.start()
        thread = w._thread                      # `stop` drops the reference
        store.submit("index", {})
        assert inside.wait(10), "the handler never started"

        w.stop(timeout=0.1)                     # the join gives up ...
        store.close()                           # ... and the store closes anyway
        release.set()
        thread.join(timeout=20)
    finally:
        threading.excepthook = previous_hook
        store.close()

    assert not thread.is_alive(), "the worker thread never finished"
    assert not escaped, (
        "an exception escaped the worker thread when the store closed under a "
        f"running job: {[getattr(a, 'exc_value', a) for a in escaped]}")
    # It unwound through cancellation rather than running to completion — a
    # closed store answers "yes, stop".
    assert not reached_the_end.is_set()
