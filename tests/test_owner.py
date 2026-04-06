"""Tests for QdrantOwner — the sole Qdrant access point."""

import tempfile
from pathlib import Path

import pytest

from ragtools.config import Settings
from ragtools.service.owner import QdrantOwner


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def owner():
    """Create a QdrantOwner with in-memory Qdrant and test fixtures."""
    # Use a temp dir for state DB to avoid polluting real data
    with tempfile.TemporaryDirectory() as tmpdir:
        state_db = str(Path(tmpdir) / "test_state.db")
        settings = Settings(content_root=str(FIXTURES), state_db=state_db)
        client = Settings.get_memory_client()
        o = QdrantOwner(settings=settings, client=client)
        o.run_full_index()
        yield o


def test_owner_initializes(owner):
    assert owner.client is not None
    assert owner.encoder is not None


def test_owner_search_returns_results(owner):
    results = owner.search("backend architecture Python FastAPI")
    assert len(results) > 0
    assert results[0].score > 0


def test_owner_search_formatted(owner):
    data = owner.search_formatted("backend architecture")
    assert "query" in data
    assert "results" in data
    assert "formatted" in data
    assert data["count"] > 0


def test_owner_search_with_project_filter(owner):
    results = owner.search("backend architecture", project_id="project_a")
    for r in results:
        assert r.project_id == "project_a"


def test_owner_search_no_results(owner):
    results = owner.search("xyznonexistent12345")
    assert len(results) == 0


def test_owner_get_status(owner):
    status = owner.get_status()
    assert "total_files" in status
    assert "total_chunks" in status
    assert "projects" in status
    assert "points_count" in status
    assert status["total_files"] > 0


def test_owner_get_projects(owner):
    projects = owner.get_projects()
    assert len(projects) > 0
    project_ids = [p["project_id"] for p in projects]
    assert "project_a" in project_ids


def test_owner_incremental_index(owner):
    stats = owner.run_incremental_index()
    # All files should be skipped (already indexed)
    assert stats["skipped"] > 0
    assert stats["indexed"] == 0


def test_owner_full_index(owner):
    stats = owner.run_full_index()
    assert stats["files_indexed"] > 0
    assert stats["chunks_indexed"] > 0
