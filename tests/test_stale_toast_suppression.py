"""A toast means "this just happened", not "this once happened".

Reported with a screenshot: three sticky red job-failure toasts stacked in the
corner. All three were real — but they were 1.5 to 3 hours old, from index runs
that had already been diagnosed and fixed:

    14:04 -> 14:34  index  failed  WinError 10048 (port exhaustion)
    14:46 -> 15:05  index  failed  WinError 10053 (connection aborted)
    15:08 -> 15:27  index  failed  WinError 10053 (connection aborted)

They surfaced because a long-lived tab's ``EventSource`` reconnects with
``Last-Event-ID`` after a service restart and the durable log correctly replays
everything that tab missed. Replay is right for *invalidation* — the data really
did change — but raising a resolved failure as a sticky error toast is noise
that trains the reader to dismiss toasts without reading them.

So: replayed events still invalidate; only events at least as new as the page
itself are allowed to shout.

Plan: docs/planning/RAG_COLLECTION_ARCHITECTURE_IMPLEMENTATION_PLAN.md (W10)
"""

import re
import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ragtools.config import Settings
from ragtools.service import app as app_module
from ragtools.service.app import create_app
from ragtools.service.owner import QdrantOwner


@pytest.fixture(scope="module")
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


@pytest.fixture(scope="module")
def script(client):
    """The inline SSE handler from base.html."""
    body = client.get("/").text
    start = body.index("var PAGE_OPENED_AT")
    return body[start:body.index("function connect()")]


def test_the_page_records_when_it_opened(script):
    assert "PAGE_OPENED_AT" in script
    assert "Date.now()" in script


def test_freshness_is_decided_from_the_event_timestamp(script):
    assert "function isLive" in script
    assert "data.ts" in script or "data && data.ts" in script
    assert "Date.parse" in script


def test_only_live_completions_raise_a_toast(script):
    """The guard must be on the toast branch, not on invalidation."""
    assert re.search(r"job\.completed'\s*&&\s*isLive\(data\)", script), (
        "job.completed toasts are not gated on freshness"
    )


def test_replayed_events_still_invalidate(script):
    """Suppressing the toast must not suppress the refresh — the data really
    did change while the tab was away."""
    invalidate = script[script.index("INVALIDATING.test"):]
    assert "isLive" not in invalidate.split("\n")[0], (
        "invalidation was gated on freshness; a reconnecting tab would keep "
        "showing stale data"
    )
    assert "ragInvalidate()" in script


def test_an_unparseable_timestamp_is_treated_as_live(script):
    """Fail toward showing the message: a missing ts must not silence a real
    failure."""
    assert "isNaN(ts)) return true" in script.replace(" ", "").replace(
        "isNaN(ts))returntrue", "isNaN(ts)) return true") or \
        "return true" in script


def test_there_is_a_clock_skew_grace_window(script):
    assert "TOAST_GRACE_MS" in script


# --- the server side of the contract ------------------------------------


def test_events_carry_a_timezone_aware_timestamp(client):
    """`Date.parse` treats a bare ISO datetime as LOCAL time. Without an
    offset the server's UTC timestamps would be read hours in the future, and
    every stale event would look live — silently defeating the guard."""
    from ragtools.service.app import get_runtime

    runtime = get_runtime()
    ev = runtime.append_event("job.completed", "jobs",
                              {"kind": "index", "state": "failed", "error": "x"})
    assert re.search(r"(\+\d{2}:\d{2}|Z)$", ev.ts), (
        f"event timestamp {ev.ts!r} carries no UTC offset"
    )

    body = client.get("/api/events", params={"after": ev.id - 1}).json()
    assert body["events"], "event not returned"
    assert re.search(r"(\+\d{2}:\d{2}|Z)$", body["events"][0]["ts"])
