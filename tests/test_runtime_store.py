"""Phase 2 (W2) — durable job + event store (`runtime.db`).

The gap the audit confirmed is absent in BOTH trees: no job identity, no
progress, no cancellation, no restart recovery, no idempotency, no durable
events. Long work runs in detached `threading.Timer` threads whose only trace is
a 500-entry in-memory ring that is lost on restart.

Why a NEW store rather than `index_state.db`: `owner.rebuild()` DELETES that
file, which would erase the job record of the rebuild that was running.

Plan: docs/planning/RAG_MODERNIZATION_MASTER_PLAN.md  (Phase 2 -> G2, D1)
"""

import pytest

from ragtools.runtime_store import (
    SCHEMA_VERSION,
    JobState,
    RuntimeStore,
)


@pytest.fixture
def store(tmp_path):
    s = RuntimeStore(str(tmp_path / "runtime.db"), instance_id="inst-1")
    yield s
    s.close()


# --- store hygiene -------------------------------------------------------


def test_schema_is_versioned_and_wal(store):
    """`index_state.py` is the only store in the codebase with a real migration
    ladder — the new store adopts it from the start."""
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_survives_reopen(tmp_path):
    p = str(tmp_path / "runtime.db")
    s1 = RuntimeStore(p, instance_id="a")
    job = s1.submit("index", {"project": "x"})
    s1.append_event("test.event", "service", {"n": 1})
    s1.close()

    s2 = RuntimeStore(p, instance_id="b")
    assert s2.get_job(job.id) is not None
    assert len(s2.events_after(0)) == 2   # submit emits an event + our explicit one
    s2.close()


# --- job lifecycle -------------------------------------------------------


def test_submit_creates_a_queued_job_with_identity(store):
    job = store.submit("index", {"project": "proj-a"})
    assert job.id
    assert job.kind == "index"
    assert job.state == JobState.QUEUED
    assert job.scope["project"] == "proj-a"


def test_claim_moves_queued_to_running(store):
    job = store.submit("index", {})
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == job.id
    assert store.get_job(job.id).state == JobState.RUNNING
    assert store.claim_next() is None      # nothing left to claim


def test_progress_is_recorded(store):
    job = store.submit("index", {})
    store.claim_next()
    store.update_progress(job.id, done=30, total=120, phase="embed")
    j = store.get_job(job.id)
    assert (j.progress_done, j.progress_total, j.phase) == (30, 120, "embed")


def test_success_records_result_and_finishes(store):
    job = store.submit("index", {})
    store.claim_next()
    store.finish(job.id, JobState.SUCCEEDED, result={"files": 10, "chunks": 42})
    j = store.get_job(job.id)
    assert j.state == JobState.SUCCEEDED
    assert j.result["chunks"] == 42
    assert j.finished_at


def test_failure_records_the_error(store):
    job = store.submit("rebuild", {})
    store.claim_next()
    store.finish(job.id, JobState.FAILED, error="qdrant unavailable")
    j = store.get_job(job.id)
    assert j.state == JobState.FAILED and "qdrant" in j.error


def test_cancellation_is_cooperative(store):
    job = store.submit("index", {})
    store.claim_next()
    assert store.is_cancel_requested(job.id) is False
    store.request_cancel(job.id)
    assert store.is_cancel_requested(job.id) is True
    store.finish(job.id, JobState.CANCELLED)
    assert store.get_job(job.id).state == JobState.CANCELLED


def test_destructive_jobs_record_verification(store):
    """A purge must not be reported as success until its effect is verified —
    today `delete_project_data` swallows the Qdrant failure and logs success."""
    job = store.submit("purge", {"project": "p"})
    store.claim_next()
    store.finish(job.id, JobState.SUCCEEDED, result={"points_before": 100, "points_after": 0},
                 verified=True)
    assert store.get_job(job.id).verified is True


# --- idempotency ---------------------------------------------------------


def test_idempotency_key_returns_the_same_job(store):
    a = store.submit("index", {"project": "p"}, idempotency_key="k1")
    b = store.submit("index", {"project": "p"}, idempotency_key="k1")
    assert a.id == b.id
    assert len(store.list_jobs()) == 1


def test_idempotency_key_is_reusable_after_completion(store):
    a = store.submit("index", {}, idempotency_key="k1")
    store.claim_next()
    store.finish(a.id, JobState.SUCCEEDED)
    b = store.submit("index", {}, idempotency_key="k1")
    assert b.id != a.id, "a finished job must not block a later identical request"


# --- restart recovery ----------------------------------------------------


def test_jobs_from_a_dead_instance_become_interrupted(tmp_path):
    p = str(tmp_path / "runtime.db")
    s1 = RuntimeStore(p, instance_id="old-instance")
    job = s1.submit("index", {})
    s1.claim_next()                     # running, owned by old-instance
    s1.close()

    s2 = RuntimeStore(p, instance_id="new-instance")
    recovered = s2.recover_interrupted()
    assert job.id in [j.id for j in recovered]
    assert s2.get_job(job.id).state == JobState.INTERRUPTED
    s2.close()


def test_recovery_does_not_touch_terminal_jobs(tmp_path):
    p = str(tmp_path / "runtime.db")
    s1 = RuntimeStore(p, instance_id="old")
    done = s1.submit("index", {})
    s1.claim_next()
    s1.finish(done.id, JobState.SUCCEEDED)
    s1.close()
    s2 = RuntimeStore(p, instance_id="new")
    s2.recover_interrupted()
    assert s2.get_job(done.id).state == JobState.SUCCEEDED
    s2.close()


def test_active_jobs_query_for_ui_reconciliation(store):
    a = store.submit("index", {})
    store.claim_next()
    b = store.submit("rebuild", {})
    store.finish(a.id, JobState.SUCCEEDED)
    active = [j.id for j in store.active_jobs()]
    assert b.id in active and a.id not in active


# --- events: the SSE backbone -------------------------------------------


def test_events_have_a_monotonic_cursor(store):
    store.append_event("a", "service", {})
    store.append_event("b", "service", {})
    evs = store.events_after(0)
    ids = [e.id for e in evs]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


def test_events_after_returns_only_newer(store):
    store.append_event("first", "service", {})
    cursor = store.latest_event_id()
    store.append_event("second", "service", {})
    new = store.events_after(cursor)
    assert [e.type for e in new] == ["second"]


def test_event_payload_roundtrips(store):
    store.append_event("job.progress", "indexer", {"done": 5, "total": 9})
    e = store.events_after(0)[-1]
    assert e.payload == {"done": 5, "total": 9} and e.source == "indexer"


def test_job_transitions_emit_events(store):
    """The UI must be able to learn about a job without polling it."""
    job = store.submit("index", {})
    store.claim_next()
    store.update_progress(job.id, done=1, total=2, phase="embed")
    store.finish(job.id, JobState.SUCCEEDED)
    types = [e.type for e in store.events_after(0)]
    assert "job.submitted" in types
    assert "job.started" in types
    assert "job.progress" in types
    assert "job.completed" in types


def test_events_can_be_pruned(store):
    for i in range(50):
        store.append_event("noise", "service", {"i": i})
    store.prune_events(keep=10)
    assert len(store.events_after(0)) == 10


def test_cancelling_a_queued_job_is_honoured_at_claim_time(store):
    """Found in live testing: a job cancelled while QUEUED had its flag set, but
    the worker only checked cancellation inside the handler — so the job would
    later be claimed and run to completion anyway."""
    job = store.submit("index", {})
    store.request_cancel(job.id)          # cancelled before it ever ran
    assert store.claim_next() is None, "a cancelled queued job must not be claimed"
    assert store.get_job(job.id).state == JobState.CANCELLED


def test_claim_skips_cancelled_and_takes_the_next_runnable(store):
    dead = store.submit("index", {"n": 1})
    live = store.submit("index", {"n": 2})
    store.request_cancel(dead.id)
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == live.id
    assert store.get_job(dead.id).state == JobState.CANCELLED
