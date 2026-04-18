"""Tests for the FastAPI service endpoints."""

import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ragtools.config import Settings
from ragtools.service.app import create_app
from ragtools.service import app as app_module
from ragtools.service.owner import QdrantOwner


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def test_client():
    """Create a test FastAPI client with in-memory Qdrant and indexed fixtures."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_db = str(Path(tmpdir) / "test_state.db")
        from ragtools.config import ProjectConfig
        settings = Settings(
            content_root=str(FIXTURES),
            state_db=state_db,
            projects=[
                ProjectConfig(id="project_a", path=str(FIXTURES / "project_a")),
                ProjectConfig(id="project_b", path=str(FIXTURES / "project_b")),
            ],
        )
        client = Settings.get_memory_client()
        owner = QdrantOwner(settings=settings, client=client)
        owner.run_full_index()

        # Inject owner into app module
        app_module._owner = owner
        app_module._settings = settings

        app = create_app()
        with TestClient(app, raise_server_exceptions=True) as tc:
            yield tc

        app_module._owner = None
        app_module._settings = None


# --- Health ---

def test_health(test_client):
    r = test_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


# --- Search ---

def test_search(test_client):
    r = test_client.get("/api/search", params={"query": "backend architecture"})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] > 0
    assert len(data["results"]) > 0


def test_search_with_project(test_client):
    r = test_client.get("/api/search", params={"query": "README", "project": "project_a"})
    assert r.status_code == 200
    for result in r.json()["results"]:
        assert result["project_id"] == "project_a"


def test_search_no_results(test_client):
    r = test_client.get("/api/search", params={"query": "xyznonexistent12345"})
    assert r.status_code == 200
    assert r.json()["count"] == 0


# --- Index ---

def test_index_incremental(test_client):
    r = test_client.post("/api/index", json={"full": False})
    assert r.status_code == 200
    stats = r.json()["stats"]
    assert "skipped" in stats


def test_index_full(test_client):
    r = test_client.post("/api/index", json={"full": True})
    assert r.status_code == 200
    stats = r.json()["stats"]
    assert "files_indexed" in stats
    assert stats["files_indexed"] > 0


# --- Status ---

def test_status(test_client):
    r = test_client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert "total_files" in data
    assert "total_chunks" in data
    assert "projects" in data


# --- Projects ---

def test_projects(test_client):
    r = test_client.get("/api/projects")
    assert r.status_code == 200
    projects = r.json()["projects"]
    assert len(projects) > 0
    assert any(p["project_id"] == "project_a" for p in projects)


# --- Config ---

def test_config(test_client):
    r = test_client.get("/api/config")
    assert r.status_code == 200
    data = r.json()
    assert "embedding_model" in data
    assert "service_port" in data


def test_config_update_invalid(test_client):
    r = test_client.put("/api/config", json={"chunk_size": 0})
    assert r.status_code == 422


def test_config_update_valid(test_client, monkeypatch):
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = os.path.join(tmpdir, "test_config.toml")
        monkeypatch.setenv("RAG_CONFIG_PATH", cfg_path)
        # Save original and restore after
        from ragtools.service import app as app_module
        original_top_k = app_module._settings.top_k
        try:
            r = test_client.put("/api/config", json={"top_k": 5})
            assert r.status_code == 200
            data = r.json()
            assert "updated" in data
            assert data["restart_required"] is False
        finally:
            object.__setattr__(app_module._settings, "top_k", original_top_k)


def test_config_update_restart_required(test_client, monkeypatch):
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = os.path.join(tmpdir, "test_config.toml")
        monkeypatch.setenv("RAG_CONFIG_PATH", cfg_path)
        r = test_client.put("/api/config", json={"service_port": 9999})
        assert r.status_code == 200
        assert r.json()["restart_required"] is True
        # service_port is restart-only, not hot-reloaded, so no in-memory restore needed


def test_config_exposes_desktop_notifications(test_client):
    r = test_client.get("/api/config")
    assert r.status_code == 200
    data = r.json()
    assert "desktop_notifications" in data
    assert isinstance(data["desktop_notifications"], bool)
    assert "notification_cooldown_seconds" in data


def test_config_update_toggles_desktop_notifications(test_client, monkeypatch):
    """The notifications flag must round-trip through PUT /api/config and be
    visible on the next GET — this is the round-trip the admin-panel checkbox
    relies on."""
    import tempfile, os
    from ragtools.service import app as app_module

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = os.path.join(tmpdir, "test_config.toml")
        monkeypatch.setenv("RAG_CONFIG_PATH", cfg_path)
        original = app_module._settings.desktop_notifications
        try:
            # Flip the opposite way from the current default so we detect
            # actual changes, not stale values.
            target = not original
            r = test_client.put("/api/config", json={"desktop_notifications": target})
            assert r.status_code == 200
            assert "desktop_notifications" in r.json()["updated"]

            r2 = test_client.get("/api/config")
            assert r2.json()["desktop_notifications"] is target
        finally:
            object.__setattr__(app_module._settings, "desktop_notifications", original)


# --- Notifications ---


def test_notifications_test_skipped_when_disabled(test_client):
    """If the user has notifications off, the test button must say so and
    not actually fire a toast — the error would be very confusing."""
    from ragtools.service import app as app_module

    original = app_module._settings.desktop_notifications
    try:
        object.__setattr__(app_module._settings, "desktop_notifications", False)
        r = test_client.post("/api/notifications/test")
        assert r.status_code == 200
        body = r.json()
        assert body["sent"] is False
        assert body["reason"] == "disabled"
    finally:
        object.__setattr__(app_module._settings, "desktop_notifications", original)


def test_notifications_test_dispatches_when_enabled(test_client, monkeypatch):
    """When enabled, the endpoint must actually invoke the notifier backend.

    We patch default_backend so no OS toast fires during tests but we can
    still assert the send() call happened with expected title/body.
    """
    from ragtools.service import app as app_module
    from ragtools.service import notify as notify_module

    captured = []

    class CapturingBackend:
        def send(self, title, message, deep_link=None):
            captured.append({"title": title, "message": message, "deep_link": deep_link})

    monkeypatch.setattr(notify_module, "default_backend", lambda: CapturingBackend())

    original = app_module._settings.desktop_notifications
    try:
        object.__setattr__(app_module._settings, "desktop_notifications", True)
        r = test_client.post("/api/notifications/test")
        assert r.status_code == 200
        assert r.json()["sent"] is True
        assert len(captured) == 1
        assert "test" in captured[0]["title"].lower()
        assert captured[0]["deep_link"].startswith("http://")
    finally:
        object.__setattr__(app_module._settings, "desktop_notifications", original)


# --- Watcher ---

def test_watcher_status_not_running(test_client):
    r = test_client.get("/api/watcher/status")
    assert r.status_code == 200
    assert r.json()["running"] is False
