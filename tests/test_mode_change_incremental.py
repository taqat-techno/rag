"""Mode change must be INCREMENTAL, not purge-and-re-embed.

Reported from real use: switching a project from `docs` to `general` deleted the
already-embedded documentation and re-embedded it alongside the code. On a large
project that is minutes of wasted GPU/CPU and a window where the project is
missing from search.

`run_incremental_index` already does the right thing in BOTH directions:

    deleted_paths = tracked_paths - current_paths     # narrowing purges
    if not file_changed(path, hash): skipped          # unchanged NOT re-embedded

The bug was only that the mode-change path called `reindex_project`
(= delete_project_data + run_full_index) instead.

Plan: docs/planning/RAG_MODERNIZATION_MASTER_PLAN.md  (defect fix F1)
"""

import tempfile
from pathlib import Path

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.service.owner import QdrantOwner


def _project(tmp: Path):
    """A project with 2 docs and 2 code files."""
    (tmp / "README.md").write_text(
        "# Guide\n\nThe backend is built with Python and FastAPI.\n"
        "## Setup\n\nRun the installer and start the service.\n", encoding="utf-8")
    (tmp / "notes.md").write_text(
        "# Notes\n\nAuthentication uses JWT tokens with refresh rotation.\n", encoding="utf-8")
    (tmp / "app.py").write_text(
        "def create_app():\n    '''Build the application.'''\n    return App()\n\n"
        "class App:\n    def run(self):\n        return 1\n", encoding="utf-8")
    (tmp / "util.py").write_text(
        "def helper(value):\n    '''Do a thing.'''\n    return value * 2\n", encoding="utf-8")


@pytest.fixture
def owner_docs():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj = root / "proj"
        proj.mkdir()
        _project(proj)
        settings = Settings(
            content_root=str(root),
            qdrant_path=str(root / "qdrant"),
            state_db=str(root / "state.db"),
            projects=[ProjectConfig(id="p", path=str(proj), mode="docs")],
        )
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        yield owner, settings
        owner.close()


def _set_mode(settings, mode):
    settings.projects[0].mode = mode


def test_widening_docs_to_general_does_not_reembed_the_docs(owner_docs):
    """The reported complaint, pinned."""
    owner, settings = owner_docs

    first = owner.run_full_index(project_id="p")
    docs_files = first["files_indexed"]
    assert docs_files == 2, f"expected the 2 markdown files, got {first}"

    _set_mode(settings, "general")
    owner.update_projects(list(settings.projects))

    stats = owner.run_incremental_index(project_id="p")

    # The two .md files were already embedded and are unchanged -> SKIPPED.
    assert stats["skipped"] == 2, f"docs were re-embedded: {stats}"
    # The two .py files are newly in scope -> indexed.
    assert stats["indexed"] == 2, f"code was not added: {stats}"
    # Widening removes nothing.
    assert stats["deleted"] == 0, f"widening purged something: {stats}"


def test_narrowing_general_to_docs_purges_only_the_code(owner_docs):
    owner, settings = owner_docs
    _set_mode(settings, "general")
    owner.update_projects(list(settings.projects))
    owner.run_full_index(project_id="p")

    _set_mode(settings, "docs")
    owner.update_projects(list(settings.projects))
    stats = owner.run_incremental_index(project_id="p")

    assert stats["deleted"] == 2, f"code chunks were not purged: {stats}"
    assert stats["skipped"] == 2, f"docs were re-embedded on narrowing: {stats}"
    assert stats["indexed"] == 0


def test_narrowing_actually_removes_the_code_from_search(owner_docs):
    """Purging must remove the vectors, not just the state rows."""
    owner, settings = owner_docs
    _set_mode(settings, "general")
    owner.update_projects(list(settings.projects))
    owner.run_full_index(project_id="p")
    with_code = owner.get_status()["total_chunks"]

    _set_mode(settings, "docs")
    owner.update_projects(list(settings.projects))
    owner.run_incremental_index(project_id="p")
    docs_only = owner.get_status()["total_chunks"]

    assert docs_only < with_code, "narrowing did not reduce the stored chunks"


def test_repeated_incremental_after_mode_change_is_a_no_op(owner_docs):
    """Convergence: once synced, nothing more is embedded."""
    owner, settings = owner_docs
    owner.run_full_index(project_id="p")
    _set_mode(settings, "general")
    owner.update_projects(list(settings.projects))
    owner.run_incremental_index(project_id="p")

    again = owner.run_incremental_index(project_id="p")
    assert again["indexed"] == 0 and again["deleted"] == 0
    assert again["skipped"] == 4
