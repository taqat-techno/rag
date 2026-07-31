"""A rebuild must be able to fail without destroying anything.

v3.4's rebuild dropped every collection and deleted the state DB BEFORE it
indexed anything, then walked projects in one loop with no try/except. On the
installed machine it reached project 14 of 15 and hit a transient
``WinError 10048`` from the Qdrant client; the whole run aborted. Project 15
(2,233 eligible files) was left with an empty collection it had been given up
front, project 14 kept 1,442 of ~35,000 files, and because ``clear_intent``
sits in a ``finally`` the evidence was erased — ``/health`` reported
``degraded: false, issues: []`` for the next twelve hours.

The invariants under test:

* I-1  no project's active collection is dropped before its replacement passes
       verification;
* I-2  one project failing does not change another project's outcome;
* I-3  a run with any failure is never reported as a plain success;
* I-5  failure state survives the run instead of being cleared in a finally.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.service.owner import QdrantOwner

PROJECTS = ["alpha", "beta", "gamma", "delta"]


def _write(root: Path) -> list[ProjectConfig]:
    configs = []
    for i, name in enumerate(PROJECTS):
        d = root / name
        d.mkdir()
        (d / "doc.md").write_text(
            f"# {name}\n\nSubsystem {name} handles ledger reconciliation "
            f"for region {i}.\n## Retry\n\nBackoff windows widen after {i} failures.\n",
            encoding="utf-8")
        configs.append(ProjectConfig(id=name, path=str(d), mode="docs"))
    return configs


@pytest.fixture
def owner():
    # ignore_cleanup_errors: the embedded store and the registries keep handles
    # open past the fixture on Windows, and a PermissionError raised while
    # deleting the tempdir would report as an ERROR on a test that had already
    # made its point.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        configs = _write(root)
        settings = Settings(
            content_root=str(root),
            qdrant_path=str(root / "qdrant"),
            state_db=str(root / "state.db"),
            data_dir=str(root / "data"),
            collection_strategy="per_project",
            projects=configs,
        )
        o = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        yield o
        o.close()


def _points_by_project(owner) -> dict[str, int]:
    out = {}
    for entry in owner._router.describe():
        if entry.get("project"):
            out[entry["project"]] = owner._count_points(entry["name"])
    return out


def test_a_clean_rebuild_indexes_every_project(owner):
    owner.run_full_index()
    before = _points_by_project(owner)
    assert all(v and v > 0 for v in before.values()), before

    stats = owner.rebuild()

    after = _points_by_project(owner)
    assert set(after) == set(PROJECTS)
    assert all(v and v > 0 for v in after.values()), after
    assert not stats.get("failed_projects"), stats


def test_one_project_failing_does_not_stop_the_others(owner, monkeypatch):
    """I-2. The v3.4 loop had no try/except, so the first error ended the run
    and every project after it in scan order was left empty."""
    owner.run_full_index()

    from ragtools.service import owner as owner_module
    real_index_file = owner_module.index_file

    def _boom(*args, **kwargs):
        if kwargs.get("project_id") == "gamma":
            raise RuntimeError("[WinError 10048] simulated transient socket failure")
        return real_index_file(*args, **kwargs)

    monkeypatch.setattr(owner_module, "index_file", _boom)
    stats = owner.rebuild()

    assert "gamma" in stats.get("failed_projects", []), stats
    survivors = [p for p in PROJECTS if p != "gamma"]
    after = _points_by_project(owner)
    for name in survivors:
        assert after.get(name), f"{name} was collateral damage of gamma's failure: {after}"


def test_a_failed_project_keeps_its_previous_index(owner, monkeypatch):
    """I-1. The whole point of building before dropping.

    v3.4 recreated every collection up front, so a project that later failed
    was left holding the empty collection it had already been given.
    """
    owner.run_full_index()
    before = _points_by_project(owner)
    assert before["gamma"]

    from ragtools.service import owner as owner_module
    real_index_file = owner_module.index_file

    def _boom(*args, **kwargs):
        if kwargs.get("project_id") == "gamma":
            raise RuntimeError("simulated failure")
        return real_index_file(*args, **kwargs)

    monkeypatch.setattr(owner_module, "index_file", _boom)
    owner.rebuild()

    after = _points_by_project(owner)
    assert after["gamma"] == before["gamma"], (
        "the failed project lost its previous, valid index — a rebuild that "
        "fails must leave the machine no worse than it started"
    )


def test_a_partial_rebuild_is_not_reported_as_success(owner, monkeypatch):
    """I-3/I-4. `Rebuild complete` rendered green for 0 files, 0 chunks."""
    owner.run_full_index()

    from ragtools.service import owner as owner_module
    real_index_file = owner_module.index_file

    def _boom(*args, **kwargs):
        if kwargs.get("project_id") == "gamma":
            raise RuntimeError("simulated failure")
        return real_index_file(*args, **kwargs)

    monkeypatch.setattr(owner_module, "index_file", _boom)
    stats = owner.rebuild()

    assert stats.get("status") == "completed_with_failures", stats
    assert stats.get("failed_projects") == ["gamma"], stats


def test_failure_evidence_survives_the_run(owner, monkeypatch):
    """I-5. `clear_intent` in a `finally` erased the only durable record.

    A hard kill left the marker behind; an exception — the common case, and
    the one that actually happened — wiped it, so /health reported healthy.
    """
    from ragtools.service import destructive
    from ragtools.service import owner as owner_module

    owner.run_full_index()
    real_index_file = owner_module.index_file

    def _boom(*args, **kwargs):
        if kwargs.get("project_id") == "gamma":
            raise RuntimeError("simulated failure")
        return real_index_file(*args, **kwargs)

    monkeypatch.setattr(owner_module, "index_file", _boom)
    owner.rebuild()

    pending = destructive.pending_intent(owner._settings)
    assert pending, "a rebuild that failed left no evidence that it had run"
    assert "gamma" in str(pending.get("failed_projects", "")), pending


def test_a_successful_rebuild_clears_the_marker(owner):
    """The converse: a clean run must not leave the install looking degraded."""
    from ragtools.service import destructive

    owner.run_full_index()
    owner.rebuild()

    assert not destructive.pending_intent(owner._settings)


def test_the_state_db_is_not_destroyed_up_front(owner, monkeypatch):
    """The state DB was unlinked before indexing, so a failure left no record
    of what had ever been indexed — the thing needed to diagnose it."""
    owner.run_full_index()

    from ragtools.service import owner as owner_module
    real_index_file = owner_module.index_file

    def _boom(*args, **kwargs):
        if kwargs.get("project_id") == "gamma":
            raise RuntimeError("simulated failure")
        return real_index_file(*args, **kwargs)

    monkeypatch.setattr(owner_module, "index_file", _boom)
    owner.rebuild()

    assert Path(owner._settings.state_db).exists(), "the state DB was destroyed"
    from ragtools.indexing.state import IndexState
    state = IndexState(owner._settings.state_db)
    try:
        summary = state.get_summary()
    finally:
        state.close()
    survivors = {p for p in PROJECTS if p != "gamma"}
    assert survivors.issubset(set(summary["projects"])), summary
