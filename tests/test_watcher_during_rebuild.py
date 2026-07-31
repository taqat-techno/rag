"""Changes made while a rebuild is running must not be lost.

A rebuild holds the index mutex end to end, so every watcher tick during one is
answered ``busy``. That answer was discarded on the assumption that "the next
tick will pick it up". Nothing schedules a next tick — the watcher only wakes
when the filesystem moves again — and the rebuild rewrites the project's state
rows from its OWN scan, so the edited file is recorded against the hash the
rebuild read. The store kept pre-edit content, the state DB agreed with it, and
nothing anywhere was pending. The user's only recovery was a full re-index they
had no reason to know they needed.

Invariants under test:

* W-1  a file edited mid-rebuild is current afterwards, WITHOUT a restart;
* W-2  a change absorbed by the rebuild's own scan is not applied a second time;
* W-3  a file deleted mid-rebuild does not survive in the store;
* W-4  a file moved mid-rebuild is indexed at its new path ONLY;
* W-5  the queue is durable — it survives the process and replays afterwards,
       including after a rebuild that only got part way through;
* W-6  overflowing the bound records a re-scan marker; it never drops silently;
* W-7  a replay that fails is reported and its work is KEPT, not swallowed;
* W-8  the watcher actually wires this up — a `busy` answer is queued.

Reading the negative control
----------------------------
``_watcher_tick`` and ``_recover_after_restart`` reach for the new entry points
through ``getattr``. That is deliberate and is what makes these tests a real
control: against the pre-fix tree they degrade to exactly the old behaviour
(discard the ``busy`` answer; no recovery path at all) and fail on an assertion
about *stored content*, which is the defect. They do not fail because a module
is missing.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.indexing.state import IndexState
from ragtools.service.owner import QdrantOwner

PROJECTS = ["alpha", "beta", "gamma", "delta"]
FILES = ["a.md", "b.md"]


def _body(project: str, name: str, version: str) -> str:
    return (
        f"# {project} {name}\n\n"
        f"Ledger reconciliation for {project} is handled here, revision {version}.\n\n"
        f"## Retry policy\n\n"
        f"Backoff windows widen after each failure. Marker {version}.\n"
    )


def _write_tree(root: Path) -> list[ProjectConfig]:
    configs = []
    for name in PROJECTS:
        d = root / name
        d.mkdir()
        for f in FILES:
            (d / f).write_text(_body(name, f, "v1"), encoding="utf-8")
        configs.append(ProjectConfig(id=name, path=str(d), mode="docs"))
    return configs


def _build_owner(root: Path, strategy: str = "per_project") -> QdrantOwner:
    configs = _write_tree(root)
    settings = Settings(
        content_root=str(root),
        qdrant_path=str(root / "qdrant"),
        state_db=str(root / "state.db"),
        data_dir=str(root / "data"),
        collection_strategy=strategy,
        projects=configs,
    )
    return QdrantOwner(settings=settings, client=Settings.get_memory_client())


@pytest.fixture
def owner():
    # ignore_cleanup_errors: the embedded store and the SQLite stores keep
    # handles open past the fixture on Windows, and a PermissionError raised
    # while deleting the tempdir would report as an ERROR on a test that had
    # already made its point. Same reason as test_safe_rebuild.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        o = _build_owner(Path(td))
        yield o
        o.close()


# ---------------------------------------------------------------------------
# Observing the store
# ---------------------------------------------------------------------------


def _stored(owner: QdrantOwner, project_id: str) -> dict:
    """``{file_path: stored text}`` from the collection the project SERVES.

    Read through the router, so this follows the rebuild's collection swap
    rather than assuming a name.
    """
    collection = owner._router.write_collection(project_id)
    points, _ = owner._client.scroll(collection_name=collection, limit=10_000,
                                     with_payload=True)
    out: dict = {}
    for point in points:
        payload = point.payload or {}
        if payload.get("project_id") != project_id:
            continue
        out.setdefault(payload["file_path"], []).append(payload.get("text", ""))
    return {path: "\n".join(texts) for path, texts in out.items()}


def _points_for(owner: QdrantOwner, project_id: str, rel_path: str) -> int:
    collection = owner._router.write_collection(project_id)
    points, _ = owner._client.scroll(collection_name=collection, limit=10_000,
                                     with_payload=True)
    return sum(1 for p in points
               if (p.payload or {}).get("file_path") == rel_path)


def _state_row(owner: QdrantOwner, rel_path: str):
    state = IndexState(owner._settings.state_db)
    try:
        return state.get(rel_path)
    finally:
        state.close()


# ---------------------------------------------------------------------------
# Simulating the watcher and the restart
# ---------------------------------------------------------------------------


def _watcher_tick(owner: QdrantOwner, project_id: str, changes) -> dict:
    """What the watcher does for one project when the filesystem moves.

    Mirrors ``WatcherThread._run_multi_root``'s per-project reaction: index, and
    if the answer is ``busy``, hand the changes to the durable ledger. The
    ``getattr`` is the negative control — against the pre-fix tree there is
    nothing to hand them to and the ``busy`` answer is discarded, which is
    precisely the behaviour under test.

    Called on the rebuild's own thread rather than from a second one: the index
    mutex is a plain ``Lock``, so a re-entrant acquire fails exactly as a
    cross-thread one does, and the test stays deterministic.
    """
    stats = owner.run_incremental_index(project_id=project_id)
    if not stats.get("busy"):
        return stats
    capture = getattr(owner, "capture_pending_changes", None)
    if capture is None:
        return stats
    return capture(project_id, changes)


def _recover_after_restart(owner: QdrantOwner) -> None:
    """The service's boot-time recovery, and nothing else.

    Deliberately NOT a full startup sync: re-hashing every file of every
    project would restore currency by brute force and hide whether the captured
    work survived at all, which is the claim under test.
    """
    replay = getattr(owner, "replay_pending_changes", None)
    if replay is not None:
        replay()


def _fire_during(monkeypatch, target_project: str, action) -> None:
    """Run ``action`` once, immediately after the rebuild has indexed a file of
    ``target_project`` — i.e. while the rebuild still holds the index mutex."""
    from ragtools.service import owner as owner_module

    real = owner_module.index_file
    fired: list = []

    def _patched(*args, **kwargs):
        count = real(*args, **kwargs)
        if kwargs.get("project_id") == target_project and not fired:
            fired.append(True)
            action()
        return count

    monkeypatch.setattr(owner_module, "index_file", _patched)


# ---------------------------------------------------------------------------
# W-1 — the defect itself
# ---------------------------------------------------------------------------


def test_a_file_edited_during_a_rebuild_is_current_afterwards(owner, monkeypatch):
    """W-1. The edit landed while alpha was being rebuilt, after the rebuild had
    already read that file. Pre-fix the store kept the pre-edit content and the
    state DB recorded the hash the rebuild read, so nothing was pending and
    nothing would ever look at the file again."""
    owner.run_full_index()
    edited = Path(owner._settings.projects[0].path) / "a.md"

    def _edit():
        edited.write_text(_body("alpha", "a.md", "v2-edited-mid-rebuild"),
                          encoding="utf-8")
        _watcher_tick(owner, "alpha", [("upsert", str(edited))])

    _fire_during(monkeypatch, "alpha", _edit)
    owner.rebuild()

    stored = _stored(owner, "alpha")
    assert "v2-edited-mid-rebuild" in stored.get("alpha/a.md", ""), (
        "the file edited while the rebuild was running is still stored with its "
        "pre-edit content, and nothing is pending that would ever correct it"
    )
    # And the state DB agrees, so a later incremental does not re-do the work.
    row = _state_row(owner, "alpha/a.md")
    assert row is not None and row["file_hash"] == IndexState.hash_file(edited), (
        "the store was corrected but the state DB still records the pre-edit "
        "hash — the two must not disagree about what is indexed"
    )


def test_a_file_edited_during_a_shared_layout_rebuild_is_current_afterwards(monkeypatch):
    """W-1 again, on the DEFAULT layout.

    ``shared`` takes a different route through the rebuild —
    ``_rebuild_project_in_place`` streams through ``_flush_window`` instead of
    ``_index_into``, and there is no collection to swap — so it needs its own
    proof rather than an argument by analogy. The hook is on ``chunk_file``
    because that is what reads the file on this path.
    """
    from ragtools.service import owner as owner_module

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        owner = _build_owner(Path(td), strategy="shared")
        try:
            owner.run_full_index()
            edited = Path(owner._settings.projects[0].path) / "a.md"

            real_chunk = owner_module.chunk_file
            fired: list = []

            def _patched(*args, **kwargs):
                chunks = real_chunk(*args, **kwargs)
                if kwargs.get("project_id") == "alpha" and not fired:
                    fired.append(True)
                    edited.write_text(_body("alpha", "a.md", "v2-shared-layout"),
                                      encoding="utf-8")
                    _watcher_tick(owner, "alpha", [("upsert", str(edited))])
                return chunks

            monkeypatch.setattr(owner_module, "chunk_file", _patched)
            owner.rebuild()
            monkeypatch.setattr(owner_module, "chunk_file", real_chunk)

            assert fired, "the hook never ran; the test proved nothing"
            assert "v2-shared-layout" in _stored(owner, "alpha").get("alpha/a.md", ""), (
                "on the default (shared) layout the file edited during the "
                "rebuild is still stored with its pre-edit content"
            )
        finally:
            owner.close()


# ---------------------------------------------------------------------------
# W-2 — the replay must not re-apply what the rebuild already absorbed
# ---------------------------------------------------------------------------


def test_an_edit_to_a_project_not_yet_rebuilt_is_not_double_applied(owner, monkeypatch):
    """W-2. ``delta`` is rebuilt last, so a change to it made while ``alpha`` is
    being rebuilt is picked up by delta's own scan. The replay must then find
    nothing to do rather than writing a second copy.

    ``alpha`` is edited in the same tick on purpose: it is the already-swapped
    project, and it is what makes this test fail against the pre-fix tree.
    """
    owner.run_full_index()
    alpha_file = Path(owner._settings.projects[0].path) / "a.md"
    delta_file = Path(owner._settings.projects[3].path) / "a.md"

    def _edit():
        delta_file.write_text(_body("delta", "a.md", "v2-before-its-turn"),
                              encoding="utf-8")
        alpha_file.write_text(_body("alpha", "a.md", "v2-after-its-turn"),
                              encoding="utf-8")
        _watcher_tick(owner, "delta", [("upsert", str(delta_file))])
        _watcher_tick(owner, "alpha", [("upsert", str(alpha_file))])

    _fire_during(monkeypatch, "alpha", _edit)
    stats = owner.rebuild()

    assert "v2-after-its-turn" in _stored(owner, "alpha").get("alpha/a.md", ""), (
        "the project that had already been swapped kept its pre-edit content"
    )
    assert "v2-before-its-turn" in _stored(owner, "delta").get("delta/a.md", ""), (
        "the project rebuilt after the edit did not pick it up from disk"
    )

    # Exactly one copy of the file's vectors — the rebuild's, not the rebuild's
    # plus the replay's.
    row = _state_row(owner, "delta/a.md")
    assert row is not None
    assert _points_for(owner, "delta", "delta/a.md") == row["chunk_count"], (
        "delta/a.md holds more vectors than its recorded chunk count — the "
        "replay re-applied a change the rebuild had already absorbed"
    )

    replayed = {r["project"]: r for r in stats.get("replayed", [])}
    assert replayed.get("delta", {}).get("indexed") == 0, (
        "the replay for delta did work; its change was already on disk when the "
        "rebuild scanned it, so there was nothing left to index"
    )


# ---------------------------------------------------------------------------
# W-3 / W-4 — deletes and moves
# ---------------------------------------------------------------------------


def test_a_file_deleted_during_a_rebuild_does_not_reappear(owner, monkeypatch):
    """W-3. The rebuild had already read the file into its replacement
    collection, so pre-fix the delete was simply never applied and search went
    on returning a file that no longer exists."""
    owner.run_full_index()
    doomed = Path(owner._settings.projects[0].path) / "a.md"

    def _delete():
        doomed.unlink()
        _watcher_tick(owner, "alpha", [("delete", str(doomed))])

    _fire_during(monkeypatch, "alpha", _delete)
    owner.rebuild()

    assert "alpha/a.md" not in _stored(owner, "alpha"), (
        "the file deleted while the rebuild was running is still in the store"
    )
    assert _state_row(owner, "alpha/a.md") is None, (
        "the deleted file still has a state row, so it is reported as indexed"
    )
    # Its neighbour is untouched — a delete must not cost the project its index.
    assert "alpha/b.md" in _stored(owner, "alpha")


def test_a_file_moved_during_a_rebuild_is_indexed_at_its_new_path_only(owner, monkeypatch):
    """W-4. A move is a delete of the old path plus a create of the new one, and
    pre-fix neither half was applied."""
    owner.run_full_index()
    project_dir = Path(owner._settings.projects[0].path)
    old_path = project_dir / "a.md"
    new_path = project_dir / "moved.md"

    def _move():
        old_path.rename(new_path)
        _watcher_tick(owner, "alpha", [("delete", str(old_path)),
                                       ("upsert", str(new_path))])

    _fire_during(monkeypatch, "alpha", _move)
    owner.rebuild()

    stored = _stored(owner, "alpha")
    assert "alpha/moved.md" in stored, (
        "the moved file was never indexed at its new path"
    )
    assert "alpha/a.md" not in stored, (
        "the moved file is still indexed at its old path as well — one file, "
        "two sets of vectors"
    )


# ---------------------------------------------------------------------------
# W-5 — durability
# ---------------------------------------------------------------------------


def test_captured_changes_survive_a_restart_mid_rebuild(monkeypatch):
    """W-5. The rebuild is interrupted after ``alpha`` has swapped and before it
    reaches the rest — a partial rebuild — and the process goes away with the
    change unreplayed. It has to still be there at the next start.

    The hook fires while ``beta`` is being rebuilt, so alpha's own post-swap
    replay has already run and returned nothing; only durability can save this
    change. ``KeyboardInterrupt`` escapes the per-project ``except Exception``,
    which is what makes it an interruption rather than a project failure.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        owner = _build_owner(root)
        settings = owner._settings
        # The store outlives the process; the owner does not. Reusing the client
        # is what "the vectors are still on disk" means for an in-memory engine.
        client = owner._client
        try:
            owner.run_full_index()
            edited = Path(settings.projects[0].path) / "a.md"

            def _edit_then_die():
                edited.write_text(_body("alpha", "a.md", "v2-survives-restart"),
                                  encoding="utf-8")
                _watcher_tick(owner, "alpha", [("upsert", str(edited))])
                raise KeyboardInterrupt("service killed mid-rebuild")

            _fire_during(monkeypatch, "beta", _edit_then_die)
            with pytest.raises(KeyboardInterrupt):
                owner.rebuild()
        finally:
            owner.close()

        # --- the service restarts ---
        restarted = QdrantOwner(settings=settings, client=client)
        try:
            _recover_after_restart(restarted)
            stored = _stored(restarted, "alpha")
            assert "v2-survives-restart" in stored.get("alpha/a.md", ""), (
                "the change captured before the process died was not replayed "
                "when it came back — the queue did not survive the restart"
            )
        finally:
            restarted.close()


# ---------------------------------------------------------------------------
# W-6 — the bound
# ---------------------------------------------------------------------------


def test_exceeding_the_bound_records_a_rescan_marker(monkeypatch):
    """W-6. Past the bound the queue must degrade to a recorded "this project
    needs a full re-scan", not start dropping events."""
    monkeypatch.setenv("RAG_PENDING_CHANGE_LIMIT", "2")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        owner = _build_owner(root)
        try:
            owner.run_full_index()
            project_dir = Path(owner._settings.projects[0].path)
            reports: list = []

            def _flood():
                changes = []
                for n in range(5):
                    path = project_dir / f"flood_{n}.md"
                    path.write_text(_body("alpha", f"flood_{n}.md", "v1-flood"),
                                    encoding="utf-8")
                    changes.append(("upsert", str(path)))
                _watcher_tick(owner, "alpha", changes)
                report = getattr(owner, "pending_changes_report", lambda: None)()
                reports.append(report)

            _fire_during(monkeypatch, "alpha", _flood)
            owner.rebuild()

            stored = _stored(owner, "alpha")
            for n in range(5):
                assert f"alpha/flood_{n}.md" in stored, (
                    f"flood_{n}.md changed during the rebuild and was dropped — "
                    f"exceeding the bound must degrade, never discard"
                )

            assert reports and reports[0] is not None
            assert "alpha" in reports[0]["rescan_required"], (
                "the bound was exceeded but nothing recorded that the project "
                "needs a full re-scan"
            )
        finally:
            owner.close()


# ---------------------------------------------------------------------------
# W-7 — a failed replay is loud, and its work is kept
# ---------------------------------------------------------------------------


def test_a_replay_failure_is_surfaced_and_its_work_is_kept(owner, monkeypatch):
    """W-7. A replay that cannot run leaves the project serving content the user
    has already changed. That must be reported, and the work must survive to be
    retried — never swallowed."""
    owner.run_full_index()
    edited = Path(owner._settings.projects[0].path) / "a.md"

    def _edit():
        edited.write_text(_body("alpha", "a.md", "v2-replay-failed-first"),
                          encoding="utf-8")
        _watcher_tick(owner, "alpha", [("upsert", str(edited))])

    _fire_during(monkeypatch, "alpha", _edit)

    # Only the replay reaches the locked indexer during a per-project rebuild:
    # `_rebuild_project` indexes through `_index_into`, and the watcher tick is
    # turned away by the mutex before it gets this far.
    real_locked = QdrantOwner._run_incremental_index_locked

    def _boom(self, project_id=None, progress=None):
        if project_id == "alpha":
            raise RuntimeError("[WinError 10048] simulated transient failure")
        return real_locked(self, project_id, progress)

    monkeypatch.setattr(QdrantOwner, "_run_incremental_index_locked", _boom)
    stats = owner.rebuild()

    assert "alpha" in stats.get("replay_failures", []), (
        "the rebuild finished without reporting that it could not replay the "
        "changes made to alpha while it ran — a silent swallow"
    )

    # The work was kept, so a retry fixes it.
    monkeypatch.setattr(QdrantOwner, "_run_incremental_index_locked", real_locked)
    _recover_after_restart(owner)
    assert "v2-replay-failed-first" in _stored(owner, "alpha").get("alpha/a.md", ""), (
        "the failed replay discarded its work instead of keeping it for a retry"
    )


# ---------------------------------------------------------------------------
# W-8 — the watcher is actually wired to this
# ---------------------------------------------------------------------------


def test_the_watcher_queues_changes_it_could_not_index(tmp_path, monkeypatch):
    """W-8. Everything above simulates the watcher's reaction; this drives the
    real loop with a stubbed ``watch`` and pins the wiring.

    Pre-fix the ``busy`` answer was read for ``indexed``/``deleted`` counts and
    then thrown away."""
    from watchfiles import Change

    from ragtools.service import watcher_thread as wt

    project_dir = tmp_path / "alpha"
    project_dir.mkdir()
    changed = project_dir / "a.md"
    changed.write_text(_body("alpha", "a.md", "v2"), encoding="utf-8")

    settings = Settings(
        content_root=str(tmp_path),
        qdrant_path=str(tmp_path / "qdrant"),
        state_db=str(tmp_path / "state.db"),
        data_dir=str(tmp_path / "data"),
        projects=[ProjectConfig(id="alpha", path=str(project_dir), mode="docs")],
    )
    owner = MagicMock()
    owner.run_incremental_index.return_value = {
        "indexed": 0, "skipped": 0, "deleted": 0, "chunks_indexed": 0,
        "projects": [], "busy": True,
    }

    def _one_batch(*paths, **kwargs):
        yield {(Change.modified, str(changed))}

    monkeypatch.setattr(wt, "watch", _one_batch)
    wt.WatcherThread(owner=owner, settings=settings)._run_multi_root(
        lambda *a, **k: None)

    assert owner.capture_pending_changes.called, (
        "the watcher was told an index run was in progress and discarded the "
        "change; there is no next tick to fall back on"
    )
    project_id, queued = owner.capture_pending_changes.call_args[0]
    assert project_id == "alpha", "the queued change lost its project identity"
    assert [p for _kind, p in queued] == [str(changed)], (
        "the queued change lost its file identity"
    )
