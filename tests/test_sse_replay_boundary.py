"""A fresh page load must not replay history as if it just happened.

The event store is durable on purpose: a client that drops reconnects with
``Last-Event-ID`` and misses nothing. But the stream's cursor defaulted to 0
when no header was present, so a FIRST connection replayed the entire history —
and `base.html` turns `job.completed{state:"failed"}` into a toast. Loading the
dashboard therefore raised long-resolved failures as if they were live. Seen in
the browser: a `ProgrammingError` from a job that had failed, been diagnosed and
been fixed an hour earlier, popping up on every page load.

The rule: replay is for RESUMPTION (Last-Event-ID) or an explicit `?after=`.
A first connection starts at now.

Plan: docs/planning/RAG_COLLECTION_ARCHITECTURE_IMPLEMENTATION_PLAN.md (W7, W10)
"""

import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ragtools.config import Settings
from ragtools.service import app as app_module
from ragtools.service.app import create_app, get_runtime
from ragtools.service.owner import QdrantOwner


@pytest.fixture
def client():
    with tempfile.TemporaryDirectory() as td:
        settings = Settings(
            content_root=td,
            qdrant_path=str(Path(td) / "q"),
            state_db=str(Path(td) / "s.db"),
            data_dir=str(Path(td) / "d"),
        )
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        app_module._owner = owner
        app_module._settings = settings
        try:
            with TestClient(create_app()) as c:
                yield c
        finally:
            app_module._owner = None
            app_module._settings = None
            owner.close()


def _seed_history(n=5):
    """Write events that a later connection must NOT be shown."""
    runtime = get_runtime()
    for i in range(n):
        runtime.append_event("job.completed", "jobs",
                             {"kind": "index", "state": "failed",
                              "error": f"historical failure {i}"})
    return runtime.latest_event_id()


def _stream(client, **params):
    params.setdefault("once", "true")
    params.setdefault("max_seconds", "2")
    return client.get("/events", params=params).text


def test_a_fresh_connection_does_not_replay_history(client):
    _seed_history()
    body = _stream(client)
    assert "historical failure" not in body, (
        "a first connection replayed the durable history — old failures surface "
        "as live toasts on every page load"
    )


def test_a_reconnect_with_last_event_id_does_replay(client):
    """Resumption is the whole point of a durable log — it must still work."""
    latest = _seed_history()
    runtime = get_runtime()
    runtime.append_event("job.completed", "jobs",
                         {"kind": "index", "state": "failed", "error": "after cursor"})

    body = client.get("/events", params={"once": "true", "max_seconds": "2"},
                      headers={"Last-Event-ID": str(latest)}).text
    assert "after cursor" in body, "a reconnect did not resume from its cursor"
    assert "historical failure" not in body, "resumed too far back"


def test_an_explicit_after_still_replays(client):
    """Tests and pollers that ask for history get it."""
    _seed_history()
    body = _stream(client, after="0")
    assert "historical failure" in body


def test_events_emitted_after_connecting_are_delivered(client):
    """Starting at 'now' must not mean starting at 'never'."""
    _seed_history()
    runtime = get_runtime()
    cursor = runtime.latest_event_id()
    runtime.append_event("job.completed", "jobs",
                         {"kind": "index", "state": "succeeded", "note": "brand new"})

    body = _stream(client, after=str(cursor))
    assert "brand new" in body


def test_the_api_events_endpoint_keeps_its_explicit_cursor(client):
    """/api/events is a polling API — `after=0` means 'from the beginning'."""
    _seed_history()
    body = client.get("/api/events", params={"after": 0}).json()
    assert any("historical failure" in str(e) for e in body["events"])
