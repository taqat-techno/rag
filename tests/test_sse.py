"""Phase 3 (W3) — the SSE event stream.

`Last-Event-ID` maps directly onto the durable event cursor, so a reconnecting
client replays exactly what it missed. This is why SSE was chosen over
WebSockets: the resume semantics already existed in the store.

The stream reads ONLY `runtime.db` — never the encoder or the vector store — so
it can never hold a lock a search or an index needs.

Plan: docs/planning/RAG_MODERNIZATION_MASTER_PLAN.md  (Phase 3 -> G3, D4)
"""

import tempfile
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


def _drain(client, url):
    """Read a `?once=true` stream to completion and return its raw text."""
    with client.stream("GET", url) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        return "".join(chunk for chunk in r.iter_text())


def test_stream_is_sse_and_replays_from_zero(client):
    from ragtools.service.app import get_runtime
    get_runtime().append_event("project.changed", "cli", {"project_id": "proj-a"})

    body = _drain(client, "/events?once=true&after=0")
    assert "event: project.changed" in body
    assert "proj-a" in body
    assert "id: " in body          # every frame carries the cursor


def test_last_event_id_header_resumes(client):
    """A reconnecting EventSource must not replay what it already saw."""
    rt = __import__("ragtools.service.app", fromlist=["get_runtime"]).get_runtime()
    rt.append_event("first", "service", {})
    cursor = rt.latest_event_id()
    rt.append_event("second", "service", {})

    with client.stream("GET", "/events?once=true",
                       headers={"Last-Event-ID": str(cursor)}) as r:
        body = "".join(r.iter_text())
    assert "event: second" in body
    assert "event: first" not in body


def test_query_cursor_also_works(client):
    rt = __import__("ragtools.service.app", fromlist=["get_runtime"]).get_runtime()
    rt.append_event("alpha", "service", {})
    cursor = rt.latest_event_id()
    rt.append_event("beta", "service", {})
    body = _drain(client, f"/events?once=true&after={cursor}")
    assert "event: beta" in body and "event: alpha" not in body


def test_stream_sends_a_heartbeat_comment(client):
    """Lets the client distinguish 'quiet' from 'dead'."""
    body = _drain(client, "/events?once=true&after=0")
    assert body.startswith(":") or "\n:" in body


def test_job_lifecycle_is_observable_on_the_stream(client):
    """The whole point: submit work, learn about it without polling the job."""
    import time

    job_id = client.post("/api/index", json={}).json()["job_id"]
    end = time.time() + 15
    while time.time() < end:
        if client.get(f"/api/jobs/{job_id}").json()["state"] in (
                "succeeded", "failed", "cancelled", "interrupted"):
            break
        time.sleep(0.05)

    body = _drain(client, "/events?once=true&after=0")
    assert "event: job.submitted" in body
    assert "event: job.completed" in body
    assert job_id in body
