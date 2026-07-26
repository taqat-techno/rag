"""Indexing must stream, not accumulate.

Both indexers used to be two-phase: chunk EVERY file into one list, then encode
and upsert in windows. Peak memory was therefore O(corpus) — measured at
**2.46 GB** re-indexing a 38,286-file project — and nothing was durable until
the whole corpus had been chunked, so the point count sat flat for ~15 minutes
and an interruption lost everything.

A 38k-file project is not large. The same shape at 150k files does not fit in
memory, and the failure mode is an OOM kill mid-index.

Peak-RSS assertions are flaky, so the property is pinned structurally: work must
INTERLEAVE (chunk … write … chunk … write) and no more than one window of files
may be held at a time. A generous tracemalloc check backs that up.

Plan: docs/planning/RAG_STABILITY_HARDENING_PLAN.md (H1, G2)
"""

import tempfile
import tracemalloc
from pathlib import Path

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.service import owner as owner_module
from ragtools.service.owner import QdrantOwner

# Each file is deliberately fat enough to make several chunks, so accumulation
# would be visible in memory.
_BODY = "\n\n".join(
    f"## Section {s}\n\n" + ("Sentence about indexing behaviour. " * 40)
    for s in range(6)
)


def _make(tmp: Path, n_files: int, strategy: str = "shared"):
    proj = tmp / "proj"
    proj.mkdir(exist_ok=True)
    for i in range(n_files):
        (proj / f"doc{i}.md").write_text(f"# Doc {i}\n\n{_BODY}\n", encoding="utf-8")
    settings = Settings(
        content_root=str(tmp),
        qdrant_path=str(tmp / "qdrant"),
        state_db=str(tmp / "state.db"),
        data_dir=str(tmp / "data"),
        collection_strategy=strategy,
        projects=[ProjectConfig(id="p", path=str(proj), mode="docs")],
    )
    return QdrantOwner(settings=settings, client=Settings.get_memory_client())


def _record_call_order(owner, monkeypatch):
    """Log 'chunk' / 'write' in the order they happen."""
    order = []
    real_chunk = owner_module.chunk_file
    real_upsert = owner_module.upsert_points

    def chunk_spy(**kw):
        order.append("chunk")
        return real_chunk(**kw)

    def upsert_spy(client, collection, points):
        order.append("write")
        return real_upsert(client, collection, points)

    monkeypatch.setattr(owner_module, "chunk_file", chunk_spy)
    monkeypatch.setattr(owner_module, "upsert_points", upsert_spy)
    return order


# --- the property ------------------------------------------------------


@pytest.mark.parametrize("method", ["run_full_index", "run_incremental_index"])
def test_work_is_interleaved_not_accumulated(monkeypatch, method):
    """Accumulating shows as chunk×ALL then write×N. Streaming alternates."""
    n = owner_module._INDEX_BATCH_SIZE * 3
    with tempfile.TemporaryDirectory() as td:
        owner = _make(Path(td), n)
        try:
            order = _record_call_order(owner, monkeypatch)
            getattr(owner, method)()

            assert "write" in order, "nothing was written"
            chunks_before_first_write = order.index("write")
            assert chunks_before_first_write <= owner_module._INDEX_BATCH_SIZE, (
                f"{chunks_before_first_write} files were chunked before the first "
                f"write — the whole corpus is being buffered "
                f"(window is {owner_module._INDEX_BATCH_SIZE})"
            )
            # And chunking continues AFTER the first write.
            assert "chunk" in order[order.index("write"):], (
                "all chunking finished before any writing — not streaming"
            )
        finally:
            owner.close()


def test_no_more_than_one_window_of_chunks_is_alive(monkeypatch):
    """The direct memory bound: live chunk-lists never exceed one window."""
    n = owner_module._INDEX_BATCH_SIZE * 3
    with tempfile.TemporaryDirectory() as td:
        owner = _make(Path(td), n)
        try:
            live = {"count": 0, "peak": 0}
            real_chunk = owner_module.chunk_file
            real_upsert = owner_module.upsert_points

            def chunk_spy(**kw):
                live["count"] += 1
                live["peak"] = max(live["peak"], live["count"])
                return real_chunk(**kw)

            def upsert_spy(client, collection, points):
                # A write means the window just landed and is about to be freed.
                live["count"] = 0
                return real_upsert(client, collection, points)

            monkeypatch.setattr(owner_module, "chunk_file", chunk_spy)
            monkeypatch.setattr(owner_module, "upsert_points", upsert_spy)

            owner.run_full_index()

            assert live["peak"] <= owner_module._INDEX_BATCH_SIZE, (
                f"{live['peak']} files held at once for a {n}-file corpus; the "
                f"window is {owner_module._INDEX_BATCH_SIZE}"
            )
        finally:
            owner.close()


def test_peak_memory_does_not_scale_with_corpus_size():
    """4x the files must not cost ~4x the peak. Generous bound — this guards
    the O(corpus) regression, not a precise figure."""
    def peak_for(n):
        with tempfile.TemporaryDirectory() as td:
            owner = _make(Path(td), n)
            try:
                tracemalloc.start()
                owner.run_full_index()
                _cur, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
                owner.close()
        return peak

    small = peak_for(25)
    large = peak_for(100)          # 4x the corpus
    assert large < small * 2.5, (
        f"peak memory grew from {small/1e6:.1f} MB to {large/1e6:.1f} MB for 4x "
        "the files — memory still scales with corpus size"
    )


# --- streaming must not change results ---------------------------------


def test_streaming_indexes_everything_exactly_once():
    with tempfile.TemporaryDirectory() as td:
        owner = _make(Path(td), 40)
        try:
            stats = owner.run_full_index()
            assert stats["files_indexed"] == 40
            status = owner.get_status()
            assert status["points_count"] == stats["chunks_indexed"] > 0
            assert status["total_files"] == 40
        finally:
            owner.close()


def test_a_second_run_is_a_no_op():
    with tempfile.TemporaryDirectory() as td:
        owner = _make(Path(td), 30)
        try:
            owner.run_full_index()
            again = owner.run_incremental_index()
            assert again["indexed"] == 0
            assert again["skipped"] == 30
        finally:
            owner.close()


def test_work_is_durable_per_window_not_only_at_the_end():
    """An interruption must keep the windows already committed."""
    n = owner_module._INDEX_BATCH_SIZE * 3
    with tempfile.TemporaryDirectory() as td:
        owner = _make(Path(td), n)
        try:
            real_upsert = owner_module.upsert_points
            writes = {"n": 0}

            class Stop(RuntimeError):
                pass

            def upsert_spy(client, collection, points):
                writes["n"] += 1
                if writes["n"] > 2:
                    raise Stop("simulated interruption")
                return real_upsert(client, collection, points)

            owner_module.upsert_points = upsert_spy
            try:
                with pytest.raises(Stop):
                    owner.run_full_index()
            finally:
                owner_module.upsert_points = real_upsert

            # The committed windows survived.
            partial = owner.get_status()["points_count"]
            assert partial > 0, "an interruption lost every window"

            # And a follow-up run completes the job.
            owner.run_incremental_index()
            assert owner.get_status()["total_files"] == n
        finally:
            owner.close()


# --- full index is now delete-aware (S3) --------------------------------


def test_full_index_removes_files_deleted_from_disk():
    """It used to only write what it found, so a deleted file's vectors stayed
    for ever and search kept returning a file that no longer existed."""
    with tempfile.TemporaryDirectory() as td:
        owner = _make(Path(td), 12)
        try:
            owner.run_full_index()
            before = owner.get_status()

            for i in range(4):
                (Path(owner.settings.projects[0].path) / f"doc{i}.md").unlink()

            stats = owner.run_full_index()
            after = owner.get_status()

            assert stats["files_indexed"] == 8
            assert after["total_files"] == 8, "state still tracks the deleted files"
            assert after["points_count"] < before["points_count"], (
                "vectors for the deleted files are still in the collection"
            )
        finally:
            owner.close()


@pytest.mark.parametrize("method", ["run_full_index", "run_incremental_index"])
def test_a_shrunk_file_leaves_no_orphan_chunks(method):
    """Deterministic chunk ids overwrite, but a file that SHRANK leaves its
    higher-index chunks behind unless they are deleted first."""
    with tempfile.TemporaryDirectory() as td:
        owner = _make(Path(td), 6)
        try:
            owner.run_full_index()
            big = owner.get_status()["points_count"]

            target = Path(owner.settings.projects[0].path) / "doc0.md"
            target.write_text("# Doc 0\n\nOne short line only.\n", encoding="utf-8")

            getattr(owner, method)()
            assert owner.get_status()["points_count"] < big, (
                "the shrunk file kept its old chunks"
            )
        finally:
            owner.close()
