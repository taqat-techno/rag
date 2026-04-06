"""Tests for Semantic Map backend — projection, caching, API."""

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from ragtools.service.map_data import (
    _pca_project,
    _normalize_coords,
    save_map_cache,
    load_cached_map,
    invalidate_map_cache,
    get_cache_version_hash,
)


# --- PCA Projection ---


def test_pca_zero_points():
    embeddings = np.empty((0, 384))
    result = _pca_project(embeddings)
    assert result.shape == (0, 2)


def test_pca_single_point():
    embeddings = np.random.randn(1, 384).astype(np.float32)
    result = _pca_project(embeddings)
    assert result.shape == (1, 2)
    assert result[0, 0] == pytest.approx(0.5)
    assert result[0, 1] == pytest.approx(0.5)


def test_pca_two_points():
    embeddings = np.random.randn(2, 384).astype(np.float32)
    result = _pca_project(embeddings)
    assert result.shape == (2, 2)


def test_pca_many_points():
    embeddings = np.random.randn(50, 384).astype(np.float32)
    result = _pca_project(embeddings)
    assert result.shape == (50, 2)


def test_pca_deterministic():
    np.random.seed(123)
    embeddings = np.random.randn(20, 384).astype(np.float32)
    r1 = _pca_project(embeddings.copy())
    r2 = _pca_project(embeddings.copy())
    np.testing.assert_array_almost_equal(r1, r2)


# --- Normalization ---


def test_normalize_empty():
    coords = np.empty((0, 2))
    result = _normalize_coords(coords)
    assert result.shape == (0, 2)


def test_normalize_single():
    coords = np.array([[3.0, -1.0]])
    result = _normalize_coords(coords)
    assert result.shape == (1, 2)


def test_normalize_range():
    coords = np.array([[0.0, 0.0], [10.0, 10.0], [5.0, 5.0]])
    result = _normalize_coords(coords)
    # After normalization with 5% padding: values should be in [0.05, 0.95]
    assert np.all(result >= 0.0)
    assert np.all(result <= 1.0)


def test_normalize_preserves_order():
    coords = np.array([[0.0, 0.0], [10.0, 10.0]])
    result = _normalize_coords(coords)
    assert result[0, 0] < result[1, 0]  # Order preserved
    assert result[0, 1] < result[1, 1]


# --- Cache ---


def test_cache_round_trip():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_state.db")

        # Create a minimal state table for version hash
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE file_state (file_path TEXT PRIMARY KEY, file_hash TEXT)")
        conn.execute("INSERT INTO file_state VALUES ('a.md', 'hash1')")
        conn.commit()
        conn.close()

        points = [
            {"file_path": "a.md", "project_id": "p1", "x": 0.5, "y": 0.3, "chunk_count": 5, "headings": []},
        ]

        save_map_cache(db_path, points)
        loaded = load_cached_map(db_path)

        assert loaded is not None
        assert len(loaded) == 1
        assert loaded[0]["file_path"] == "a.md"
        assert loaded[0]["x"] == pytest.approx(0.5)


def test_cache_stale_after_change():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_state.db")

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE file_state (file_path TEXT PRIMARY KEY, file_hash TEXT)")
        conn.execute("INSERT INTO file_state VALUES ('a.md', 'hash1')")
        conn.commit()
        conn.close()

        points = [{"file_path": "a.md", "x": 0.5, "y": 0.3}]
        save_map_cache(db_path, points)

        # Modify state (simulate re-index)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE file_state SET file_hash = 'hash2'")
        conn.commit()
        conn.close()

        loaded = load_cached_map(db_path)
        assert loaded is None  # Cache is stale


def test_cache_invalidation():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_state.db")

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE file_state (file_path TEXT PRIMARY KEY, file_hash TEXT)")
        conn.execute("INSERT INTO file_state VALUES ('a.md', 'hash1')")
        conn.commit()
        conn.close()

        points = [{"file_path": "a.md", "x": 0.5, "y": 0.3}]
        save_map_cache(db_path, points)

        # Verify cache exists
        assert load_cached_map(db_path) is not None

        # Invalidate
        invalidate_map_cache(db_path)
        assert load_cached_map(db_path) is None


def test_cache_empty_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_state.db")
        # No state table at all
        loaded = load_cached_map(db_path)
        assert loaded is None


def test_version_hash_changes():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_state.db")

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE file_state (file_path TEXT PRIMARY KEY, file_hash TEXT)")
        conn.execute("INSERT INTO file_state VALUES ('a.md', 'hash1')")
        conn.commit()
        conn.close()

        h1 = get_cache_version_hash(db_path)

        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO file_state VALUES ('b.md', 'hash2')")
        conn.commit()
        conn.close()

        h2 = get_cache_version_hash(db_path)
        assert h1 != h2
