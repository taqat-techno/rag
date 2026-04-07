"""Tests for the admin panel page routes and htmx fragments."""

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
def client():
    """Create a test client with in-memory Qdrant and indexed fixtures."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_db = str(Path(tmpdir) / "test_state.db")
        settings = Settings(content_root=str(FIXTURES), state_db=state_db)
        qdrant_client = Settings.get_memory_client()
        owner = QdrantOwner(settings=settings, client=qdrant_client)
        owner.run_full_index()

        app_module._owner = owner
        app_module._settings = settings

        app = create_app()
        with TestClient(app, raise_server_exceptions=True) as tc:
            yield tc

        app_module._owner = None
        app_module._settings = None


# --- Full page renders ---


def test_dashboard_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Dashboard" in r.text
    assert "RAG Tools" in r.text


def test_search_page_renders(client):
    r = client.get("/search")
    assert r.status_code == 200
    assert "Search" in r.text


def test_index_page_renders(client):
    r = client.get("/index")
    assert r.status_code == 200
    assert "Indexing" in r.text


def test_ignore_page_renders(client):
    r = client.get("/ignore")
    assert r.status_code == 200
    assert "Ignore Rules" in r.text


def test_config_page_renders(client):
    r = client.get("/config")
    assert r.status_code == 200
    assert "Settings" in r.text


def test_startup_page_renders(client):
    r = client.get("/startup")
    assert r.status_code == 200
    assert "Startup" in r.text


# --- htmx fragment routes ---


def test_ui_status_fragment(client):
    r = client.get("/ui/status")
    assert r.status_code == 200
    assert "Total files" in r.text


def test_ui_projects_fragment(client):
    r = client.get("/ui/projects")
    assert r.status_code == 200
    assert "project_a" in r.text


def test_ui_watcher_fragment(client):
    r = client.get("/ui/watcher")
    assert r.status_code == 200
    assert "Stopped" in r.text or "Running" in r.text


def test_ui_search_empty(client):
    r = client.get("/ui/search", params={"query": ""})
    assert r.status_code == 200
    assert "Enter a search query" in r.text


def test_ui_search_with_results(client):
    r = client.get("/ui/search", params={"query": "backend architecture"})
    assert r.status_code == 200
    assert "result" in r.text.lower()


def test_ui_index_incremental(client):
    r = client.post("/ui/index")
    assert r.status_code == 200
    assert "index" in r.text.lower()


def test_ui_index_full(client):
    r = client.post("/ui/index?full=true")
    assert r.status_code == 200
    assert "complete" in r.text.lower()


def test_ui_ignore_builtin(client):
    r = client.get("/ui/ignore/builtin")
    assert r.status_code == 200
    assert ".git/" in r.text


def test_ui_ignore_config(client):
    r = client.get("/ui/ignore/config")
    assert r.status_code == 200
    # May be empty if no config patterns set


def test_ui_ignore_ragignore(client):
    r = client.get("/ui/ignore/ragignore")
    assert r.status_code == 200


def test_ui_ignore_test_ignored(client):
    r = client.post("/ui/ignore/test", data={"path": ".git/config"})
    assert r.status_code == 200
    assert "IGNORED" in r.text


def test_ui_ignore_test_not_ignored(client):
    r = client.post("/ui/ignore/test", data={"path": "project_a/README.md"})
    assert r.status_code == 200
    assert "NOT IGNORED" in r.text


def test_ui_config_fragment(client):
    r = client.get("/ui/config")
    assert r.status_code == 200
    assert "Indexing" in r.text
    assert "Retrieval" in r.text


# --- Projects page ---


def test_projects_page_renders(client):
    r = client.get("/projects")
    assert r.status_code == 200
    assert "Projects" in r.text
    assert "Add Project" in r.text


def test_ui_projects_list_fragment(client):
    r = client.get("/ui/projects/list")
    assert r.status_code == 200


# --- Static files ---


def test_map_page_renders(client):
    r = client.get("/map")
    assert r.status_code == 200
    assert "map-canvas" in r.text
    assert "Semantic Map" in r.text


def test_static_css(client):
    r = client.get("/static/design.css")
    assert r.status_code == 200
    assert "--color-primary" in r.text


def test_static_map_js(client):
    r = client.get("/static/map.js")
    assert r.status_code == 200
    assert "map-canvas" in r.text
