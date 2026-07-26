"""Changing the store must not leave the state DB lying about it.

Incremental indexing is fast because it trusts ``index_state.db``: an unchanged
hash means "already indexed, skip". That trust holds only while the state DB and
the vector store describe the same index.

Observed on this machine during the live cutover from embedded/shared to
managed/per-project:

    state DB : 38,213 files / 88,825 chunks
    Qdrant   :                 60,930 points
    result   : "indexed 0, skipped 38286"

Nothing was corrupt. The state DB simply described the PREVIOUS store, and the
incremental run skipped every file against a store that did not contain them —
~28k chunks silently missing, and no run would ever restore them because every
subsequent incremental would skip them too.

Plan: docs/planning/RAG_COLLECTION_ARCHITECTURE_IMPLEMENTATION_PLAN.md (W9, W10)
"""

import tempfile
from pathlib import Path

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.index_identity import (
    IndexIdentity, META_KEY, current_identity, explain, reconcile, stamp,
)
from ragtools.indexing.state import IndexState
from ragtools.service.owner import QdrantOwner


def _identity(**kw):
    base = dict(storage_backend="embedded", collection_strategy="shared",
                collection_name="markdown_kb", model_name="all-MiniLM-L6-v2",
                dimension=384)
    base.update(kw)
    return IndexIdentity(**base)


@pytest.fixture
def state(tmp_path):
    s = IndexState(str(tmp_path / "state.db"))
    yield s
    s.close()


# --- the guard itself ----------------------------------------------------


def test_a_fresh_state_db_is_trustworthy_and_gets_stamped(state):
    trustworthy, changed = reconcile(state, _identity())
    assert trustworthy and changed == []
    assert state.get_meta(META_KEY), "identity was not recorded"


def test_matching_identity_is_trustworthy(state):
    stamp(state, _identity())
    trustworthy, changed = reconcile(state, _identity())
    assert trustworthy and changed == []


@pytest.mark.parametrize("field,value", [
    ("storage_backend", "managed"),
    ("collection_strategy", "per_project"),
    ("collection_name", "something_else"),
    ("model_name", "some-other-model"),
    ("dimension", 768),
])
def test_any_store_change_makes_the_state_untrustworthy(state, field, value):
    stamp(state, _identity())
    trustworthy, changed = reconcile(state, _identity(**{field: value}))
    assert not trustworthy
    assert changed == [field]
    assert field in explain(changed)


def test_a_populated_state_db_with_no_identity_is_not_trusted(state):
    """The real-world case: a state DB written before identities existed."""
    state.update("some/file.md", "proj", "hash123", 3)
    trustworthy, changed = reconcile(state, _identity())
    assert not trustworthy
    assert changed == ["unrecorded"]
    assert "predates" in explain(changed)


def test_an_empty_state_db_with_no_identity_is_fine(state):
    """Nothing indexed yet — there is nothing to be wrong about."""
    trustworthy, _ = reconcile(state, _identity())
    assert trustworthy


def test_unreadable_identity_is_not_trusted(state):
    state.set_meta(META_KEY, "{not json")
    trustworthy, changed = reconcile(state, _identity())
    assert not trustworthy
    assert changed == ["unreadable"]


def test_a_mismatch_does_not_overwrite_the_stored_identity(state):
    """Stamping on mismatch would make an interrupted migration look done."""
    stamp(state, _identity())
    before = state.get_meta(META_KEY)
    reconcile(state, _identity(storage_backend="managed"))
    assert state.get_meta(META_KEY) == before


# --- end to end: the actual data-loss scenario ---------------------------


def _owner(root: Path, strategy: str, files: int = 6):
    proj = root / "proj"
    proj.mkdir(exist_ok=True)
    for i in range(files):
        (proj / f"doc{i}.md").write_text(
            f"# Doc {i}\n\nDistinct content for document number {i}.\n",
            encoding="utf-8")
    settings = Settings(
        content_root=str(root),
        qdrant_path=str(root / f"qdrant-{strategy}"),
        state_db=str(root / "state.db"),          # SHARED across both owners
        data_dir=str(root / "data"),
        collection_strategy=strategy,
        projects=[ProjectConfig(id="proj", path=str(proj), mode="docs")],
    )
    return QdrantOwner(settings=settings, client=Settings.get_memory_client()), settings


def test_switching_the_collection_layout_reindexes_instead_of_skipping():
    """The exact failure: same state DB, different store, everything skipped."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        shared_owner, _ = _owner(root, "shared")
        try:
            first = shared_owner.run_full_index()
            assert first["files_indexed"] == 6
        finally:
            shared_owner.close()

        # New layout => a DIFFERENT, empty store, but the same state DB.
        per_owner, _ = _owner(root, "per_project")
        try:
            stats = per_owner.run_incremental_index()
            assert stats["skipped"] == 0, (
                "the incremental run trusted a state DB describing the previous "
                "store and skipped every file"
            )
            assert stats["indexed"] == 6

            status = per_owner.get_status()
            assert status["points_count"] == stats["chunks_indexed"] > 0, (
                "vectors are missing from the new store"
            )
        finally:
            per_owner.close()


def test_a_second_run_after_the_migration_skips_normally():
    """The guard must fire once, not forever — otherwise every run is a full one."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        shared_owner, _ = _owner(root, "shared")
        try:
            shared_owner.run_full_index()
        finally:
            shared_owner.close()

        per_owner, _ = _owner(root, "per_project")
        try:
            per_owner.run_incremental_index()          # migrates
            again = per_owner.run_incremental_index()   # should be a no-op
            assert again["indexed"] == 0
            assert again["skipped"] == 6
        finally:
            per_owner.close()


def test_the_identity_is_recorded_after_a_full_index():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        owner, settings = _owner(root, "per_project")
        try:
            owner.run_full_index()
            state = IndexState(settings.state_db)
            try:
                recorded = IndexIdentity.from_json(state.get_meta(META_KEY))
            finally:
                state.close()
            assert recorded == current_identity(settings, owner.encoder.dimension)
        finally:
            owner.close()
