"""Changing the collection layout must never leave the machine without an index.

A layout change points ragtools at a *different, empty* store. The state DB
still describes the previous one, so an incremental run reads "38,286 files
already indexed", skips every one, and reports success against a store holding a
fraction of them — observed exactly once, with ~28k chunks silently missing.
`index_identity` exists to stop that, and these tests pin the property that
makes the cutover survivable:

    the previous index stays intact and queryable until the new layout has been
    built, so an interrupted or unsatisfactory cutover is a decision to reverse
    rather than an outage to recover from.

That property currently holds *by construction* — the new layout writes to new
collections and nothing drops the old one — which is worth asserting precisely
because nothing in the code says so out loud. A future "tidy up the old
collection first" would look like an improvement and would remove the only
rollback the user has.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.service.owner import QdrantOwner


@pytest.fixture
def corpus(tmp_path):
    project = tmp_path / "alpha"
    project.mkdir()
    (project / "a.md").write_text(
        "# Alpha\n\nContent about widgets and gears.", encoding="utf-8")
    (project / "b.md").write_text(
        "# Beta\n\nMore content, about sprockets.", encoding="utf-8")
    return tmp_path, project


def owner_for(root: Path, project: Path, strategy: str):
    settings = Settings(
        qdrant_path=str(root / "qdrant"),
        state_db=str(root / "state.db"),
        collection_strategy=strategy,
        projects=[ProjectConfig(id="alpha", path=str(project), mode="docs")],
    )
    return QdrantOwner(settings), settings


def test_the_previous_index_survives_the_cutover(corpus):
    """The rollback the user actually has.

    If the new layout is wrong, incomplete, or simply not what they wanted,
    switching `collection_strategy` back must find the old index still there.
    """
    root, project = corpus

    old, settings = owner_for(root, project, "shared")
    old.run_full_index()
    shared_name = settings.collection_name
    before = old._count_points(shared_name)
    assert before > 0, "the fixture indexed nothing"
    old.close()

    new, _ = owner_for(root, project, "per_project")
    new.run_full_index()
    try:
        after = new._count_points(shared_name)
        assert after == before, (
            "the cutover destroyed the previous index, so there is nothing to "
            "roll back to if the new layout is not what the user wanted"
        )
    finally:
        new.close()


def test_the_new_layout_is_actually_populated(corpus):
    """Preserving the old index would be worthless if the new one were empty."""
    root, project = corpus

    old, _ = owner_for(root, project, "shared")
    old.run_full_index()
    old.close()

    new, _ = owner_for(root, project, "per_project")
    new.run_full_index()
    try:
        per_collection = {name: new._count_points(name)
                          for name in new.router.all_collections()}
        assert per_collection, "the per-project layout created no collections"
        assert sum(per_collection.values()) > 0, (
            f"the new layout is empty after a full index: {per_collection}"
        )
    finally:
        new.close()


def test_switching_back_restores_the_original_layout(corpus):
    """Rollback is a configuration change, not a restore procedure."""
    root, project = corpus

    first, settings = owner_for(root, project, "shared")
    first.run_full_index()
    shared_name = settings.collection_name
    original = first._count_points(shared_name)
    first.close()

    forward, _ = owner_for(root, project, "per_project")
    forward.run_full_index()
    forward.close()

    back, _ = owner_for(root, project, "shared")
    try:
        assert back._count_points(shared_name) == original
        assert back.router.strategy == "shared"
    finally:
        back.close()


def test_the_state_db_is_distrusted_exactly_once_across_the_cutover(corpus):
    """The failure this guards: file hashes that describe the PREVIOUS store.

    Trusting them skips every file against an empty collection. Distrust must
    fire on the layout change and stop firing once the re-index has stamped the
    new identity — otherwise every subsequent start re-embeds the whole corpus.
    """
    from ragtools.index_identity import current_identity, reconcile
    from ragtools.indexing.state import IndexState

    root, project = corpus

    old, _ = owner_for(root, project, "shared")
    old.run_full_index()
    old.close()

    new, settings = owner_for(root, project, "per_project")
    try:
        state = IndexState(settings.state_db)
        try:
            identity = current_identity(settings, new._encoder.dimension)
            trustworthy, changed = reconcile(state, identity)
            assert not trustworthy, "the layout change did not force a re-index"
            assert any("collection_strategy" in c for c in changed), changed
        finally:
            state.close()

        new.run_full_index()          # stamps the new identity

        state = IndexState(settings.state_db)
        try:
            identity = current_identity(settings, new._encoder.dimension)
            trustworthy, changed = reconcile(state, identity)
            assert trustworthy, (
                "the re-index repeats on every start — it is triggered more "
                "than once"
            )
        finally:
            state.close()
    finally:
        new.close()


# --- reclaiming the previous layout, deliberately as a separate step ------


def _reclaim(root: Path, project: Path, strategy: str, **kwargs):
    """Invoke `rag storage reclaim` against an isolated config."""
    from typer.testing import CliRunner

    import ragtools.cli as cli_module
    from ragtools.cli import app

    settings = Settings(
        qdrant_path=str(root / "qdrant"),
        state_db=str(root / "state.db"),
        collection_strategy=strategy,
        projects=[ProjectConfig(id="alpha", path=str(project), mode="docs")],
    )
    original_get, original_probe = cli_module._get_settings, cli_module._probe_service
    cli_module._get_settings = lambda: settings
    cli_module._probe_service = lambda *a, **k: False
    try:
        return CliRunner().invoke(app, ["storage", "reclaim", *kwargs.get("args", [])])
    finally:
        cli_module._get_settings = original_get
        cli_module._probe_service = original_probe


def test_reclaim_refuses_while_the_new_layout_is_empty(corpus):
    """An index that has not been built is not one that has been validated.

    Dropping the previous collections at that moment leaves the machine with
    nothing to search — which is the exact outcome the whole cutover design
    exists to make impossible.
    """
    root, project = corpus

    old, _ = owner_for(root, project, "shared")
    old.run_full_index()
    old.close()

    # Switch layout, but do NOT re-index.
    result = _reclaim(root, project, "per_project", args=["--yes"])

    assert result.exit_code == 1, result.output
    assert "Refusing" in result.output
    assert "rag index" in result.output, "the refusal does not say how to proceed"


def test_reclaim_drops_the_previous_layout_once_the_new_one_is_built(corpus):
    root, project = corpus

    old, settings = owner_for(root, project, "shared")
    old.run_full_index()
    shared_name = settings.collection_name
    old.close()

    new, _ = owner_for(root, project, "per_project")
    new.run_full_index()
    new.close()

    result = _reclaim(root, project, "per_project", args=["--yes"])
    assert result.exit_code == 0, result.output
    assert shared_name in result.output

    check, _ = owner_for(root, project, "per_project")
    try:
        remaining = {c.name for c in check._client.get_collections().collections}
        assert shared_name not in remaining, "the orphaned collection survived"
        assert sum(check._count_points(n) for n in check.router.all_collections()) > 0
    finally:
        check.close()


def test_reclaim_never_touches_a_collection_the_layout_uses(corpus):
    """Reclaiming must be incapable of deleting the live index."""
    root, project = corpus

    owner, settings = owner_for(root, project, "shared")
    owner.run_full_index()
    owner.close()

    result = _reclaim(root, project, "shared", args=["--yes"])
    assert result.exit_code == 0, result.output
    assert "Nothing to reclaim" in result.output

    check, _ = owner_for(root, project, "shared")
    try:
        assert check._count_points(settings.collection_name) > 0
    finally:
        check.close()
