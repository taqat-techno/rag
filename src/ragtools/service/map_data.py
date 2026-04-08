"""Semantic Map data pipeline — file-level 2D/3D projection.

Pipeline:
  1. Scroll all chunk vectors from Qdrant
  2. Group by file_path → compute mean embedding per file
  3. PCA reduce to 3D (2D view uses x,y; 3D view uses x,y,z)
  4. Normalize coordinates to [0, 1]
  5. Cache in SQLite
"""

import hashlib
import json
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from qdrant_client import QdrantClient

from ragtools.config import Settings

logger = logging.getLogger("ragtools.service.map")

CACHE_KEY = "file_map_v2"


def compute_map_points(client: QdrantClient, settings: Settings) -> list[dict]:
    """Compute 2D/3D coordinates for all indexed files.

    Returns a list of dicts with: file_path, project_id, x, y, z, chunk_count, headings.
    The 2D canvas view uses x,y; the 3D ECharts GL view uses all three.
    """
    # Step 1: Scroll all points with vectors
    file_vectors: dict[str, list[np.ndarray]] = defaultdict(list)
    file_meta: dict[str, dict] = {}

    offset = None
    while True:
        records, offset = client.scroll(
            collection_name=settings.collection_name,
            limit=500,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )

        for record in records:
            fp = record.payload.get("file_path", "")
            if not fp:
                continue

            vec = record.vector
            if vec is not None:
                file_vectors[fp].append(np.array(vec, dtype=np.float32))

            # Track metadata (first chunk wins for headings/project)
            if fp not in file_meta:
                file_meta[fp] = {
                    "project_id": record.payload.get("project_id", ""),
                    "headings": record.payload.get("headings", []),
                }

        if offset is None:
            break

    if not file_vectors:
        return []

    # Step 2: Mean embedding per file
    file_paths = sorted(file_vectors.keys())
    mean_embeddings = np.array([
        np.mean(file_vectors[fp], axis=0) for fp in file_paths
    ])

    # Step 3: PCA to 3D
    coords_3d = _pca_project(mean_embeddings)

    # Step 4: Normalize to [0, 1]
    coords_norm = _normalize_coords(coords_3d)

    # Step 5: Build result
    points = []
    for i, fp in enumerate(file_paths):
        meta = file_meta.get(fp, {})
        points.append({
            "file_path": fp,
            "project_id": meta.get("project_id", ""),
            "x": float(coords_norm[i, 0]),
            "y": float(coords_norm[i, 1]),
            "z": float(coords_norm[i, 2]) if coords_norm.shape[1] > 2 else 0.5,
            "chunk_count": len(file_vectors[fp]),
            "headings": meta.get("headings", []),
        })

    logger.info("Computed map: %d files, %d total chunks", len(points), sum(p["chunk_count"] for p in points))
    return points


def _pca_project(embeddings: np.ndarray) -> np.ndarray:
    """Reduce embeddings to 3D using PCA.

    Handles edge cases:
    - 0 points: returns empty array
    - 1 point: returns [[0.5, 0.5, 0.5]]
    - 2+ points: standard PCA (up to 3 components)
    """
    n = embeddings.shape[0]
    if n == 0:
        return np.empty((0, 3))
    if n == 1:
        return np.array([[0.5, 0.5, 0.5]])

    from sklearn.decomposition import PCA

    n_components = min(3, n, embeddings.shape[1])
    pca = PCA(n_components=n_components, random_state=42)
    result = pca.fit_transform(embeddings)

    # Pad to 3 columns if fewer components were possible
    while result.shape[1] < 3:
        result = np.column_stack([result, np.zeros(n)])

    return result


def _normalize_coords(coords: np.ndarray) -> np.ndarray:
    """Normalize coordinates to [0, 1] range with padding."""
    if coords.shape[0] <= 1:
        return coords

    for dim in range(coords.shape[1]):
        col = coords[:, dim]
        vmin, vmax = col.min(), col.max()
        span = vmax - vmin
        if span > 0:
            coords[:, dim] = (col - vmin) / span
        else:
            coords[:, dim] = 0.5

    # Add 5% padding so points don't sit on edges
    coords = coords * 0.9 + 0.05
    return coords


# --- SQLite Cache ---


def _ensure_cache_table(db_path: str) -> None:
    """Create the map_cache table if it doesn't exist."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS map_cache (
            cache_key TEXT PRIMARY KEY,
            version_hash TEXT NOT NULL,
            points_json TEXT NOT NULL,
            computed_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def get_cache_version_hash(db_path: str) -> str:
    """Compute a hash of the current index state. Changes when any file is added/removed/modified."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT file_path, file_hash FROM file_state ORDER BY file_path"
        ).fetchall()
    except sqlite3.OperationalError:
        # Table doesn't exist yet
        return ""
    finally:
        conn.close()

    raw = "|".join(f"{fp}:{fh}" for fp, fh in rows)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load_cached_map(db_path: str) -> list[dict] | None:
    """Load cached map points. Returns None if cache is stale or missing."""
    _ensure_cache_table(db_path)

    current_hash = get_cache_version_hash(db_path)
    if not current_hash:
        return None

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT version_hash, points_json FROM map_cache WHERE cache_key = ?",
        (CACHE_KEY,)
    ).fetchone()
    conn.close()

    if row is None:
        return None

    stored_hash, points_json = row
    if stored_hash != current_hash:
        logger.debug("Map cache stale (hash mismatch)")
        return None

    try:
        return json.loads(points_json)
    except json.JSONDecodeError:
        return None


def save_map_cache(db_path: str, points: list[dict]) -> None:
    """Save computed map points to the cache."""
    _ensure_cache_table(db_path)

    version_hash = get_cache_version_hash(db_path)
    points_json = json.dumps(points)

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO map_cache (cache_key, version_hash, points_json, computed_at) VALUES (?, ?, ?, ?)",
        (CACHE_KEY, version_hash, points_json, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    logger.debug("Map cache saved (%d points)", len(points))


def invalidate_map_cache(db_path: str) -> None:
    """Delete the cached map data. Next request will recompute."""
    _ensure_cache_table(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM map_cache WHERE cache_key = ?", (CACHE_KEY,))
    conn.commit()
    conn.close()

    logger.debug("Map cache invalidated")
