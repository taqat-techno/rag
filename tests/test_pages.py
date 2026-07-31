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
        from ragtools.config import ProjectConfig
        settings = Settings(
            content_root=str(FIXTURES),
            state_db=state_db,
            projects=[
                ProjectConfig(id="project_a", path=str(FIXTURES / "project_a")),
                ProjectConfig(id="project_b", path=str(FIXTURES / "project_b")),
            ],
        )
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


def test_config_page_renders(client):
    r = client.get("/config")
    assert r.status_code == 200
    assert "Settings" in r.text


# --- htmx fragment routes ---


def test_ui_status_fragment(client):
    r = client.get("/ui/status")
    assert r.status_code == 200
    assert "Total files" in r.text


def test_ui_projects_fragment(client):
    r = client.get("/ui/projects")
    assert r.status_code == 200
    assert "project_a" in r.text


def test_dash_status_tiles_say_which_question_each_number_answers(client):
    """The vitals row must not answer two questions with one label.

    A tile reading `projects` sat above a table iterating the CONFIGURED ones;
    on the installed machine that was 14 over a list of 15 and both were right,
    because the tile counted projects with at least one indexed FILE. And
    `searchable` (live vectors) and `chunks` (the state DB's total) were two
    labels over one quantity — 26,713 twice — since they agree on a healthy
    index, which is exactly why presenting both as independent facts misleads.

    NEGATIVE CONTROL: against the pre-fix fragment the labels are `searchable`,
    `files`, `chunks`, `projects`, and this fixture's live count equals its
    recorded chunk total — so both assertions below fail.
    """
    import re

    html = client.get("/ui/dash/status").text
    tiles = re.findall(r"<strong>([\d,]+)</strong>\s*<span>([^<]+)</span>", html)
    labels = [label.strip().lower() for _value, label in tiles]
    assert tiles, html

    project_labels = [label for label in labels if "project" in label]
    assert project_labels, f"no projects tile at all: {labels}"
    assert all("configured" in label or "indexed" in label
               for label in project_labels), (
        f"a projects tile does not say WHICH projects it counts: {project_labels}")

    by_value: dict[str, set[str]] = {}
    for value, label in tiles:
        by_value.setdefault(value, set()).add(label.strip().lower())
    for value, shared_labels in by_value.items():
        assert not ({"searchable", "chunks"} <= shared_labels), (
            f"{value} appears under both 'searchable' and 'chunks' — one "
            f"quantity, two labels: {tiles}")


def test_dash_projects_rows_carry_a_state_and_not_one_string(client):
    """`Not indexed yet` rendered identically for a project that was scanned and
    legitimately had nothing, one whose folder had moved, one that was switched
    off, and one whose rebuild had FAILED. Four causes, four remedies, one
    string — so each row now carries its state.

    NEGATIVE CONTROL: the pre-fix card emits no `data-state` at all.
    """
    html = client.get("/ui/dash/projects").text
    assert 'data-state="indexed"' in html, html
    assert "project_a" in html
    # The counts stay the headline for a healthy project.
    assert "files" in html and "chunks" in html


def test_every_project_state_has_a_human_badge():
    """The owner owns the vocabulary, the page owns the wording.

    A state with no entry here falls back to printing its raw enum name at the
    reader, and `no_eligible_files` is not English — the same defect the
    degraded banner had when it read `Degraded: scale_over`.
    """
    from ragtools.service.owner import PROJECT_STATES
    from ragtools.service.pages import _PROJECT_STATE_BADGE

    missing = [state for state in PROJECT_STATES
               if state not in _PROJECT_STATE_BADGE]
    assert not missing, f"no badge wording for {missing}"
    for state, (_css, label) in _PROJECT_STATE_BADGE.items():
        assert "_" not in label, f"{state} still reads as an identifier: {label}"


def test_ui_watcher_fragment(client):
    r = client.get("/ui/watcher")
    assert r.status_code == 200
    assert "Starting" in r.text or "Running" in r.text


def test_ui_search_empty(client):
    r = client.get("/ui/search", params={"query": ""})
    assert r.status_code == 200
    assert "Enter a search query" in r.text


def test_ui_search_with_results(client):
    r = client.get(
        "/ui/search",
        params={"query": "backend architecture", "project": "project_a"},
    )
    assert r.status_code == 200
    assert "result" in r.text.lower()


def test_ui_search_unscoped_prompts_for_project(client):
    # Fail-closed (S1/A2): the panel guides the user to pick a project rather
    # than silently searching every one.
    r = client.get("/ui/search", params={"query": "backend architecture"})
    assert r.status_code == 200
    assert "select a project" in r.text.lower()


def test_ui_index_incremental(client):
    r = client.post("/ui/index")
    assert r.status_code == 200
    assert "index" in r.text.lower()


def test_ui_index_full(client):
    r = client.post("/ui/index?full=true")
    assert r.status_code == 200
    assert "complete" in r.text.lower()


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
    # Case-insensitive: the intent is "the page offers a way to add a project",
    # not a particular capitalisation. Labels are sentence case product-wide.
    assert "add project" in r.text.lower()


def test_ui_projects_list_fragment(client):
    r = client.get("/ui/projects/list")
    assert r.status_code == 200


# --- Static files ---


def test_map_page_renders(client):
    r = client.get("/map")
    assert r.status_code == 200
    assert "map-canvas" in r.text
    assert "semantic map" in r.text.lower()


def test_static_css(client):
    r = client.get("/static/design.css")
    assert r.status_code == 200
    assert "--color-primary" in r.text


def test_static_map_js(client):
    r = client.get("/static/map.js")
    assert r.status_code == 200
    assert "map-canvas" in r.text
