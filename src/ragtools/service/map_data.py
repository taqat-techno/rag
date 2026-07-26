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

#: The map is a visual overview, not an exhaustive listing. Scrolling EVERY
#: point and running PCA over the whole collection is what made the UI crawl
#: during indexing (reported from real use). A sample of this size is visually
#: equivalent for a scatter plot and bounds the cost.
MAP_MAX_POINTS = 5000


def compute_map_points(
    client: QdrantClient, settings: Settings, collections: list[str] | None = None
) -> list[dict]:
    """Compute 2D/3D coordinates for all indexed files.

    Returns a list of dicts with: file_path, project_id, x, y, z, chunk_count, headings.
    The 2D canvas view uses x,y; the 3D ECharts GL view uses all three.

    ``collections`` defaults to the single configured collection. The owner
    passes every routed collection so the map shows the whole knowledge base
    rather than one project's slice of it.
    """
    # Step 1: Scroll all points with vectors
    file_vectors: dict[str, list[np.ndarray]] = defaultdict(list)
    file_meta: dict[str, dict] = {}

    targets = collections or [settings.collection_name]
    scanned = 0
    for collection_name in targets:
        offset = None
        while True:
            try:
                records, offset = client.scroll(
                    collection_name=collection_name,
                    limit=500,
                    offset=offset,
                    with_payload=True,
                    with_vectors=True,
                )
            except Exception:  # noqa: BLE001 — collection not created yet
                break
            scanned += len(records)

            is_framework = collection_name.startswith("fw_")
            for record in records:
                payload = record.payload or {}
                fp = payload.get("file_path", "")
                if not fp:
                    continue

                # Key by (collection, path), not path alone. A project file and
                # a framework file can share a relative path — `odoo/api.py` in
                # both a project and the corpus it vendors — and a bare-path key
                # merges them into ONE map point built from two collections'
                # vectors.
                key = (collection_name, fp)

                vec = record.vector
                if vec is not None:
                    file_vectors[key].append(np.array(vec, dtype=np.float32))

                # Track metadata (first chunk wins for headings/project)
                if key not in file_meta:
                    file_meta[key] = {
                        "project_id": payload.get("project_id", ""),
                        "headings": payload.get("headings", []),
                        # A framework corpus carries the FRAMEWORK's id in
                        # project_id, so without this the map would draw a
                        # vendored core as if it were a project of your own.
                        "scope": "framework" if is_framework else "project",
                        "scope_source": collection_name if is_framework else "",
                    }

            if offset is None:
                break
            if scanned >= MAP_MAX_POINTS:
                # Bounded by design: the map is an overview. Scrolling every
                # point and running PCA over the whole collection is what made
                # the UI crawl during indexing.
                logger.info("Map sampled the first %d points (cap %d)",
                            scanned, MAP_MAX_POINTS)
                break
        if scanned >= MAP_MAX_POINTS:
            break

    if not file_vectors:
        return []

    # Step 2: Mean embedding per file
    file_keys = sorted(file_vectors.keys())
    mean_embeddings = np.array([
        np.mean(file_vectors[key], axis=0) for key in file_keys
    ])

    # Step 3: PCA to 3D
    coords_3d = _pca_project(mean_embeddings)

    # Step 4: Normalize to [0, 1]
    coords_norm = _normalize_coords(coords_3d)

    # Step 5: Build result
    points = []
    for i, key in enumerate(file_keys):
        meta = file_meta.get(key, {})
        points.append({
            "file_path": key[1],
            "project_id": meta.get("project_id", ""),
            "scope": meta.get("scope", "project"),
            "scope_source": meta.get("scope_source", ""),
            "x": float(coords_norm[i, 0]),
            "y": float(coords_norm[i, 1]),
            "z": float(coords_norm[i, 2]) if coords_norm.shape[1] > 2 else 0.5,
            "chunk_count": len(file_vectors[key]),
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


def load_cached_map(db_path: str, *, allow_stale: bool = True) -> list[dict] | None:
    """Load cached map points.

    ``allow_stale`` (default True) returns the previous map even when the index
    has moved on, so the UI paints instantly instead of blocking on a full
    recompute. Pass ``allow_stale=False`` to require an up-to-date cache.
    """
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
    if stored_hash != current_hash and not allow_stale:
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
    """Mark the cached map STALE rather than deleting it.

    Deleting forced a full recompute (scroll every point + PCA) on the next
    view — and it fired at the end of every index, i.e. exactly when the machine
    was busiest. Keeping the rows lets the UI render the previous map instantly
    while a fresh one is computed in the background.
    """
    _ensure_cache_table(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE map_cache SET version_hash = ? WHERE cache_key = ?",
            ("__stale__", CACHE_KEY),
        )
        conn.commit()
    finally:
        conn.close()

    logger.debug("Map cache marked stale")


def is_map_cache_stale(db_path: str) -> bool:
    """True when the cached map exists but no longer matches the index."""
    _ensure_cache_table(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT version_hash FROM map_cache WHERE cache_key = ?", (CACHE_KEY,)
        ).fetchone()
    finally:
        conn.close()
    return bool(row and row[0] == "__stale__")
