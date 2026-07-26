"""Only one indexing run at a time, whoever asks.

The job engine serialises index JOBS, but the watcher thread calls
``run_incremental_index`` directly and bypasses that queue. That was harmless
while the watcher's runs skipped everything and finished in milliseconds.

It stopped being harmless once a storage/layout change forced a genuine
re-index: the submitted job and the watcher tick both re-chunked all 38,286
files at the same time. The process pegged a core with a 1.7 GB working set and
uvicorn's event loop was starved so completely that EVERY endpoint timed out at
12s — ``/health``, ``/identity``, ``/api/jobs``, ``/api/status``. From the
outside the service looked dead; it was doing the same work twice.

A second caller is now told it was skipped rather than queued: a watcher tick
that lands during a long re-index has nothing to add, and the next tick picks up
whatever changed.

Plan: docs/planning/RAG_COLLECTION_ARCHITECTURE_IMPLEMENTATION_PLAN.md (W10)
"""

import tempfile
import threading
import time
from pathlib import Path

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.service.owner import QdrantOwner


@pytest.fixture
def owner():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj = root / "proj"
        proj.mkdir()
        for i in range(8):
            (proj / f"doc{i}.md").write_text(
                f"# Doc {i}\n\nContent for document {i}.\n", encoding="utf-8")
        settings = Settings(
            content_root=str(root),
            qdrant_path=str(root / "qdrant"),
            state_db=str(root / "state.db"),
            data_dir=str(root / "data"),
            projects=[ProjectConfig(id="p", path=str(proj), mode="docs")],
        )
        o = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        yield o
        o.close()


def test_nothing_is_indexing_at_rest(owner):
    assert owner.indexing is False


def test_a_concurrent_incremental_is_skipped_not_queued(owner):
    """The watcher-vs-job collision, reproduced deterministically."""
    owner.run_full_index()

    started = threading.Event()
    release = threading.Event()
    result = {}

    # Hold the mutex from a "job", then have the "watcher" try to run.
    def long_running():
        with owner._exclusive_index("test") as acquired:
            assert acquired is True
            started.set()
            release.wait(10)

    t = threading.Thread(target=long_running, daemon=True)
    t.start()
    assert started.wait(5)
    try:
        assert owner.indexing is True
        t0 = time.perf_counter()
        result = owner.run_incremental_index()
        elapsed = time.perf_counter() - t0

        assert result.get("busy") is True, "the second run was not reported as skipped"
        assert result["indexed"] == 0
        assert elapsed < 2.0, (
            f"the second caller blocked for {elapsed:.1f}s — it queued behind a "
            "run that may take half an hour"
        )
    finally:
        release.set()
        t.join(timeout=5)

    assert owner.indexing is False


def test_a_concurrent_full_index_is_skipped_too(owner):
    started = threading.Event()
    release = threading.Event()

    def long_running():
        with owner._exclusive_index("test") as acquired:
            started.set()
            release.wait(10)

    t = threading.Thread(target=long_running, daemon=True)
    t.start()
    assert started.wait(5)
    try:
        stats = owner.run_full_index()
        assert stats.get("busy") is True
        assert stats["files_indexed"] == 0
    finally:
        release.set()
        t.join(timeout=5)


def test_the_mutex_is_released_even_when_indexing_raises(owner):
    """A crashed run must not wedge every future index."""
    try:
        with owner._exclusive_index("test") as acquired:
            assert acquired is True
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert owner.indexing is False
    stats = owner.run_incremental_index()
    assert not stats.get("busy"), "the mutex leaked after a failure"


def test_sequential_runs_are_unaffected(owner):
    """Exclusion must not make ordinary back-to-back indexing skip."""
    first = owner.run_full_index()
    assert first["files_indexed"] == 8
    assert not first.get("busy")

    second = owner.run_incremental_index()
    assert not second.get("busy")
    assert second["skipped"] == 8

    third = owner.run_incremental_index()
    assert not third.get("busy")


def test_search_still_works_while_an_index_holds_the_mutex(owner):
    """The mutex must gate indexing only — search never took this lock."""
    owner.run_full_index()

    started = threading.Event()
    release = threading.Event()

    def long_running():
        with owner._exclusive_index("test"):
            started.set()
            release.wait(10)

    t = threading.Thread(target=long_running, daemon=True)
    t.start()
    assert started.wait(5)
    try:
        t0 = time.perf_counter()
        hits = owner.search("document content", project_id="p", top_k=5)
        assert time.perf_counter() - t0 < 5.0
        assert hits
    finally:
        release.set()
        t.join(timeout=5)
