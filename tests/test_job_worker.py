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


def _wait_for(store, job_id, states, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        j = store.get_job(job_id)
        if j and j.state in states:
            return j
        time.sleep(0.02)
    return store.get_job(job_id)


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
