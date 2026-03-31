"""Tests for embedding and indexing pipeline."""

from pathlib import Path

import numpy as np
import pytest

from ragtools.embedding.encoder import Encoder
from ragtools.indexing.indexer import (
    chunks_to_points,
    ensure_collection,
    hash_file,
    index_file,
    run_full_index,
    upsert_points,
)
from ragtools.chunking.markdown import chunk_markdown_file
from ragtools.config import Settings
from ragtools.models import Chunk

FIXTURES = Path(__file__).parent / "fixtures"


# --- Encoder Tests ---


class TestEncoder:
    @pytest.fixture(scope="class")
    def encoder(self):
        """Load encoder once for all tests in this class (model loading is slow)."""
        return Encoder("all-MiniLM-L6-v2")

    def test_dimension(self, encoder):
        assert encoder.dimension == 384

    def test_encode_batch(self, encoder):
        texts = ["Hello world", "Another sentence", "Third one"]
        embeddings = encoder.encode_batch(texts)
        assert isinstance(embeddings, np.ndarray)
        assert embeddings.shape == (3, 384)

    def test_encode_batch_normalized(self, encoder):
        embeddings = encoder.encode_batch(["Test sentence"])
        norm = np.linalg.norm(embeddings[0])
        assert abs(norm - 1.0) < 0.01, f"Expected unit norm, got {norm}"

    def test_encode_query(self, encoder):
        embedding = encoder.encode_query("What is Python?")
        assert isinstance(embedding, np.ndarray)
        assert embedding.shape == (384,)

    def test_encode_query_normalized(self, encoder):
        embedding = encoder.encode_query("Test query")
        norm = np.linalg.norm(embedding)
        assert abs(norm - 1.0) < 0.01

    def test_similar_texts_closer(self, encoder):
        embs = encoder.encode_batch([
            "The cat sat on the mat",
            "A feline rested on the rug",
            "Quantum physics is complex",
        ])
        # Cosine similarity: cat/feline should be closer than cat/quantum
        sim_close = np.dot(embs[0], embs[1])
        sim_far = np.dot(embs[0], embs[2])
        assert sim_close > sim_far

    def test_empty_list(self, encoder):
        embeddings = encoder.encode_batch([])
        assert len(embeddings) == 0


# --- Qdrant Collection Tests ---


class TestEnsureCollection:
    def test_creates_collection(self, memory_client):
        ensure_collection(memory_client, "test_col", 384)
        collections = [c.name for c in memory_client.get_collections().collections]
        assert "test_col" in collections

    def test_idempotent(self, memory_client):
        ensure_collection(memory_client, "test_col2", 384)
        ensure_collection(memory_client, "test_col2", 384)  # Should not raise
        collections = [c.name for c in memory_client.get_collections().collections]
        assert collections.count("test_col2") == 1


# --- Points Construction Tests ---


class TestChunksToPoints:
    def test_point_structure(self):
        test_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        chunks = [
            Chunk(
                chunk_id=test_uuid,
                project_id="proj",
                file_path="proj/README.md",
                chunk_index=0,
                text="Heading\n\nContent here",
                raw_text="Content here",
                headings=["## Heading"],
                token_count=3,
            )
        ]
        embeddings = np.random.randn(1, 384).astype(np.float32)
        points = chunks_to_points(chunks, embeddings, file_hash="abc123")

        assert len(points) == 1
        p = points[0]
        assert p.id == test_uuid
        assert p.payload["project_id"] == "proj"
        assert p.payload["file_path"] == "proj/README.md"
        assert p.payload["chunk_index"] == 0
        assert p.payload["text"] == "Content here"  # raw_text, not enriched
        assert p.payload["headings"] == ["## Heading"]
        assert p.payload["token_count"] == 3
        assert p.payload["file_hash"] == "abc123"

    def test_multiple_chunks(self):
        chunks = [
            Chunk(
                chunk_id=f"a1b2c3d4-e5f6-7890-abcd-ef123456789{i}",
                project_id="proj",
                file_path="proj/doc.md",
                chunk_index=i,
                text=f"Text {i}",
                raw_text=f"Raw {i}",
                token_count=2,
            )
            for i in range(5)
        ]
        embeddings = np.random.randn(5, 384).astype(np.float32)
        points = chunks_to_points(chunks, embeddings, file_hash="hash123")
        assert len(points) == 5
        assert [p.id for p in points] == [f"a1b2c3d4-e5f6-7890-abcd-ef123456789{i}" for i in range(5)]


# --- Upsert Tests ---


class TestUpsertPoints:
    def test_upsert_and_count(self, memory_client):
        ensure_collection(memory_client, "upsert_test", 384)

        chunks = [
            Chunk(
                chunk_id=f"a1b2c3d4-e5f6-7890-abcd-e0000000000{i}",
                project_id="proj",
                file_path="proj/file.md",
                chunk_index=i,
                text=f"Text {i}",
                raw_text=f"Raw {i}",
                token_count=2,
            )
            for i in range(10)
        ]
        embeddings = np.random.randn(10, 384).astype(np.float32)
        points = chunks_to_points(chunks, embeddings, file_hash="abc")
        count = upsert_points(memory_client, "upsert_test", points, batch_size=3)

        assert count == 10
        info = memory_client.get_collection("upsert_test")
        assert info.points_count == 10

    def test_upsert_idempotent(self, memory_client):
        """Re-upserting same IDs should not increase count."""
        ensure_collection(memory_client, "idemp_test", 384)

        chunks = [
            Chunk(
                chunk_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                project_id="proj",
                file_path="proj/file.md",
                chunk_index=0,
                text="Text",
                raw_text="Raw",
                token_count=1,
            )
        ]
        embeddings = np.random.randn(1, 384).astype(np.float32)
        points = chunks_to_points(chunks, embeddings, file_hash="h1")

        upsert_points(memory_client, "idemp_test", points)
        upsert_points(memory_client, "idemp_test", points)  # Same ID again

        info = memory_client.get_collection("idemp_test")
        assert info.points_count == 1  # Not 2


# --- Hash File Tests ---


class TestHashFile:
    def test_deterministic(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("Hello world")
        h1 = hash_file(f)
        h2 = hash_file(f)
        assert h1 == h2

    def test_different_content(self, tmp_path):
        f1 = tmp_path / "a.md"
        f2 = tmp_path / "b.md"
        f1.write_text("content a")
        f2.write_text("content b")
        assert hash_file(f1) != hash_file(f2)


# --- Integration: index_file ---


class TestIndexFile:
    @pytest.fixture(scope="class")
    def encoder(self):
        return Encoder("all-MiniLM-L6-v2")

    def test_index_single_file(self, memory_client, encoder):
        ensure_collection(memory_client, "idx_test", 384)

        count = index_file(
            client=memory_client,
            encoder=encoder,
            collection_name="idx_test",
            project_id="project_a",
            file_path=FIXTURES / "project_a" / "README.md",
            relative_path="project_a/README.md",
        )
        assert count > 0

        info = memory_client.get_collection("idx_test")
        assert info.points_count == count

    def test_index_file_idempotent(self, memory_client, encoder):
        """Re-indexing same file should not double the points."""
        ensure_collection(memory_client, "idemp_file", 384)

        for _ in range(2):
            index_file(
                client=memory_client,
                encoder=encoder,
                collection_name="idemp_file",
                project_id="project_a",
                file_path=FIXTURES / "project_a" / "guide.md",
                relative_path="project_a/guide.md",
            )

        info = memory_client.get_collection("idemp_file")
        # Count should be the same as one indexing run
        single_count = len(chunk_markdown_file(
            file_path=FIXTURES / "project_a" / "guide.md",
            project_id="project_a",
            relative_path="project_a/guide.md",
        ))
        assert info.points_count == single_count

    def test_payload_contents(self, memory_client, encoder):
        ensure_collection(memory_client, "payload_test", 384)

        index_file(
            client=memory_client,
            encoder=encoder,
            collection_name="payload_test",
            project_id="project_b",
            file_path=FIXTURES / "project_b" / "notes.md",
            relative_path="project_b/notes.md",
        )

        # Scroll all points and check payload
        points, _ = memory_client.scroll(
            collection_name="payload_test",
            limit=100,
            with_payload=True,
        )
        assert len(points) > 0
        for p in points:
            assert "project_id" in p.payload
            assert p.payload["project_id"] == "project_b"
            assert "file_path" in p.payload
            assert "text" in p.payload
            assert "headings" in p.payload
            assert "token_count" in p.payload
            assert "file_hash" in p.payload
            assert "chunk_index" in p.payload


# --- Integration: run_full_index ---


class TestRunFullIndex:
    def test_full_index_fixtures(self):
        """Index the test fixtures directory end-to-end."""
        settings = Settings(
            content_root=str(FIXTURES),
            qdrant_path=":memory:",
        )
        # Override to use in-memory client
        stats = _run_full_index_memory(settings)

        assert stats["files_indexed"] >= 3  # README.md, guide.md, notes.md
        assert stats["chunks_indexed"] > 0
        assert "project_a" in stats["projects"]
        assert "project_b" in stats["projects"]


def _run_full_index_memory(settings: Settings) -> dict:
    """Helper: run full index with in-memory Qdrant for testing."""
    from ragtools.indexing.scanner import scan_project, get_relative_path
    from ragtools.embedding.encoder import Encoder

    client = Settings.get_memory_client()
    encoder = Encoder(settings.embedding_model)
    ensure_collection(client, settings.collection_name, encoder.dimension)

    files = scan_project(settings.content_root)
    stats = {"files_indexed": 0, "chunks_indexed": 0, "projects": set()}

    for pid, file_path in files:
        relative_path = get_relative_path(file_path, settings.content_root)
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
        stats["files_indexed"] += 1
        stats["chunks_indexed"] += count
        stats["projects"].add(pid)

    stats["projects"] = sorted(stats["projects"])
    return stats
