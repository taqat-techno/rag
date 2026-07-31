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
    from ragtools.config import ProjectConfig
    with tempfile.TemporaryDirectory() as tmpdir:
        state_db = str(Path(tmpdir) / "test_state.db")
        settings = Settings(
            content_root=str(FIXTURES),
            state_db=state_db,
            projects=[
                ProjectConfig(id="project_a", path=str(FIXTURES / "project_a")),
                ProjectConfig(id="project_b", path=str(FIXTURES / "project_b")),
            ],
        )
        client = Settings.get_memory_client()
        o = QdrantOwner(settings=settings, client=client)
        o.run_full_index()
        yield o


def test_owner_initializes(owner):
    assert owner.client is not None
    assert owner.encoder is not None


def test_owner_search_returns_results(owner):
    from ragtools.retrieval.scope import ScopeUnresolvedError

    # Fail-closed (S1/A2): an unscoped owner search is REFUSED, never widened.
    with pytest.raises(ScopeUnresolvedError):
        owner.search("backend architecture Python FastAPI")

    # An explicit scope still returns results (indexing + retrieval work).
    results = owner.search(
        "backend architecture Python FastAPI",
        project_ids=["project_a", "project_b"],
    )
    assert len(results) > 0
    assert results[0].score > 0


def test_owner_search_formatted(owner):
    from ragtools.retrieval.scope import ScopeUnresolvedError

    with pytest.raises(ScopeUnresolvedError):
        owner.search_formatted("backend architecture")

    data = owner.search_formatted(
        "backend architecture", project_ids=["project_a", "project_b"]
    )
    assert "query" in data
    assert "results" in data
    assert "formatted" in data
    assert data["count"] > 0


def test_owner_search_with_project_filter(owner):
    results = owner.search("backend architecture", project_id="project_a")
    for r in results:
        assert r.project_id == "project_a"


def test_owner_search_no_results(owner):
    # A scoped search with a nonsense query returns no results (not a refusal).
    results = owner.search(
        "xyznonexistent12345", project_ids=["project_a", "project_b"]
    )
    assert len(results) == 0


def test_owner_unscoped_search_can_opt_in_to_global(owner):
    """The single sanctioned global path: explicit allow_unscoped."""
    results = owner.search("backend architecture", allow_unscoped=True)
    assert isinstance(results, list)  # runs globally, does not refuse


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


# --- honest per-project state (v3.5 WP-19/20/21) --------------------------


def _drift_fixture(tmp_path, strategy: str):
    """Two projects indexed, then one project's vectors deleted underneath it.

    The shape that produced the field report: a project that is configured,
    enabled, has a real folder and a full set of state-DB rows, and holds not
    one live vector — 41,832 of them gone, with the dashboard showing "14
    projects" over a table of 15 and nothing anywhere saying which.
    """
    from ragtools.config import ProjectConfig

    for name in ("alpha", "beta"):
        d = tmp_path / name
        d.mkdir()
        (d / "doc.md").write_text(f"# {name}\n\nLedger reconciliation.\n",
                                  encoding="utf-8")
    settings = Settings(
        content_root=str(tmp_path),
        qdrant_path=str(tmp_path / "qdrant"),
        state_db=str(tmp_path / "state.db"),
        data_dir=str(tmp_path / "data"),
        collection_strategy=strategy,
        projects=[ProjectConfig(id=name, path=str(tmp_path / name), mode="docs")
                  for name in ("alpha", "beta")],
    )
    o = QdrantOwner(settings=settings, client=Settings.get_memory_client())
    o.run_full_index()
    return o, settings


@pytest.mark.parametrize("strategy", ["shared", "per_project"])
def test_a_project_whose_vectors_vanished_reads_as_drifted(tmp_path, strategy):
    """Recorded files, zero live vectors: `drifted`, in BOTH layouts.

    Under `shared` the number needs a payload-filtered count; under
    `per_project` the collection is the project. The layout decides how the
    question is asked, never what the answer means.

    NEGATIVE CONTROL: before this work package `get_status_projects` did not
    exist, and the dashboard rendered this project as "Not indexed yet" — the
    same string it showed for a folder that had been deleted.
    """
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    owner, _settings = _drift_fixture(tmp_path, strategy)
    try:
        before = {row["id"]: row for row in owner.get_status_projects()}
        assert before["beta"]["state"] == "indexed", before["beta"]

        if strategy == "per_project":
            owner.client.delete_collection(
                collection_name=owner.router.write_collection("beta"))
        else:
            owner.client.delete(
                collection_name=owner.router.shared_collection,
                points_selector=Filter(must=[FieldCondition(
                    key="project_id", match=MatchValue(value="beta"))]))

        after = {row["id"]: row for row in owner.get_status_projects()}
        assert after["beta"]["state"] == "drifted", after["beta"]
        assert after["beta"]["files"] > 0, "the state DB rows are the evidence"
        assert after["beta"]["points"] == 0
        assert "re-index" in after["beta"]["reason"]
        # One project losing its vectors says nothing about the other.
        assert after["alpha"]["state"] == "indexed", after["alpha"]
    finally:
        owner.close()


def test_a_project_the_last_rebuild_failed_on_says_so(tmp_path):
    """A failed rebuild leaves the previous index in place, so the counts look
    fine and the remedy is still "read the error" — not "wait". It is recorded
    in the pending rebuild intent and was visible nowhere else."""
    from ragtools.service import destructive

    owner, settings = _drift_fixture(tmp_path, "per_project")
    try:
        destructive.record_intent(settings, {
            "operation": "rebuild", "status": "completed_with_failures",
            "failed_projects": ["beta"], "projects_rebuilt": ["alpha"]})
        rows = {row["id"]: row for row in owner.get_status_projects()}
        assert rows["beta"]["state"] == "failed", rows["beta"]
        assert rows["alpha"]["state"] == "indexed", rows["alpha"]
    finally:
        destructive.clear_intent(settings)
        owner.close()


def test_status_counts_projects_configured_apart_from_projects_indexed(tmp_path):
    """The two numbers the dashboard merged, and the third nobody had.

    NEGATIVE CONTROL: on the pre-fix status dict none of these keys exist —
    there was only `projects`, the file-state list, and every surface read its
    length as though it meant "projects".
    """
    from ragtools.config import ProjectConfig

    owner, settings = _drift_fixture(tmp_path, "per_project")
    try:
        (tmp_path / "gamma").mkdir()
        owner.update_projects(list(settings.projects) + [
            ProjectConfig(id="gamma", path=str(tmp_path / "gamma"), mode="docs"),
            ProjectConfig(id="delta", path=str(tmp_path / "alpha"), mode="docs",
                          enabled=False),
        ])
        status = owner.get_status()
        assert status["projects_configured"] == 4, status
        assert status["projects_enabled"] == 3, status
        assert status["projects_indexed"] == 2, status
        assert status["projects_searchable"] == 2, status
    finally:
        owner.close()
