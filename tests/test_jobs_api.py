"""Phase 2 (W2) — the job HTTP contract.

`202 Accepted` + `job_id` + `Location` replaces a request that used to block for
the entire index and lose its result if the caller went away. `?wait=true`
preserves the original synchronous shape so the CLI and existing tests keep
working.

Plan: docs/planning/RAG_MODERNIZATION_MASTER_PLAN.md  (Phase 2 -> G2, C-1)
"""

import tempfile
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ragtools.config import ProjectConfig, Settings
import ragtools.service.app as app_module
from ragtools.service.app import create_app
from ragtools.service.owner import QdrantOwner


@pytest.fixture
def client():
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            content_root=str(Path(tmp)),
            qdrant_path=str(Path(tmp) / "qdrant"),
            state_db=str(Path(tmp) / "state.db"),
            projects=[ProjectConfig(id="proj-a", path=str(Path(tmp)))],
        )
        app_module._owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        app_module._settings = settings
        try:
            with TestClient(create_app(), raise_server_exceptions=True) as tc:
                yield tc
        finally:
            app_module._owner = None
            app_module._settings = None


def _await_terminal(client, job_id, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["state"] in ("succeeded", "failed", "cancelled", "interrupted"):
            return j
        time.sleep(0.05)
    return client.get(f"/api/jobs/{job_id}").json()


# --- async submission ----------------------------------------------------


def test_index_returns_202_with_a_job_and_location(client):
    r = client.post("/api/index", json={"full": False})
    assert r.status_code == 202
    body = r.json()
    assert body["job_id"]
    assert body["kind"] == "index"
    assert body["state"] in ("queued", "running", "succeeded")
    assert r.headers["Location"] == f"/api/jobs/{body['job_id']}"


def test_submitted_job_reaches_a_terminal_state(client):
    job_id = client.post("/api/index", json={"full": True}).json()["job_id"]
    done = _await_terminal(client, job_id)
    assert done["state"] == "succeeded", done
    assert "result" in done


def test_wait_true_preserves_the_original_synchronous_shape(client):
    """CLI / existing-test compatibility must not be broken by the async default."""
    r = client.post("/api/index?wait=true", json={"full": False})
    assert r.status_code == 200
    assert "stats" in r.json()
    assert "job_id" not in r.json()


def test_idempotency_key_does_not_start_a_second_job(client):
    h = {"Idempotency-Key": "same-key"}
    a = client.post("/api/index", json={"full": False}, headers=h).json()
    b = client.post("/api/index", json={"full": False}, headers=h).json()
    assert a["job_id"] == b["job_id"]


# --- job roster ----------------------------------------------------------


def test_job_lookup_and_404(client):
    job_id = client.post("/api/index", json={}).json()["job_id"]
    assert client.get(f"/api/jobs/{job_id}").status_code == 200
    assert client.get("/api/jobs/nope").status_code == 404


def test_jobs_listing_and_active_filter(client):
    job_id = client.post("/api/index", json={}).json()["job_id"]
    _await_terminal(client, job_id)
    allj = client.get("/api/jobs").json()["jobs"]
    assert any(j["job_id"] == job_id for j in allj)
    active = client.get("/api/jobs?active=true").json()["jobs"]
    assert all(j["state"] in ("queued", "running") for j in active)


# --- cancellation --------------------------------------------------------


def test_cancelling_a_finished_job_is_409(client):
    job_id = client.post("/api/index", json={}).json()["job_id"]
    _await_terminal(client, job_id)
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 409


def test_cancelling_an_unknown_job_is_404(client):
    assert client.post("/api/jobs/nope/cancel").status_code == 404


# --- events feed ---------------------------------------------------------


def test_events_feed_advances_with_a_cursor(client):
    job_id = client.post("/api/index", json={}).json()["job_id"]
    _await_terminal(client, job_id)
    first = client.get("/api/events?after=0").json()
    assert first["events"], "job activity produced no events"
    types = [e["type"] for e in first["events"]]
    assert "job.submitted" in types and "job.completed" in types
    # Nothing new after the cursor.
    second = client.get(f"/api/events?after={first['cursor']}").json()
    assert second["events"] == []


def test_job_events_carry_the_job_id(client):
    job_id = client.post("/api/index", json={}).json()["job_id"]
    _await_terminal(client, job_id)
    evs = client.get("/api/events?after=0").json()["events"]
    assert any(e["payload"].get("job_id") == job_id for e in evs)


# --- durable activity write-through --------------------------------------


def test_existing_log_activity_calls_become_durable_events(client):
    """The ring buffer is 'lost on service restart'. Registering a sink makes
    every EXISTING call site durable with no call-site changes."""
    from ragtools.service.activity import log_activity

    log_activity("warning", "watcher", "a durable message", "with details")
    evs = client.get("/api/events?after=0").json()["events"]
    match = [e for e in evs if e["payload"].get("message") == "a durable message"]
    assert match, "log_activity did not reach the durable store"
    assert match[0]["type"] == "activity.warning"
    assert match[0]["source"] == "watcher"
    assert match[0]["payload"]["details"] == "with details"
