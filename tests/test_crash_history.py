"""Tests for the crash-history banner feature.

Covers:
  - list_unreviewed_crashes: empty, one marker, both markers, stale-aged-out
  - dismiss_crash_marker: renames file, is idempotent, returns False for unknown key
  - Fragment endpoint /ui/crash-banner: empty vs populated HTML
  - API endpoint /api/crash-history: JSON shape
  - API endpoint /api/crash-history/{key}/dismiss: success + 404
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from ragtools.config import Settings
from ragtools.service.crash_history import (
    _MAX_MARKER_AGE_SECONDS,
    dismiss_crash_marker,
    list_unreviewed_crashes,
)


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        qdrant_path=str(tmp_path / "data" / "qdrant"),
        state_db=str(tmp_path / "data" / "index_state.db"),
    )


def _write_marker(tmp_path: Path, name: str, payload: dict) -> Path:
    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / name
    path.write_text(json.dumps(payload))
    return path


# ---------------------------------------------------------------------------
# Pure-helper layer
# ---------------------------------------------------------------------------


def test_no_markers_returns_empty_list(tmp_path):
    s = _make_settings(tmp_path)
    assert list_unreviewed_crashes(s) == []


def test_service_crash_marker_is_surfaced(tmp_path):
    s = _make_settings(tmp_path)
    _write_marker(tmp_path, "last_crash.json", {
        "timestamp": "2026-04-18T10:15:00Z",
        "exception_type": "RuntimeError",
        "message": "boom",
        "traceback": "Traceback (most recent call last):\n  ...",
    })

    items = list_unreviewed_crashes(s)
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "service_crash"
    assert item["dismiss_key"] == "service_crash"
    assert item["exception_type"] == "RuntimeError"
    assert item["message"] == "boom"


def test_supervisor_gave_up_marker_is_surfaced(tmp_path):
    s = _make_settings(tmp_path)
    _write_marker(tmp_path, "supervisor_gave_up.json", {
        "timestamp": "2026-04-18T10:20:00Z",
        "reason": "Too many crashes in a row",
        "max_failures": 5,
        "window_seconds": 300,
    })

    items = list_unreviewed_crashes(s)
    assert len(items) == 1
    assert items[0]["kind"] == "supervisor_gave_up"
    assert items[0]["dismiss_key"] == "supervisor_gave_up"


def test_watcher_gave_up_marker_is_surfaced(tmp_path):
    s = _make_settings(tmp_path)
    _write_marker(tmp_path, "watcher_gave_up.json", {
        "timestamp": "2026-04-18T10:25:00Z",
        "retries": 5,
        "error": "OSError: watcher process died",
        "error_type": "OSError",
    })

    items = list_unreviewed_crashes(s)
    assert len(items) == 1
    assert items[0]["kind"] == "watcher_gave_up"
    assert items[0]["dismiss_key"] == "watcher_gave_up"
    assert items[0]["retries"] == 5


def test_both_markers_are_surfaced_newest_first(tmp_path):
    s = _make_settings(tmp_path)
    _write_marker(tmp_path, "last_crash.json", {
        "timestamp": "2026-04-18T10:15:00Z",
        "exception_type": "RuntimeError",
        "message": "earlier",
    })
    _write_marker(tmp_path, "supervisor_gave_up.json", {
        "timestamp": "2026-04-18T10:20:00Z",  # newer
        "reason": "later",
    })

    items = list_unreviewed_crashes(s)
    assert len(items) == 2
    assert items[0]["kind"] == "supervisor_gave_up"  # newest first
    assert items[1]["kind"] == "service_crash"


def test_reviewed_files_are_ignored(tmp_path):
    """After dismiss, the *.reviewed.json file must not re-appear in the list."""
    s = _make_settings(tmp_path)
    _write_marker(tmp_path, "last_crash.json", {
        "timestamp": "2026-04-18T10:15:00Z",
        "exception_type": "RuntimeError",
        "message": "boom",
    })

    assert len(list_unreviewed_crashes(s)) == 1
    dismiss_crash_marker(s, "service_crash")
    assert list_unreviewed_crashes(s) == []


def test_expired_marker_is_ignored(tmp_path):
    s = _make_settings(tmp_path)
    path = _write_marker(tmp_path, "last_crash.json", {
        "timestamp": "2026-01-01T00:00:00Z",
        "exception_type": "RuntimeError",
        "message": "ancient",
    })
    # Backdate the file mtime to older than the cutoff.
    old_time = time.time() - _MAX_MARKER_AGE_SECONDS - 3600
    os.utime(path, (old_time, old_time))

    assert list_unreviewed_crashes(s) == []


def test_malformed_marker_is_ignored(tmp_path):
    s = _make_settings(tmp_path)
    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "last_crash.json").write_text("{not valid json")

    # Must not raise — must simply skip the bad file
    items = list_unreviewed_crashes(s)
    assert items == []


def test_dismiss_renames_file(tmp_path):
    s = _make_settings(tmp_path)
    path = _write_marker(tmp_path, "last_crash.json", {
        "timestamp": "2026-04-18T10:15:00Z",
        "exception_type": "RuntimeError",
        "message": "boom",
    })
    assert path.exists()

    assert dismiss_crash_marker(s, "service_crash") is True
    assert not path.exists()
    assert (path.parent / "last_crash.reviewed.json").exists()


def test_dismiss_idempotent_when_already_reviewed(tmp_path):
    """Dismissing twice should not raise; the second returns False."""
    s = _make_settings(tmp_path)
    _write_marker(tmp_path, "last_crash.json", {"message": "x"})

    assert dismiss_crash_marker(s, "service_crash") is True
    assert dismiss_crash_marker(s, "service_crash") is False


def test_dismiss_unknown_key_returns_false(tmp_path):
    s = _make_settings(tmp_path)
    assert dismiss_crash_marker(s, "not_a_real_key") is False


# ---------------------------------------------------------------------------
# Service endpoints (JSON API + HTML fragment)
# ---------------------------------------------------------------------------


@pytest.fixture
def client_with_markers(tmp_path, monkeypatch):
    """Spin up a TestClient wired to a Settings pointing at tmp_path.

    Uses the service.app module-level state injection pattern established
    in test_service.py: pre-create the QdrantOwner and stuff it into
    app_module._owner / ._settings so the lifespan callback sees "already
    initialized" and yields without trying to load the real encoder.
    """
    from starlette.testclient import TestClient
    from ragtools.service.app import create_app
    from ragtools.service import app as app_module

    monkeypatch.setenv("RAG_CONFIG_PATH", str(tmp_path / "ragtools.toml"))

    settings = _make_settings(tmp_path)

    from unittest.mock import MagicMock
    fake_client = MagicMock()
    fake_client.get_collection.return_value = MagicMock(points_count=0)
    monkeypatch.setattr("ragtools.service.owner.ensure_collection", lambda *a, **k: None)
    monkeypatch.setattr("ragtools.service.owner.Encoder", lambda *a, **k: MagicMock(dimension=384))

    from ragtools.service.owner import QdrantOwner
    owner = QdrantOwner(settings=settings, client=fake_client)

    app_module._owner = owner
    app_module._settings = settings
    try:
        app = create_app()
        with TestClient(app) as client:
            yield client, settings
    finally:
        app_module._owner = None
        app_module._settings = None


def test_api_returns_empty_when_no_crashes(client_with_markers):
    client, _settings = client_with_markers
    r = client.get("/api/crash-history")
    assert r.status_code == 200
    assert r.json() == {"count": 0, "items": []}


def test_api_returns_marker_details(tmp_path, client_with_markers):
    client, settings = client_with_markers
    _write_marker(tmp_path, "last_crash.json", {
        "timestamp": "2026-04-18T10:15:00Z",
        "exception_type": "MemoryError",
        "message": "OOM while indexing",
        "traceback": "Traceback:\n  File ...",
    })

    r = client.get("/api/crash-history")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["kind"] == "service_crash"
    assert item["exception_type"] == "MemoryError"


def test_api_dismiss_endpoint_success(tmp_path, client_with_markers):
    client, settings = client_with_markers
    _write_marker(tmp_path, "last_crash.json", {
        "timestamp": "2026-04-18T10:15:00Z",
        "exception_type": "RuntimeError",
        "message": "boom",
    })

    r = client.post("/api/crash-history/service_crash/dismiss")
    assert r.status_code == 200
    assert r.json() == {"dismissed": "service_crash"}
    assert client.get("/api/crash-history").json()["count"] == 0


def test_api_dismiss_returns_404_for_missing_marker(client_with_markers):
    client, _settings = client_with_markers
    r = client.post("/api/crash-history/service_crash/dismiss")
    assert r.status_code == 404


def test_ui_fragment_empty_when_no_crashes(client_with_markers):
    client, _settings = client_with_markers
    r = client.get("/ui/crash-banner")
    assert r.status_code == 200
    assert r.text.strip() == ""  # empty fragment keeps the banner slot collapsed


def test_ui_fragment_renders_html_for_crash(tmp_path, client_with_markers):
    client, _settings = client_with_markers
    _write_marker(tmp_path, "last_crash.json", {
        "timestamp": "2026-04-18T10:15:00Z",
        "exception_type": "RuntimeError",
        "message": "spectacular boom",
        "traceback": "Traceback (most recent call last):\n  File...",
    })

    r = client.get("/ui/crash-banner")
    assert r.status_code == 200
    html = r.text
    assert "crash-banner" in html
    assert "spectacular boom" in html
    assert "RuntimeError" in html
    assert 'hx-post="/api/crash-history/service_crash/dismiss"' in html


def test_ui_fragment_renders_supervisor_gave_up(tmp_path, client_with_markers):
    client, _settings = client_with_markers
    _write_marker(tmp_path, "supervisor_gave_up.json", {
        "timestamp": "2026-04-18T10:20:00Z",
        "reason": "exceeded 5 failures in 300 seconds",
    })

    r = client.get("/ui/crash-banner")
    assert r.status_code == 200
    html = r.text
    assert "Supervisor stopped restarting" in html
    assert "exceeded 5 failures" in html
    assert 'hx-post="/api/crash-history/supervisor_gave_up/dismiss"' in html


def test_ui_fragment_renders_watcher_gave_up(tmp_path, client_with_markers):
    client, _settings = client_with_markers
    _write_marker(tmp_path, "watcher_gave_up.json", {
        "timestamp": "2026-04-18T10:25:00Z",
        "retries": 5,
        "error": "OSError: file watcher died permanently",
        "error_type": "OSError",
    })

    r = client.get("/ui/crash-banner")
    assert r.status_code == 200
    html = r.text
    assert "File watcher stopped" in html
    assert "5" in html
    assert "OSError" in html or "file watcher died permanently" in html
    assert 'hx-post="/api/crash-history/watcher_gave_up/dismiss"' in html
