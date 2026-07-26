"""Indexing must not open one connection per file.

Found by indexing a real 38,286-file project against a managed Qdrant server:
the job died at 29,730 files with

    ResponseHandlingException: [WinError 10048] Only one usage of each socket
    address (protocol/network address/port) is normally permitted

That is ephemeral-port exhaustion. The indexer called ``upsert_points`` once per
FILE, and each call is an HTTP request to the server; sockets linger in
TIME_WAIT, so a long run walks the whole dynamic port range and then cannot open
another. It never appeared on the embedded engine, where "upsert" is an
in-process call and costs no socket at all — so moving to a real server turned a
harmless inefficiency into a hard failure at scale.

The fix is to group each batch's points by destination collection and upsert
once per collection. These tests pin the bound so per-file upserts cannot creep
back in.

Plan: docs/planning/RAG_COLLECTION_ARCHITECTURE_IMPLEMENTATION_PLAN.md (W10)
"""

import tempfile
from pathlib import Path

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.service import owner as owner_module
from ragtools.service.owner import QdrantOwner

FILES_PER_PROJECT = 40


class _CountingClient:
    """Wraps the in-memory client and counts the calls that cost a round-trip.

    Against a managed/external server every one of these is an HTTP request, so
    the counts here stand in for sockets consumed.
    """

    def __init__(self, inner):
        self._inner = inner
        self.upsert_calls = 0
        self.delete_calls = 0

    def upsert(self, *a, **kw):
        self.upsert_calls += 1
        return self._inner.upsert(*a, **kw)

    def delete(self, *a, **kw):
        self.delete_calls += 1
        return self._inner.delete(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _make(tmp: Path, strategy: str):
    projects = []
    for name in ("alpha", "beta"):
        d = tmp / name
        d.mkdir()
        for i in range(FILES_PER_PROJECT):
            (d / f"doc{i}.md").write_text(
                f"# {name} {i}\n\nContent block number {i} for project {name}.\n",
                encoding="utf-8")
        projects.append(ProjectConfig(id=name, path=str(d), mode="docs"))

    settings = Settings(
        content_root=str(tmp),
        qdrant_path=str(tmp / "qdrant"),
        state_db=str(tmp / "state.db"),
        data_dir=str(tmp / "data"),
        collection_strategy=strategy,
        projects=projects,
    )
    client = _CountingClient(Settings.get_memory_client())
    return QdrantOwner(settings=settings, client=client), client


@pytest.mark.parametrize("strategy", ["shared", "per_project"])
def test_full_index_upserts_per_batch_not_per_file(strategy):
    """The bound: batches x collections, never files."""
    with tempfile.TemporaryDirectory() as td:
        owner, client = _make(Path(td), strategy)
        try:
            total_files = FILES_PER_PROJECT * 2
            batch_size = owner_module._INDEX_BATCH_SIZE
            collections = 2 if strategy == "per_project" else 1
            # ceil(files / batch) batches, at most `collections` upserts each.
            max_expected = -(-total_files // batch_size) * collections

            stats = owner.run_full_index()
            assert stats["files_indexed"] == total_files

            assert client.upsert_calls <= max_expected, (
                f"{client.upsert_calls} upserts for {total_files} files — "
                f"expected at most {max_expected} (one per collection per batch). "
                "Per-file upserts exhaust the ephemeral port range on a server."
            )
            assert client.upsert_calls < total_files, (
                "upsert count scales with FILES, which is the port-exhaustion bug"
            )
        finally:
            owner.close()


@pytest.mark.parametrize("strategy", ["shared", "per_project"])
def test_incremental_index_also_batches(strategy):
    with tempfile.TemporaryDirectory() as td:
        owner, client = _make(Path(td), strategy)
        try:
            owner.run_full_index()
            # Touch every file so the incremental run has real work to do.
            for p in owner.settings.projects:
                for f in Path(p.path).glob("*.md"):
                    f.write_text(f.read_text(encoding="utf-8") + "\nAppended line.\n",
                                 encoding="utf-8")

            client.upsert_calls = 0
            stats = owner.run_incremental_index()
            assert stats["indexed"] == FILES_PER_PROJECT * 2

            assert client.upsert_calls < stats["indexed"], (
                f"{client.upsert_calls} upserts for {stats['indexed']} changed "
                "files — incremental indexing is still per-file"
            )
        finally:
            owner.close()


@pytest.mark.parametrize("strategy", ["shared", "per_project"])
def test_incremental_deletes_are_batched_too(strategy):
    """The OTHER half of the port exhaustion.

    Batching the upserts was not enough: the incremental path still called
    ``delete_file_points`` once per changed file to clear its stale chunks, so a
    38,286-file re-index still made 38,286 delete requests. The failures came
    straight back as WinError 10053 after the upsert fix shipped.
    """
    with tempfile.TemporaryDirectory() as td:
        owner, client = _make(Path(td), strategy)
        try:
            owner.run_full_index()
            for p in owner.settings.projects:
                for f in Path(p.path).glob("*.md"):
                    f.write_text(f.read_text(encoding="utf-8") + "\nEdited.\n",
                                 encoding="utf-8")

            client.delete_calls = 0
            stats = owner.run_incremental_index()
            changed = stats["indexed"]
            assert changed == FILES_PER_PROJECT * 2

            assert client.delete_calls < changed, (
                f"{client.delete_calls} delete requests for {changed} changed "
                "files — still one round-trip per file"
            )
        finally:
            owner.close()


@pytest.mark.parametrize("strategy", ["shared", "per_project"])
def test_removing_many_files_is_batched(strategy):
    """Deleting files from disk must not cost one request each either."""
    with tempfile.TemporaryDirectory() as td:
        owner, client = _make(Path(td), strategy)
        try:
            owner.run_full_index()
            removed = 0
            for p in owner.settings.projects:
                for f in sorted(Path(p.path).glob("*.md"))[:20]:
                    f.unlink()
                    removed += 1

            client.delete_calls = 0
            stats = owner.run_incremental_index()
            assert stats["deleted"] == removed

            assert client.delete_calls < removed, (
                f"{client.delete_calls} delete requests to remove {removed} files"
            )
        finally:
            owner.close()


def test_batched_delete_actually_removes_every_named_path():
    """A batched delete that quietly dropped paths would orphan vectors."""
    from ragtools.indexing.indexer import delete_files_points

    with tempfile.TemporaryDirectory() as td:
        owner, _client = _make(Path(td), "per_project")
        try:
            owner.run_full_index()
            collection = owner.router.write_collection("alpha")
            before = owner._count_points(collection)

            # Read the paths Qdrant actually stored rather than assuming their
            # shape — the payload carries the resolved relative path.
            stored, _ = owner.client.scroll(collection_name=collection, limit=1000,
                                            with_payload=True)
            paths = sorted({p.payload["file_path"] for p in stored})[:5]
            assert len(paths) == 5

            n = delete_files_points(owner.client, collection, paths)
            assert n == 5

            after = owner._count_points(collection)
            assert after < before
            points, _ = owner.client.scroll(collection_name=collection, limit=1000,
                                            with_payload=True)
            remaining = {p.payload["file_path"] for p in points}
            assert not (remaining & set(paths)), "batched delete missed a path"
        finally:
            owner.close()


def test_batched_delete_chunks_large_inputs():
    """One unbounded filter for 38k paths would be a huge request body."""
    from ragtools.indexing import indexer

    calls = []

    class _Rec:
        def delete(self, **kw):
            calls.append(kw)

    indexer.delete_files_points(_Rec(), "c", [f"f{i}.md" for i in range(1000)])
    assert len(calls) == -(-1000 // indexer._DELETE_BATCH)
    for kw in calls:
        any_values = kw["points_selector"].must[0].match.any
        assert len(any_values) <= indexer._DELETE_BATCH


def test_batched_delete_is_a_no_op_for_an_empty_list():
    from ragtools.indexing import indexer

    class _Boom:
        def delete(self, **kw):
            raise AssertionError("should not issue a request for zero paths")

    assert indexer.delete_files_points(_Boom(), "c", []) == 0


def test_batching_does_not_lose_or_duplicate_chunks():
    """Grouping must preserve exactly the points a per-file upsert produced."""
    with tempfile.TemporaryDirectory() as td:
        owner, _client = _make(Path(td), "per_project")
        try:
            stats = owner.run_full_index()
            status = owner.get_status()
            assert status["points_count"] == stats["chunks_indexed"], (
                "points in Qdrant do not match the chunks the run reported"
            )
            # And each project's collection holds only its own points.
            for pid in ("alpha", "beta"):
                name = owner.router.write_collection(pid)
                points, _ = owner.client.scroll(collection_name=name, limit=1000,
                                                with_payload=True)
                assert points
                assert {p.payload["project_id"] for p in points} == {pid}
        finally:
            owner.close()


def test_a_shrinking_file_leaves_no_orphan_chunks():
    """Batched upserts must not skip the per-file delete that precedes them."""
    with tempfile.TemporaryDirectory() as td:
        owner, _client = _make(Path(td), "per_project")
        try:
            owner.run_full_index()
            target = Path(owner.settings.projects[0].path) / "doc0.md"
            target.write_text(
                "# alpha 0\n\n" + ("Long paragraph. " * 400) + "\n", encoding="utf-8")
            owner.run_incremental_index()
            grown = owner.get_status()["points_count"]

            target.write_text("# alpha 0\n\nTiny.\n", encoding="utf-8")
            owner.run_incremental_index()
            shrunk = owner.get_status()["points_count"]

            assert shrunk < grown, "chunks from the larger version were orphaned"
        finally:
            owner.close()
