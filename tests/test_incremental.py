"""Tests for incremental indexing and state tracking."""

import shutil
from pathlib import Path

import pytest

from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.indexing.indexer import (
    delete_file_points,
    ensure_collection,
    index_file,
    run_incremental_index,
)
from ragtools.indexing.state import IndexState


# --- IndexState Tests ---


class TestIndexState:
    @pytest.fixture
    def state(self, tmp_path):
        db_path = str(tmp_path / "test_state.db")
        s = IndexState(db_path)
        yield s
        s.close()

    def test_empty_state(self, state):
        assert state.get_all_paths() == set()
        assert state.get("nonexistent.md") is None

    def test_update_and_get(self, state):
        state.update("proj/file.md", "proj", "abc123", 5)
        record = state.get("proj/file.md")
        assert record is not None
        assert record["project_id"] == "proj"
        assert record["file_hash"] == "abc123"
        assert record["chunk_count"] == 5
        assert record["last_indexed"] is not None

    def test_file_changed_new(self, state):
        assert state.file_changed("proj/new.md", "hash1") is True

    def test_file_changed_same_hash(self, state):
        state.update("proj/file.md", "proj", "hash1", 3)
        assert state.file_changed("proj/file.md", "hash1") is False

    def test_file_changed_different_hash(self, state):
        state.update("proj/file.md", "proj", "hash1", 3)
        assert state.file_changed("proj/file.md", "hash2") is True

    def test_remove(self, state):
        state.update("proj/file.md", "proj", "hash1", 3)
        state.remove("proj/file.md")
        assert state.get("proj/file.md") is None
        assert "proj/file.md" not in state.get_all_paths()

    def test_get_all_paths(self, state):
        state.update("a/one.md", "a", "h1", 1)
        state.update("b/two.md", "b", "h2", 2)
        assert state.get_all_paths() == {"a/one.md", "b/two.md"}

    def test_get_all_for_project(self, state):
        state.update("a/one.md", "a", "h1", 1)
        state.update("a/two.md", "a", "h2", 2)
        state.update("b/three.md", "b", "h3", 3)
        records = state.get_all_for_project("a")
        assert len(records) == 2
        paths = {r["file_path"] for r in records}
        assert paths == {"a/one.md", "a/two.md"}

    def test_get_summary(self, state):
        state.update("a/one.md", "a", "h1", 5)
        state.update("b/two.md", "b", "h2", 3)
        summary = state.get_summary()
        assert summary["total_files"] == 2
        assert summary["total_chunks"] == 8
        assert sorted(summary["projects"]) == ["a", "b"]

    def test_update_replaces(self, state):
        state.update("proj/file.md", "proj", "hash1", 5)
        state.update("proj/file.md", "proj", "hash2", 3)
        record = state.get("proj/file.md")
        assert record["file_hash"] == "hash2"
        assert record["chunk_count"] == 3

    def test_hash_file(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("Hello world")
        h1 = IndexState.hash_file(f)
        h2 = IndexState.hash_file(f)
        assert h1 == h2
        assert len(h1) == 64  # SHA256 hex

    def test_hash_file_changes(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("version 1")
        h1 = IndexState.hash_file(f)
        f.write_text("version 2")
        h2 = IndexState.hash_file(f)
        assert h1 != h2

    def test_persistence(self, tmp_path):
        db_path = str(tmp_path / "persist.db")
        s1 = IndexState(db_path)
        s1.update("proj/file.md", "proj", "hash1", 5)
        s1.close()

        s2 = IndexState(db_path)
        record = s2.get("proj/file.md")
        assert record is not None
        assert record["file_hash"] == "hash1"
        s2.close()


# --- Delete File Points ---


class TestDeleteFilePoints:
    def test_delete_points_by_file(self):
        from ragtools.chunking.markdown import chunk_markdown_file

        client = Settings.get_memory_client()
        encoder = Encoder("all-MiniLM-L6-v2")
        ensure_collection(client, "del_test", encoder.dimension)

        fixtures = Path(__file__).parent / "fixtures"
        index_file(
            client=client,
            encoder=encoder,
            collection_name="del_test",
            project_id="project_a",
            file_path=fixtures / "project_a" / "README.md",
            relative_path="project_a/README.md",
        )

        info = client.get_collection("del_test")
        assert info.points_count > 0

        delete_file_points(client, "del_test", "project_a/README.md")

        info = client.get_collection("del_test")
        assert info.points_count == 0


# --- Incremental Indexing Integration ---


class TestIncrementalIndexing:
    """Integration tests for incremental indexing using a temp directory."""

    @pytest.fixture
    def work_dir(self, tmp_path):
        """Create a temp workspace with sample markdown files."""
        proj = tmp_path / "my_project"
        proj.mkdir()
        (proj / "doc1.md").write_text("# Doc 1\n\n## Section A\n\nContent of doc 1 section A.")
        (proj / "doc2.md").write_text("# Doc 2\n\n## Section B\n\nContent of doc 2 section B.")
        return tmp_path

    @pytest.fixture
    def settings(self, work_dir, tmp_path):
        return Settings(
            content_root=str(work_dir),
            qdrant_path=":memory:",
            state_db=str(tmp_path / "state.db"),
        )

    def test_first_run_indexes_everything(self, settings):
        stats = _run_incremental_memory(settings)
        assert stats["indexed"] == 2
        assert stats["skipped"] == 0
        assert stats["deleted"] == 0
        assert stats["chunks_indexed"] > 0

    def test_second_run_skips_unchanged(self, settings):
        _run_incremental_memory(settings)
        stats = _run_incremental_memory(settings)
        assert stats["indexed"] == 0
        assert stats["skipped"] == 2
        assert stats["deleted"] == 0

    def test_changed_file_reindexed(self, settings):
        _run_incremental_memory(settings)

        # Modify one file
        proj = Path(settings.content_root) / "my_project"
        (proj / "doc1.md").write_text("# Doc 1 Updated\n\n## New Section\n\nUpdated content.")

        stats = _run_incremental_memory(settings)
        assert stats["indexed"] == 1  # Only the changed file
        assert stats["skipped"] == 1  # The other file unchanged

    def test_new_file_indexed(self, settings):
        _run_incremental_memory(settings)

        # Add a new file
        proj = Path(settings.content_root) / "my_project"
        (proj / "doc3.md").write_text("# Doc 3\n\n## New\n\nBrand new document.")

        stats = _run_incremental_memory(settings)
        assert stats["indexed"] == 1  # Only the new file
        assert stats["skipped"] == 2  # Existing files unchanged

    def test_deleted_file_removed(self, settings):
        _run_incremental_memory(settings)

        # Delete one file
        proj = Path(settings.content_root) / "my_project"
        (proj / "doc1.md").unlink()

        stats = _run_incremental_memory(settings)
        assert stats["deleted"] == 1
        assert stats["skipped"] == 1  # doc2.md unchanged

    def test_state_persists_across_runs(self, settings):
        _run_incremental_memory(settings)

        # Verify state DB has records
        state = IndexState(settings.state_db)
        assert len(state.get_all_paths()) == 2
        summary = state.get_summary()
        assert summary["total_files"] == 2
        state.close()


def _run_incremental_memory(settings: Settings) -> dict:
    """Run incremental indexing with in-memory Qdrant for testing.

    Uses a shared in-memory client stored as an attribute on settings
    to simulate persistence across runs within a test.
    """
    if not hasattr(settings, "_test_client"):
        settings._test_client = Settings.get_memory_client()

    client = settings._test_client
    encoder = Encoder(settings.embedding_model)
    state = IndexState(settings.state_db)

    from ragtools.indexing.indexer import ensure_collection
    ensure_collection(client, settings.collection_name, encoder.dimension)

    from ragtools.indexing.scanner import scan_project, get_relative_path
    from ragtools.indexing.indexer import (
        index_file,
        delete_file_points,
    )

    files = scan_project(settings.content_root)
    current_paths = {get_relative_path(fp, settings.content_root) for _, fp in files}

    tracked_paths = state.get_all_paths()
    deleted_paths = tracked_paths - current_paths

    stats = {
        "indexed": 0,
        "skipped": 0,
        "deleted": 0,
        "chunks_indexed": 0,
        "projects": set(),
    }

    for del_path in deleted_paths:
        delete_file_points(client, settings.collection_name, del_path)
        state.remove(del_path)
        stats["deleted"] += 1

    for pid, file_path in files:
        relative_path = get_relative_path(file_path, settings.content_root)
        current_hash = IndexState.hash_file(file_path)

        if not state.file_changed(relative_path, current_hash):
            stats["skipped"] += 1
            continue

        delete_file_points(client, settings.collection_name, relative_path)

        count = index_file(
            client=client,
            encoder=encoder,
            collection_name=settings.collection_name,
            project_id=pid,
            file_path=file_path,
            relative_path=relative_path,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
        )

        state.update(relative_path, pid, current_hash, count)
        stats["indexed"] += 1
        stats["chunks_indexed"] += count
        stats["projects"].add(pid)

    stats["projects"] = sorted(stats["projects"])
    state.close()
    return stats
