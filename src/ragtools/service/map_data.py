"""Semantic Map data pipeline — file-level 2D/3D projection.

Pipeline:
  0. Establish that the routed collections share ONE vector space
     (:mod:`ragtools.service.vector_space`); exclude the ones that do not
  1. Scroll the surviving collections' chunk vectors from Qdrant
  2. Group by file_path → compute mean embedding per file
  3. PCA reduce to 3D (2D view uses x,y; 3D view uses x,y,z)
  4. Normalize coordinates to [0, 1]
  5. Cache in SQLite

Step 0 is not decoration. Every later step assumes the vectors are points in
one space, and until it existed nothing checked: a collection of a different
dimension raised an uncaught ``ValueError`` out of ``np.array`` (HTTP 500, the
whole map gone, including the projects that were fine), a named-vector
collection raised ``TypeError`` the same way, and a same-dimension /
different-model mix produced a projection that separated ENCODERS rather than
meaning — a plausible picture that was wrong, and silent.
"""

import hashlib
import json
import logging
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from qdrant_client import QdrantClient

from ragtools.config import Settings
from ragtools.identity import is_framework_collection_name

logger = logging.getLogger("ragtools.service.map")

#: Bumped from ``file_map_v3``: coverage gained the collection-level breakdown
#: (excluded / incompatible / failed / empty) and the cache identity now folds
#: in the recorded model, so a v3 blob would be served as if it described this
#: map — and would have been computed without any compatibility pre-flight.
CACHE_KEY = "file_map_v4"

#: Files plotted. The map is an overview, but "overview" means EVERY project is
#: represented — a different requirement from "as many points as possible".
#: v3.4 spent one GLOBAL point budget in registry order, so collections 3..15
#: received zero Qdrant calls and the footer asserted "385 files across 2
#: projects" as fact while 5,383 files existed across 15.
MAP_MAX_FILES = 2000

#: A project is never squeezed out by having many neighbours. At 15 projects
#: the even split is 133; this floor keeps a 4-file project on the map.
MIN_FILES_PER_PROJECT = 25

#: Safety valve on the vector pass only. Files are the unit of the budget; this
#: exists so a pathological chunk count cannot scroll without bound.
MAP_MAX_POINTS = 20000

#: file_paths per filtered scroll. Same reasoning as ``indexer._DELETE_BATCH``:
#: one request per file exhausts Windows' 16,384-port dynamic range.
_FETCH_BATCH = 128


def _enumerate_files(client: QdrantClient, collection: str) -> dict[str, dict]:
    """Every file in one collection, with its true chunk count. NO vectors.

    A vector is ~384 floats; enumerating without them is the difference between
    a pass the UI can afford and the one that made it crawl. This pass is what
    lets the sampler know how many files each project really has, so coverage
    can be reported honestly instead of asserted.
    """
    files: dict[str, dict] = {}
    offset = None
    while True:
        records, offset = client.scroll(
            collection_name=collection,
            limit=1000,
            offset=offset,
            with_payload=["file_path", "project_id", "headings"],
            with_vectors=False,
        )
        for record in records:
            payload = record.payload or {}
            fp = payload.get("file_path")
            if not fp:
                continue
            entry = files.get(fp)
            if entry is None:
                files[fp] = {
                    "chunks": 1,
                    "project_id": payload.get("project_id", ""),
                    "headings": payload.get("headings", []),
                }
            else:
                entry["chunks"] += 1
        if offset is None:
            break
    return files


def _spread(paths: list[str], k: int) -> list[str]:
    """``k`` paths spread evenly across the sorted namespace.

    Deterministic, so the map does not reshuffle between recomputes, and a
    SPREAD rather than a prefix — taking the first k would show one directory
    and call it the project.
    """
    n = len(paths)
    if k >= n:
        return paths
    step = n / k
    return [paths[min(int(i * step), n - 1)] for i in range(k)]


def _fetch_vectors(
    client: QdrantClient, collection: str, wanted: list[str],
    vector_name: str | None = "", dimension: int | None = None,
) -> dict[str, list[np.ndarray]]:
    """Every chunk vector of each requested file, IN THE MAP'S SPACE.

    ALL chunks, not the ones that happened to fall inside a prefix. v3.4 built
    a file's position from whatever subset the global budget reached, so 347 of
    375 displayed files were plotted from an incomplete mean — the map was
    wrong about position, not merely incomplete.

    ``vector_name`` and ``dimension`` come from the pre-flight's reference
    space. Extraction goes through ``extract_vector`` rather than
    ``np.array(record.vector)``: a named-vector record is a ``dict``, and
    handing that to numpy is the ``TypeError`` that returned a 500. A vector
    that is not in the reference space is DROPPED, not coerced — which is what
    makes the matrix rectangular by construction rather than by hope.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchAny

    from ragtools.service.vector_space import extract_vector

    out: dict[str, list[np.ndarray]] = defaultdict(list)
    for i in range(0, len(wanted), _FETCH_BATCH):
        window = wanted[i:i + _FETCH_BATCH]
        offset = None
        while True:
            records, offset = client.scroll(
                collection_name=collection,
                scroll_filter=Filter(
                    must=[FieldCondition(key="file_path", match=MatchAny(any=window))]
                ),
                limit=1000,
                offset=offset,
                with_payload=["file_path"],
                with_vectors=True,
            )
            for record in records:
                fp = (record.payload or {}).get("file_path")
                if not fp:
                    continue
                vec = extract_vector(record.vector, vector_name)
                if vec is None or (dimension is not None and len(vec) != dimension):
                    continue
                out[fp].append(np.array(vec, dtype=np.float32))
            if offset is None:
                break
    return out


def _stack(file_keys: list[tuple[str, str]],
           file_vectors: dict[tuple[str, str], list[np.ndarray]]):
    """``(matrix, keys)`` — the mean embedding per file, guaranteed rectangular.

    The pre-flight makes a ragged stack almost impossible; this makes it
    impossible. Rows of the modal width are kept and any other width is
    dropped, so ``np.array`` cannot raise here whatever a collection turns out
    to hold. A drop is logged rather than counted into a coverage field: it is
    a per-FILE anomaly, and coverage counts collections and files the sampler
    chose, not rows numpy refused.
    """
    means: dict[tuple[str, str], np.ndarray] = {}
    for key in file_keys:
        vectors = file_vectors.get(key) or []
        if vectors:
            means[key] = np.mean(vectors, axis=0)

    widths = Counter(int(v.shape[0]) for v in means.values() if v.ndim == 1)
    if not widths:
        return np.empty((0, 0)), []
    width = widths.most_common(1)[0][0]

    kept = [k for k in file_keys
            if k in means and means[k].ndim == 1 and means[k].shape[0] == width]
    dropped = len(means) - len(kept)
    if dropped:
        logger.warning("Map: dropped %d file(s) whose mean embedding was not "
                       "%d-dimensional", dropped, width)
    return np.array([means[k] for k in kept]), kept


def compute_map_points(
    client: QdrantClient, settings: Settings, collections: list[str] | None = None
) -> dict:
    """Coordinates for a balanced sample of indexed files, plus honest coverage.

    Returns ``{"points", "coverage", "excluded", "space"}``, where ``space``
    names the vector space the projection was actually built in — the thing
    every exclusion is measured against, and unreadable prose without it.

    Three passes. The first establishes which collections share ONE vector
    space; the second enumerates the survivors' files without vectors; the
    third fetches every chunk of the files actually selected. That ordering is
    what makes the guarantees possible at once: an incompatible collection
    costs one cheap probe rather than a full enumeration, each project gets a
    quota (so none is dropped for being enumerated late), and each plotted file
    is positioned from ALL of its chunks (so the coordinates are true).

    A collection that cannot be read, or whose vectors are not points in the
    same space as the rest, is reported in ``excluded`` with a stated reason —
    never silently skipped, and never allowed to take the other collections
    down. There is no input shape for which this returns an error instead of a
    map: every refusal is data.
    """
    targets = list(collections or [])
    if not targets:
        # No legacy fallback. Under per_project the configured name identifies
        # nothing that exists; asking for "the collection" is the v2 question.
        return {"points": [], "coverage": _coverage(0, 0, 0, 0, 0, False, [], []),
                "excluded": [], "space": None}

    # --- pass 0: do these collections share a vector space? ---------------
    from ragtools.service import vector_space as vspace

    recorded = vspace.recorded_models(getattr(settings, "state_db", None))
    spaces = {name: vspace.probe_space(client, name, recorded) for name in targets}
    reference, compatible, excluded = vspace.partition(spaces)
    for entry in excluded:
        logger.warning("Map: excluding collection %s — %s",
                       entry["collection"], entry["reason"])

    if reference is None:
        return {"points": [],
                "coverage": _coverage(len(targets), 0, 0, 0, 0, False, [], excluded),
                "excluded": excluded, "space": None}

    inventory: dict[str, dict[str, dict]] = {}
    for collection_name in compatible:
        try:
            found = _enumerate_files(client, collection_name)
        except Exception as exc:  # noqa: BLE001
            # Report it. "No results" and "could not be read" are different
            # answers and the UI must be able to tell them apart.
            excluded.append({"collection": collection_name,
                             "kind": vspace.KIND_FAILED,
                             "reason": f"unreadable: {type(exc).__name__}"})
            logger.warning("Map: collection %s unreadable: %s", collection_name, exc)
            continue
        if not found:
            excluded.append({"collection": collection_name, "kind": "empty",
                             "reason": "empty"})
            continue
        inventory[collection_name] = found

    if not inventory:
        return {"points": [],
                "coverage": _coverage(len(targets), 0, 0, 0, 0, False, [], excluded),
                "excluded": excluded, "space": reference.describe()}

    # --- balanced file quota, with the unused share redistributed ---------
    quota = max(MIN_FILES_PER_PROJECT, MAP_MAX_FILES // len(inventory))
    selected: dict[str, list[str]] = {}
    spare = 0
    for name, files in inventory.items():
        paths = sorted(files)
        take = min(quota, len(paths))
        spare += quota - take
        selected[name] = _spread(paths, take)
    if spare:
        for name, files in inventory.items():
            if not spare:
                break
            paths = sorted(files)
            room = len(paths) - len(selected[name])
            if room > 0:
                extra = min(room, spare)
                selected[name] = _spread(paths, len(selected[name]) + extra)
                spare -= extra

    # --- vectors for exactly those files ----------------------------------
    file_vectors: dict[tuple[str, str], list[np.ndarray]] = {}
    file_meta: dict[tuple[str, str], dict] = {}
    sampled_vectors = 0
    truncated = False
    for name, paths in selected.items():
        if sampled_vectors >= MAP_MAX_POINTS:
            truncated = True
            excluded.append({"collection": name, "kind": "truncated",
                             "reason": "vector budget exhausted"})
            continue
        try:
            fetched = _fetch_vectors(client, name, paths,
                                     vector_name=reference.vector_name,
                                     dimension=reference.dimension)
        except Exception as exc:  # noqa: BLE001
            excluded.append({"collection": name, "kind": vspace.KIND_FAILED,
                             "reason": f"vector fetch failed: {type(exc).__name__}"})
            logger.warning("Map: vector fetch failed for %s: %s", name, exc)
            continue
        # One predicate answers "what kind of collection is this" (R08); this
        # was the third independent spelling of it.
        is_framework = is_framework_collection_name(name)
        for fp, vecs in fetched.items():
            if not vecs:
                continue
            # Key by (collection, path), not path alone. A project file and a
            # framework file can share a relative path — `odoo/api.py` in both
            # a project and the corpus it vendors — and a bare-path key merges
            # them into ONE point built from two collections' vectors.
            key = (name, fp)
            file_vectors[key] = vecs
            meta = inventory[name].get(fp, {})
            file_meta[key] = {
                "project_id": meta.get("project_id", ""),
                "headings": meta.get("headings", []),
                # A framework corpus carries the FRAMEWORK's id in project_id,
                # so without this the map would draw a vendored core as if it
                # were a project of your own.
                "scope": "framework" if is_framework else "project",
                "scope_source": name if is_framework else "",
            }
            sampled_vectors += len(vecs)

    file_keys = sorted(file_vectors.keys())
    mean_embeddings, file_keys = _stack(file_keys, file_vectors)
    if not file_keys:
        return {"points": [],
                "coverage": _coverage(len(targets), 0,
                                      sum(len(f) for f in inventory.values()), 0, 0,
                                      truncated, [], excluded),
                "excluded": excluded, "space": reference.describe()}

    coords_norm = _normalize_coords(_pca_project(mean_embeddings))

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

    per_project = [
        {"collection": name,
         "project_id": next(iter(inventory[name].values()), {}).get("project_id", ""),
         "eligible": len(inventory[name]),
         "sampled": len(selected[name])}
        for name in sorted(selected)
    ]
    files_eligible = sum(len(f) for f in inventory.values())
    coverage = _coverage(
        projects_total=len(targets),
        projects_represented=len({k[0] for k in file_keys}),
        files_eligible=files_eligible,
        files_sampled=len(points),
        vectors_sampled=sampled_vectors,
        truncated=truncated or len(points) < files_eligible,
        per_project=per_project,
        excluded=excluded,
    )
    logger.info(
        "Computed map: %d/%d files across %d/%d collections (%d vectors, "
        "%d excluded: %d incompatible, %d failed)",
        len(points), files_eligible, coverage["projects_represented"],
        len(targets), sampled_vectors, coverage["collections_excluded"],
        coverage["collections_incompatible"], coverage["collections_failed"],
    )
    return {"points": points, "coverage": coverage, "excluded": excluded,
            "space": reference.describe()}


def _coverage(projects_total, projects_represented, files_eligible,
              files_sampled, vectors_sampled, truncated, per_project,
              excluded) -> dict:
    """The map's own account of what it did and did not draw.

    The ``collections_*`` counts are derived from ``excluded`` rather than
    accumulated alongside it, so a reason recorded without a count (or the
    reverse) is not expressible. Each names its unit and its meaning: a reader
    must never have to guess whether a number counts files or collections, nor
    whether an exclusion was a refusal (``incompatible``) or a fault
    (``failed``).
    """
    kinds = Counter(entry.get("kind", "excluded") for entry in excluded)
    return {
        "projects_total": projects_total,
        "projects_represented": projects_represented,
        "files_eligible": files_eligible,
        "files_sampled": files_sampled,
        "vectors_sampled": vectors_sampled,
        "truncated": bool(truncated),
        "per_project": per_project,
        # Collections routed to the map but not drawn, by why.
        "collections_excluded": len(excluded),
        "collections_incompatible": kinds.get("incompatible", 0),
        "collections_failed": kinds.get("failed", 0),
        "collections_empty": kinds.get("empty", 0),
        "collections_truncated": kinds.get("truncated", 0),
    }


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
    """Hash of everything that decides what the map should look like.

    Two inputs, because there are two ways the picture changes:

    * **What is indexed** — the file hashes. A file added, removed or edited
      moves a point.
    * **What it was indexed WITH** — the recorded index identity (model,
      dimension, project → collection mapping). Re-embedding under a different
      model rewrites every vector while leaving every file hash untouched, so a
      file-only digest kept serving the previous model's projection as current.
      That is the same input the compatibility pre-flight reads, and the cache
      has to be keyed on it or a model change is invisible to the map.
    """
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

    from ragtools.service.vector_space import identity_meta_rows

    identity = identity_meta_rows(db_path)
    raw = "|".join(f"{fp}:{fh}" for fp, fh in rows)
    raw += "||" + "|".join(f"{k}={identity[k]}" for k in sorted(identity))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load_cached_map(db_path: str, *, allow_stale: bool = True):
    """Load the cached map payload.

    ``allow_stale`` (default True) returns the previous map even when the index
    has moved on, so the UI paints instantly instead of blocking on a full
    recompute. Pass ``allow_stale=False`` to require an up-to-date cache.

    When the payload is a mapping it is stamped with ``cache`` —
    ``{computed_at, stale}``. v3.4 served an arbitrarily old map with no
    indicator anywhere, so a permanently failing background refresh was
    indistinguishable from a current map.
    """
    _ensure_cache_table(db_path)

    current_hash = get_cache_version_hash(db_path)
    if not current_hash:
        return None

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT version_hash, points_json, computed_at FROM map_cache WHERE cache_key = ?",
        (CACHE_KEY,)
    ).fetchone()
    conn.close()

    if row is None:
        return None

    stored_hash, points_json, computed_at = row
    stale = stored_hash != current_hash
    if stale and not allow_stale:
        logger.debug("Map cache stale (hash mismatch)")
        return None

    try:
        payload = json.loads(points_json)
    except json.JSONDecodeError:
        return None

    if isinstance(payload, dict):
        payload["cache"] = {"computed_at": computed_at, "stale": stale}
    return payload


def save_map_cache(db_path: str, payload) -> None:
    """Cache a computed map payload (any JSON-serialisable value)."""
    _ensure_cache_table(db_path)

    version_hash = get_cache_version_hash(db_path)
    points_json = json.dumps(payload)

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO map_cache (cache_key, version_hash, points_json, computed_at) VALUES (?, ?, ?, ?)",
        (CACHE_KEY, version_hash, points_json, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    size = len(payload.get("points", [])) if isinstance(payload, dict) else len(payload)
    logger.debug("Map cache saved (%d points)", size)


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
