"""R06 refinement — one registry fingerprint was answering two questions.

The first R06 folded a digest of the whole ``project_id -> collection_name``
mapping into ``IndexIdentity`` and let a difference invalidate the state DB.
That conflated:

    "is this the same registry?"             — global, integrity
    "is this project's mapping still valid?" — per project, a skip decision

and the conflation has a price a user pays on an ordinary Tuesday: **registering
project N+1 changes the registry-wide digest, so the N projects already indexed
are declared untrustworthy and re-embedded.** Adding one small project to a
15-project install costs a full corpus re-index. Nothing is corrupt, nothing
warns usefully, and the machine is busy for an hour.

So there are now two identities:

* the registry fingerprint stays, as a GLOBAL INTEGRITY signal — corruption,
  replacement, rollback, backup/restore validation, degraded health, and the
  block on pointer swaps and orphan reaping while ownership is ambiguous;
* :class:`~ragtools.index_identity.ProjectIdentity` — uuid, collection,
  generation, embedding identity — persisted one row per project in
  ``index_meta``, and the ONLY thing that decides whether a project's file
  hashes may be trusted.

Every test below that counts ``indexed`` / ``skipped`` is a behavioural control
against the fused version: on b8a10ea each of them re-indexes every project.

Nothing in this work package deletes a collection. A mismatch means RE-INDEX.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from ragtools import registry_integrity
from ragtools.config import ProjectConfig, Settings
from ragtools.index_identity import (
    IndexIdentity,
    META_KEY,
    current_identity,
    current_project_identities,
    project_meta_key,
    reconcile,
    stamp,
    stamp_projects,
    stored_project_identities,
    untrusted_projects,
)
from ragtools.indexing.state import IndexState
from ragtools.registry import (
    ProjectRegistry,
    RegistryIntegrityError,
    project_mapping,
    registry_fingerprint,
    sync_projects_from_config,
)
from ragtools.service.owner import QdrantOwner

#: Files written per project. Small enough to index in a test, large enough
#: that "8 skipped" cannot be confused with "0 skipped".
FILES = 4


@pytest.fixture(autouse=True)
def _fresh_integrity_cache():
    """The last-status cache is process-wide; no test may inherit another's."""
    registry_integrity.reset_for_tests()
    yield
    registry_integrity.reset_for_tests()


# --- harness ---------------------------------------------------------------


def _write_project(root: Path, pid: str, files: int = FILES) -> Path:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (d / f"{pid}_{i}.md").write_text(
            f"# {pid} document {i}\n\nDistinct prose for {pid}, number {i}.\n",
            encoding="utf-8")
    return d


def _settings(root: Path, project_ids) -> Settings:
    return Settings(
        content_root=str(root),
        qdrant_path=str(root / "qdrant"),
        state_db=str(root / "state.db"),      # SHARED across owner instances
        data_dir=str(root / "data"),          # SHARED — this is where registry.db lives
        collection_strategy="per_project",
        projects=[ProjectConfig(id=p, path=str(root / p), mode="docs")
                  for p in project_ids],
    )


@contextmanager
def _owner(root: Path, project_ids, client):
    """A service owner over ``project_ids``, sharing one vector store.

    Reconstructing the owner is how a project addition really arrives: the
    config changes, ``build_router`` re-syncs the registry, and the next
    incremental run has to decide what it may still skip.
    """
    owner = QdrantOwner(settings=_settings(root, project_ids), client=client)
    try:
        yield owner
    finally:
        owner.close()


def _snapshot_db(source: Path, target: Path) -> None:
    """Copy a live SQLite DB safely (WAL and all), via the online backup API."""
    src = sqlite3.connect(str(source))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _replace_db(source: Path, target: Path) -> None:
    """Put ``source`` back in ``target``'s place — a registry restore."""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(target) + suffix)
        if p.exists():
            p.unlink()
    _snapshot_db(source, target)


def _record_mapping(state, registry, *, dimension: int = 384,
                    model: str = "all-MiniLM-L6-v2") -> None:
    """Stamp both identities the way a completed index run does."""
    settings = _Shim(model)
    stamp(state, current_identity(settings, dimension, registry=registry))
    stamp_projects(state, current_project_identities(
        settings, dimension, registry=registry))


@dataclass
class _Shim:
    """Just enough settings for the identity builders."""

    embedding_model: str = "all-MiniLM-L6-v2"
    storage_backend: str = "embedded"
    collection_strategy: str = "per_project"
    collection_name: str = "markdown_kb"


@pytest.fixture
def registry(tmp_path):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    reg = ProjectRegistry(str(data / "registry.db"))
    for pid in ("alpha", "beta", "gamma"):
        reg.add(pid, path=str(tmp_path / pid))
    yield reg
    reg.close()


@pytest.fixture
def state(tmp_path):
    s = IndexState(str(tmp_path / "state.db"))
    yield s
    s.close()


# === 1. adding a project costs only the project that was added =============


def _index_two_then_add_a_third(root: Path, client) -> dict:
    for pid in ("alpha", "beta"):
        _write_project(root, pid)
    with _owner(root, ["alpha", "beta"], client) as owner:
        first = owner.run_full_index()
        assert first["files_indexed"] == 2 * FILES, first

    _write_project(root, "gamma")
    with _owner(root, ["alpha", "beta", "gamma"], client) as owner:
        return owner.run_incremental_index()


def test_adding_a_project_does_not_reindex_the_ones_already_there(tmp_path):
    """THE defect. Project N+1 must cost N+1, and nothing else.

    On b8a10ea the registry-wide fingerprint changes the moment gamma is
    registered, ``reconcile`` reports the whole state DB untrustworthy, and
    every file of alpha and beta is re-embedded: ``skipped == 0``.
    """
    stats = _index_two_then_add_a_third(tmp_path, Settings.get_memory_client())

    assert stats["skipped"] == 2 * FILES, (
        f"the projects that were already indexed were re-indexed because a "
        f"DIFFERENT project was added: {stats}"
    )
    assert stats["indexed"] == FILES, (
        f"expected only the new project's files to be indexed: {stats}"
    )


def test_only_the_new_project_receives_an_index_job(tmp_path):
    """The work is scoped to N+1 by identity, not merely by counting."""
    stats = _index_two_then_add_a_third(tmp_path, Settings.get_memory_client())

    assert stats["projects"] == ["gamma"], (
        f"projects other than the new one were given indexing work: {stats}"
    )


def test_the_registry_fingerprint_is_no_longer_a_skip_input(registry, state, tmp_path):
    """Unit form of the same rule, at the seam that used to fuse the two.

    A fourth project changes the registry-wide digest. With per-project rows
    recorded, that is an *integrity observation* about the registry — not a
    reason to distrust three projects' file hashes.
    """
    _record_mapping(state, registry)
    before = registry_fingerprint(registry)

    registry.add("delta", path=str(tmp_path / "delta"))
    after = registry_fingerprint(registry)
    assert before != after, "the fingerprint should still notice a new project"

    trustworthy, changed = reconcile(
        state, current_identity(_Shim(), 384, registry=registry))
    assert trustworthy, (
        f"a project ADDITION invalidated the whole index state: {changed}"
    )
    assert untrusted_projects(
        state, current_project_identities(_Shim(), 384, registry=registry)) == {}, (
        "no existing project's mapping changed, so none may be invalidated"
    )


# === 2/3. one project's pointer moves — one project is invalidated ==========


def test_repointing_one_project_invalidates_only_that_project(tmp_path):
    """Project 3's pointer moves. Projects 1 and 2 keep their index.

    Also the "never a delete" property: the collection gamma was moved off is
    still there, still holding its points. An identity mismatch schedules a
    re-index; nothing in R06 removes a collection.
    """
    client = Settings.get_memory_client()
    for pid in ("alpha", "beta", "gamma"):
        _write_project(tmp_path, pid)

    with _owner(tmp_path, ["alpha", "beta", "gamma"], client) as owner:
        owner.run_full_index()

        rec = owner.registry.get("gamma")
        vacated = rec.collection_name
        before = client.count(collection_name=vacated, exact=True).count
        assert before > 0

        owner.registry.set_active_collection(
            rec.uuid, f"{vacated}_g1", generation=1)

        stats = owner.run_incremental_index()

        assert stats["projects"] == ["gamma"], stats
        assert stats["indexed"] == FILES, stats
        assert stats["skipped"] == 2 * FILES, (
            f"repointing ONE project re-indexed the others: {stats}"
        )
        assert client.count(collection_name=f"{vacated}_g1",
                            exact=True).count > 0, (
            "gamma was invalidated but never re-indexed into its new collection"
        )
        assert client.count(collection_name=vacated, exact=True).count == before, (
            "the collection gamma was moved off was emptied; a mismatch must "
            "trigger a re-index, never a delete"
        )


# === 4. a re-added project may not inherit the old one's state rows ========


def test_a_re_added_project_cannot_reuse_the_old_projects_state_rows(tmp_path):
    """Same project id, new UUID, new (empty) collection — old file rows survive.

    Those rows describe chunks in a collection this project no longer owns. If
    they are trusted, every file is skipped and the new collection stays empty:
    the confidently-empty index, one project wide.
    """
    client = Settings.get_memory_client()
    for pid in ("alpha", "beta"):
        _write_project(tmp_path, pid)

    with _owner(tmp_path, ["alpha", "beta"], client) as owner:
        owner.run_full_index()

        old = owner.registry.get("alpha")
        owner.registry.remove("alpha")           # row dropped; collection kept
        fresh = owner.registry.add("alpha", path=str(tmp_path / "alpha"))
        assert fresh.uuid != old.uuid
        assert fresh.collection_name != old.collection_name

        state = IndexState(str(tmp_path / "state.db"))
        try:
            assert state.get_all_for_project("alpha"), (
                "precondition: the removed project's file rows are still there"
            )
            changed = untrusted_projects(state, current_project_identities(
                owner.settings, owner.encoder.dimension, registry=owner.registry))
        finally:
            state.close()
        assert "project_uuid" in changed.get("alpha", []), changed

        stats = owner.run_incremental_index()

        assert stats["projects"] == ["alpha"], stats
        assert stats["indexed"] == FILES, (
            f"the re-added project reused state rows written for a collection "
            f"it no longer owns: {stats}"
        )
        assert stats["skipped"] == FILES, f"beta was disturbed: {stats}"
        assert client.count(collection_name=fresh.collection_name,
                            exact=True).count > 0
        assert client.count(collection_name=old.collection_name,
                            exact=True).count > 0, (
            "the orphaned collection was destroyed; R06 orphans, never deletes"
        )


def test_a_scoped_run_does_not_stamp_the_projects_it_did_not_touch(tmp_path):
    """Indexing project A must not record project B's mapping as proven.

    B is repointed and still owes a re-index. If A's scoped run stamps B's
    current mapping, B looks finished and its new collection stays empty for
    ever — the confidently-empty index, one project wide.
    """
    client = Settings.get_memory_client()
    for pid in ("alpha", "beta"):
        _write_project(tmp_path, pid)

    with _owner(tmp_path, ["alpha", "beta"], client) as owner:
        owner.run_full_index()

        rec = owner.registry.get("beta")
        owner.registry.set_active_collection(rec.uuid, f"{rec.collection_name}_g1",
                                             generation=1)
        owner.run_full_index(project_id="alpha")      # scoped: alpha only

        state = IndexState(str(tmp_path / "state.db"))
        try:
            still_stale = untrusted_projects(state, current_project_identities(
                owner.settings, owner.encoder.dimension, registry=owner.registry))
        finally:
            state.close()
        assert "beta" in still_stale, (
            "indexing alpha marked beta's repointed mapping as proven"
        )

        stats = owner.run_incremental_index()
        assert stats["projects"] == ["beta"], stats
        assert stats["indexed"] == FILES, stats


# === 5. an older registry, restored, is caught globally =====================


def test_restoring_an_older_registry_backup_fires_the_integrity_signal(
        registry, state, tmp_path):
    """A generation that goes BACKWARDS is the signature of a restored copy.

    A fingerprint difference alone cannot tell a restore from an ordinary swap
    — both change the mapping. The recorded generation can: they only ever go
    forward.
    """
    _record_mapping(state, registry)
    older = tmp_path / "registry.older.db"
    _snapshot_db(Path(registry.db_path), older)

    rec = registry.get("gamma")
    registry.set_active_collection(rec.uuid, f"{rec.collection_name}_g1",
                                   generation=1)
    _record_mapping(state, registry)             # the index now matches g1
    live_path = Path(registry.db_path)
    registry.close()

    _replace_db(older, live_path)                # the operator restores g0
    restored = ProjectRegistry(str(live_path))
    try:
        status = registry_integrity.evaluate(state, restored)
    finally:
        restored.close()

    assert status.state == registry_integrity.STATE_ROLLED_BACK, status.describe()
    assert not status.ok and status.degraded, status.describe()
    assert "gamma" in status.regressed, status


def test_a_registry_that_lost_a_project_is_a_rollback_not_an_addition(
        registry, state, tmp_path):
    """The other shape of an older copy: a project the index knows is absent."""
    _record_mapping(state, registry)
    registry.remove("gamma")

    status = registry_integrity.evaluate(state, registry)
    assert status.state == registry_integrity.STATE_ROLLED_BACK, status.describe()
    assert status.removed == ("gamma",), status
    assert status.degraded


def test_an_addition_is_not_an_integrity_failure(registry, state, tmp_path):
    """The counterexample that keeps the signal usable.

    If adding a project degraded integrity, every install would sit permanently
    degraded and the signal would be ignored — which is how a real one gets
    missed.
    """
    _record_mapping(state, registry)
    registry.add("delta", path=str(tmp_path / "delta"))

    status = registry_integrity.evaluate(state, registry)
    assert status.state == registry_integrity.STATE_EXTENDED, status.describe()
    assert status.ok and not status.degraded
    assert status.added == ("delta",)
    assert status.affected == (), "an addition affects no existing project"


# === 6/7. a lost registry mints nothing, rebuilds nothing, blocks writes ====


def test_losing_the_registry_mints_no_replacements_and_rebuilds_nothing(tmp_path):
    """The original R06 failure, re-checked against the refined design.

    Losing registry.db must not produce N fresh uuid4s (N empty collections
    beside the N holding the data), must not start a destructive rebuild, and
    must leave the vectors exactly where they are — the collection names still
    encode the UUIDs, so the loss stays recoverable.
    """
    client = Settings.get_memory_client()
    for pid in ("alpha", "beta"):
        _write_project(tmp_path, pid)

    with _owner(tmp_path, ["alpha", "beta"], client) as owner:
        owner.run_full_index()
        mapping = project_mapping(owner.registry)
        counts = {name: client.count(collection_name=name, exact=True).count
                  for name in mapping.values()}
    assert all(c > 0 for c in counts.values()), counts

    registry_db = tmp_path / "data" / "registry.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(registry_db) + suffix)
        if p.exists():
            p.unlink()

    with _owner(tmp_path, ["alpha", "beta"], client) as owner:
        assert project_mapping(owner.registry) == {}, (
            "a lost registry was silently repopulated with fresh identities; "
            "the vectors are in the OLD collections"
        )
        live = {c.name for c in client.get_collections().collections}
        assert set(mapping.values()) <= live, "existing collections were dropped"
        for name, count in counts.items():
            assert client.count(collection_name=name, exact=True).count == count, (
                f"{name} was emptied; nothing in R06 may delete or rebuild"
            )

        status = registry_integrity.last_status()
        assert status is not None and status.state == registry_integrity.STATE_MISSING, (
            status.describe() if status else "no reconciliation ran"
        )
        assert status.degraded, "a registry that cannot be vouched for is degraded"
        assert registry_integrity.registry_integrity_ok(owner.registry) is False

        # ... and the loss is still recoverable, by INVERTING the collection
        # name rather than by handing the project a new identity.
        from ragtools.registry import readopt_collection

        recovered = readopt_collection(
            owner.registry, "alpha", mapping["alpha"],
            path=str(tmp_path / "alpha"))
        assert recovered.collection_name == mapping["alpha"]


def test_ambiguous_registry_state_blocks_swaps_until_reconciled(registry, state):
    """A held registry refuses the two writes that make a guess permanent."""
    _record_mapping(state, registry)
    rec = registry.get("alpha")

    registry.hold("the live mapping cannot be vouched for")

    with pytest.raises(RegistryIntegrityError) as swap:
        registry.set_active_collection(rec.uuid, "proj_whatever_g1", generation=1)
    assert "refusing to swap" in str(swap.value)

    with pytest.raises(RegistryIntegrityError):
        registry.add("delta", path="/tmp/delta")

    # Unchanged: the refusal happens before the UPDATE, not after it.
    assert registry.get("alpha").collection_name == rec.collection_name

    registry.release_hold()
    swapped = registry.set_active_collection(rec.uuid, "proj_whatever_g1",
                                             generation=1)
    assert swapped.collection_name == "proj_whatever_g1"


def test_reconciliation_arms_and_then_clears_the_hold(registry, state):
    """"Until reconciled" has to mean it can actually clear.

    A hold that only ever tightens turns one bad boot into a permanently
    read-only install.
    """
    _record_mapping(state, registry)
    removed_uuid = registry.get("gamma").uuid
    collection = registry.get("gamma").collection_name
    registry.remove("gamma")

    armed = registry_integrity.reconcile_startup(state, registry)
    assert armed.blocking and registry.hold_reason

    from ragtools.registry import readopt_collection

    registry.release_hold()          # the operator's recovery step
    readopt_collection(registry, "gamma", collection, path="/tmp/gamma")
    assert registry.get("gamma").uuid == removed_uuid

    cleared = registry_integrity.reconcile_startup(state, registry)
    assert cleared.ok, cleared.describe()
    assert registry.hold_reason == ""


def test_health_reports_the_degradation(tmp_path, monkeypatch):
    """A block nobody can see is a block nobody can clear."""
    from starlette.testclient import TestClient

    import ragtools.service.app as app_module
    from ragtools.service.app import create_app
    from ragtools.service import routes as routes_module

    for pid in ("alpha",):
        _write_project(tmp_path, pid)
    settings = _settings(tmp_path, ["alpha"])
    owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
    app_module._owner = owner
    app_module._settings = settings
    routes_module.set_bound_address(None, None)
    try:
        with TestClient(create_app(), raise_server_exceptions=True) as tc:
            body = tc.get("/health").json()
            assert body["registry_integrity"] in (
                registry_integrity.STATE_OK, registry_integrity.STATE_UNKNOWN,
                registry_integrity.STATE_EXTENDED), body["registry_integrity"]
            assert "registry_integrity_unresolved" not in body["issues"]

            registry_integrity._record(registry_integrity.IntegrityStatus(
                registry_integrity.STATE_MISSING, "the registry is gone"))
            degraded = tc.get("/health").json()
            assert degraded["registry_integrity"] == registry_integrity.STATE_MISSING
            assert "registry_integrity_unresolved" in degraded["issues"]
            assert degraded["degraded"] is True
    finally:
        app_module._owner = None
        app_module._settings = None
        routes_module.set_bound_address(None, None)
        owner.close()


# === 8. a restore must bring back the mapping EXACTLY ======================


def test_a_backup_restore_preserves_the_exact_project_to_collection_mapping(
        tmp_path, registry):
    """Per project, named. "Mostly restored" is a wrong index nobody can see."""
    from ragtools.backup import backup_state_db, restore_backup

    # Its own state handle, closed before the restore: on Windows an open
    # handle makes os.replace fail, and the restore is a file replacement.
    state = IndexState(str(tmp_path / "state.db"))
    try:
        _record_mapping(state, registry)
    finally:
        state.close()
    expected = project_mapping(registry)
    settings = _BackupShim(state_db=str(tmp_path / "state.db"),
                           data_dir=str(tmp_path / "data"))
    backup_id = backup_state_db(settings, trigger="manual").name
    registry_path = Path(registry.db_path)
    registry.close()

    # Drift: two projects repointed, one removed. Nothing a fingerprint can
    # describe, and exactly what a restore has to undo.
    drifted = ProjectRegistry(str(registry_path))
    try:
        for pid in ("alpha", "beta"):
            rec = drifted.get(pid)
            drifted.set_active_collection(rec.uuid, f"{rec.collection_name}_g7",
                                          generation=7)
        drifted.remove("gamma")
        assert project_mapping(drifted) != expected
    finally:
        drifted.close()

    restore_backup(settings, backup_id)

    restored = ProjectRegistry(str(registry_path))
    try:
        ok, problems = registry_integrity.verify_restored_mapping(restored, expected)
        assert ok, problems
        assert project_mapping(restored) == expected
        assert registry_fingerprint(restored) == _stamped_fingerprint(
            IndexState(str(tmp_path / "state.db")))
    finally:
        restored.close()


def _stamped_fingerprint(state) -> str:
    try:
        return IndexIdentity.from_json(state.get_meta(META_KEY)).registry_fingerprint
    finally:
        state.close()


@dataclass
class _BackupShim:
    """Only the attributes backup.py reads."""

    state_db: str
    data_dir: str
    backup_keep: int = 10


def test_a_restore_that_lands_a_different_mapping_is_named_per_project(
        registry, state):
    """The negative half: the verifier must not pass a wrong restore."""
    expected = dict(project_mapping(registry))
    expected["gamma"] = "proj_something_else"

    ok, problems = registry_integrity.verify_restored_mapping(registry, expected)
    assert not ok
    assert any(p.startswith("gamma:") for p in problems), problems


# === 9. reaping is refused while ownership is ambiguous =====================


def test_orphan_reaping_is_refused_while_integrity_is_unresolved(registry, state):
    """WP-R03's guard. The reaper is not in scope; the predicate it consults is.

    A collection looks orphaned exactly when the registry does not claim it —
    which is what a lost registry produces for EVERY collection this install
    owns. So the reaper does not get to decide on its own evidence.
    """
    _record_mapping(state, registry)
    owned = set(project_mapping(registry).values())
    reaped: list[str] = []

    def reaper(reg, existing):
        """A reaping-style caller: ask first, then compute the orphan set."""
        registry_integrity.assert_reaping_allowed(reg)
        claimed = set(project_mapping(reg).values())
        for name in sorted(set(existing) - claimed):
            reaped.append(name)
        return reaped

    # Sound registry: the guard permits, and a genuine stray is still found.
    assert reaper(registry, owned | {"proj_stray"}) == ["proj_stray"]

    reaped.clear()
    registry.remove("gamma")
    status = registry_integrity.reconcile_startup(state, registry)
    assert status.blocking, status.describe()

    with pytest.raises(RegistryIntegrityError) as refusal:
        reaper(registry, owned)
    assert "reap orphaned collections" in str(refusal.value)
    assert reaped == [], "the reaper enumerated candidates before asking"
    assert registry_integrity.registry_integrity_ok(registry) is False
    assert registry_integrity.integrity_block_reason(registry)


def test_the_guard_refuses_a_registry_that_is_not_there_at_all(state):
    """"Nothing to ask" is not "yes"."""
    assert registry_integrity.registry_integrity_ok(None) is False
    with pytest.raises(RegistryIntegrityError):
        registry_integrity.assert_reaping_allowed(None)


# === backward compatibility ================================================


def test_a_v350_stamp_does_not_mass_invalidate(registry, state):
    """Absent is unknown, not different.

    A v3.5.0 stamp has no fingerprint and no per-project rows. Reading either
    absence as a mismatch would re-embed every upgraded install's corpus on the
    first start after the upgrade.
    """
    stamp(state, IndexIdentity(
        storage_backend="embedded", collection_strategy="per_project",
        collection_name="markdown_kb", model_name="all-MiniLM-L6-v2",
        dimension=384))                       # no registry_fingerprint at all
    state.update("alpha/doc.md", "alpha", "hash", 3)

    trustworthy, changed = reconcile(
        state, current_identity(_Shim(), 384, registry=registry))
    assert trustworthy, changed
    assert untrusted_projects(state, current_project_identities(
        _Shim(), 384, registry=registry)) == {}
    assert registry_integrity.evaluate(state, registry).state == \
        registry_integrity.STATE_UNKNOWN


def test_a_stamp_with_a_fingerprint_but_no_per_project_rows_stays_conservative(
        registry, state, tmp_path):
    """The one stamp shape that cannot be localised: answer for the whole store.

    A fingerprint with no per-project rows can only have come from the fused
    version. There is nothing to say WHICH project moved, and inventing an
    answer is worse than paying for one re-index.
    """
    stamp(state, current_identity(_Shim(), 384, registry=registry))
    state.update("alpha/doc.md", "alpha", "hash", 3)
    assert stored_project_identities(state) == {}

    registry.add("delta", path=str(tmp_path / "delta"))
    trustworthy, changed = reconcile(
        state, current_identity(_Shim(), 384, registry=registry))
    assert not trustworthy
    assert changed == ["registry_fingerprint"]


def test_shared_layout_records_and_compares_nothing(state):
    """No registry, no fingerprint, no per-project rows — and no behaviour change."""
    identity = current_identity(_Shim(collection_strategy="shared"), 384)
    assert identity.registry_fingerprint == ""
    stamp(state, identity)
    trustworthy, changed = reconcile(
        state, current_identity(_Shim(collection_strategy="shared"), 384))
    assert trustworthy and changed == []
    assert registry_integrity.evaluate(state, None).state == \
        registry_integrity.STATE_UNKNOWN


# === persistence shape =====================================================


def test_the_per_project_identity_lands_in_index_meta(registry, state):
    """Persisted beside the existing state, one row per project — not a new store."""
    _record_mapping(state, registry)

    for pid in ("alpha", "beta", "gamma"):
        raw = state.get_meta(project_meta_key(pid))
        assert raw, f"no recorded identity for {pid}"
    recorded = stored_project_identities(state)
    assert set(recorded) == {"alpha", "beta", "gamma"}
    assert recorded["alpha"].project_uuid == registry.get("alpha").uuid
    assert recorded["alpha"].collection_name == registry.get("alpha").collection_name
    assert recorded["alpha"].dimension == 384


def test_a_deliberate_removal_forgets_its_own_row(registry, state, tmp_path):
    """Otherwise every ordinary project removal reads as a lost registry."""
    from ragtools.index_identity import forget_project

    _record_mapping(state, registry)
    registry.remove("gamma")
    forget_project(state, "gamma")

    status = registry_integrity.evaluate(state, registry)
    assert status.state == registry_integrity.STATE_OK, status.describe()
    assert registry_integrity.reconcile_startup(state, registry).ok
    assert registry.hold_reason == ""


def test_sync_reports_blocked_projects_instead_of_minting_them(registry, state,
                                                               tmp_path):
    """A refused mint is a reported absence, never a silent empty collection."""
    _record_mapping(state, registry)
    registry.hold("integrity unresolved")

    configs = [ProjectConfig(id="delta", path=str(tmp_path / "delta"), mode="docs")]
    result = sync_projects_from_config(configs, registry)

    assert result["blocked"] == ["delta"], result
    assert result["added"] == 0
    assert registry.get("delta") is None
