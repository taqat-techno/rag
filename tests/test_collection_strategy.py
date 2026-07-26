"""`collection_strategy` selects where the ONE indexing pipeline writes.

This file originally covered ``index_by_strategy`` — a dispatcher choosing
between the shared pipeline and a separate ``run_per_project_index``. Both are
gone. The strategy is now resolved by
:class:`~ragtools.collection_router.CollectionRouter`, and ``run_full_index`` /
``run_incremental_index`` route through it, so per-project indexing is not a
second pipeline to dispatch to — it is the same pipeline with a different
answer to "which collection".

Removing the duplicate closed a real hazard, not just duplication: the second
per-project indexer had no state tracking (hence no incremental runs and no
delete detection), no progress or cancellation, and no redaction / source-class
tagging. Anything that reached it silently lost those guarantees.

The original coverage is preserved here against the surviving pipeline.

Plan: docs/planning/RAG_COLLECTION_ARCHITECTURE_IMPLEMENTATION_PLAN.md (W11)
"""

import tempfile
from pathlib import Path

import pytest

from ragtools.collection_router import CollectionRouter
from ragtools.config import ProjectConfig, Settings
from ragtools.indexing.indexer import run_full_index
from ragtools.registry import ProjectRegistry


def _project(root: Path, name: str, body: str) -> Path:
    d = root / name
    d.mkdir()
    (d / "doc.md").write_text(f"# {name}\n\n{body}\n", encoding="utf-8")
    return d


@pytest.fixture
def env():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        a = _project(root, "alpha", "Invoices reconcile nightly against the ledger.")
        b = _project(root, "beta", "Satellite downlink windows are scheduled ahead.")
        yield root, a, b


def _settings(root, a, b, strategy):
    return Settings(
        content_root=str(root),
        qdrant_path=str(root / f"qdrant-{strategy}"),
        state_db=str(root / f"state-{strategy}.db"),
        data_dir=str(root / f"data-{strategy}"),
        collection_strategy=strategy,
        projects=[
            ProjectConfig(id="alpha", path=str(a), mode="docs"),
            ProjectConfig(id="beta", path=str(b), mode="docs"),
        ],
    )


def _colls(client):
    return {c.name for c in client.get_collections().collections}


def test_shared_strategy_writes_one_collection(env):
    root, a, b = env
    settings = _settings(root, a, b, "shared")
    client = Settings.get_memory_client()

    stats = run_full_index(settings, client=client)
    assert stats["files_indexed"] == 2
    assert _colls(client) == {settings.collection_name}


def test_per_project_strategy_writes_one_collection_per_project(env):
    root, a, b = env
    settings = _settings(root, a, b, "per_project")
    client = Settings.get_memory_client()

    run_full_index(settings, client=client)

    with ProjectRegistry(str(Path(settings.data_dir) / "registry.db")) as reg:
        router = CollectionRouter(settings, registry=reg)
        alpha = router.write_collection("alpha")
        beta = router.write_collection("beta")
        assert alpha != beta
        assert {alpha, beta} <= _colls(client)
        assert client.count(alpha, exact=True).count > 0
        assert client.count(beta, exact=True).count > 0


def test_per_project_content_does_not_mix(env):
    """Alpha's collection must hold only alpha's chunks."""
    root, a, b = env
    settings = _settings(root, a, b, "per_project")
    client = Settings.get_memory_client()
    run_full_index(settings, client=client)

    with ProjectRegistry(str(Path(settings.data_dir) / "registry.db")) as reg:
        router = CollectionRouter(settings, registry=reg)
        for pid in ("alpha", "beta"):
            points, _ = client.scroll(collection_name=router.write_collection(pid),
                                      limit=100, with_payload=True)
            assert points, f"{pid} collection is empty"
            assert {p.payload["project_id"] for p in points} == {pid}


def test_an_unknown_strategy_is_refused_not_silently_shared(env):
    root, a, b = env
    settings = _settings(root, a, b, "shared")
    object.__setattr__(settings, "collection_strategy", "sharded")
    with pytest.raises(ValueError, match="collection_strategy"):
        run_full_index(settings, client=Settings.get_memory_client())


def test_the_two_strategies_index_the_same_files(env):
    """Only the destination differs — not what gets indexed."""
    root, a, b = env
    shared = run_full_index(_settings(root, a, b, "shared"),
                            client=Settings.get_memory_client())
    per = run_full_index(_settings(root, a, b, "per_project"),
                         client=Settings.get_memory_client())
    assert shared["files_indexed"] == per["files_indexed"]
    assert shared["chunks_indexed"] == per["chunks_indexed"]
    assert shared["projects"] == per["projects"]


def test_the_removed_duplicate_pipeline_is_really_gone():
    """Guards against reintroducing a second per-project indexer."""
    from ragtools.indexing import indexer

    assert not hasattr(indexer, "index_by_strategy")
    assert not hasattr(indexer, "run_per_project_index")
