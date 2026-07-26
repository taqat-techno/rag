"""Full indexing pipeline: scan -> chunk -> embed -> upsert to Qdrant.

Supports both full and incremental indexing modes.
Ignore rules are applied by the scanner — the indexer does NOT re-check.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    Filter,
    FieldCondition,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)

from ragtools.chunking.dispatch import chunk_file
from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.indexing.scanner import (
    get_relative_path,
    scan_project,
)
from ragtools.indexing.state import IndexState
from ragtools.models import Chunk

if TYPE_CHECKING:
    from ragtools.ignore import IgnoreRules


def assert_collection_model_compatible(client: QdrantClient, name: str, expected_dim: int) -> None:
    """Refuse an existing collection whose vector dimension != the current model's.

    A5 / §11.4: model metadata is enforced on every open. Qdrant records the
    vector dimension, so a collection built under a different model is caught
    here — before it returns silently-meaningless cosine scores — rather than
    discovered later. Best-effort: if the dimension can't be read (missing
    collection, unexpected shape), never raise a FALSE refusal.
    """
    try:
        vectors = client.get_collection(name).config.params.vectors
        actual_dim = getattr(vectors, "size", None)
    except Exception:
        return
    if actual_dim is not None and actual_dim != expected_dim:
        from ragtools.embedding.backend import ModelMismatchError

        raise ModelMismatchError(
            f"collection {name!r} was built with vector dimension {actual_dim}, "
            f"but the current model produces {expected_dim}. Re-index under the "
            "current model or open the correct collection — the stored vectors "
            "are not comparable."
        )


def ensure_collection(client: QdrantClient, name: str, dim: int) -> None:
    """Create the collection if it doesn't exist; else verify it's compatible.

    Uses cosine distance and the specified vector dimension. If the collection
    already exists, its stored dimension must match ``dim`` (A5 refusal on a
    model mismatch) — it is not silently reused under the wrong model.
    """
    existing = [c.name for c in client.get_collections().collections]
    if name in existing:
        assert_collection_model_compatible(client, name, dim)
        return

    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )


def recreate_collection(client: QdrantClient, name: str, dim: int) -> None:
    """Drop and recreate the collection, guaranteeing a clean slate.

    Qdrant local mode may not fully release files on delete_collection,
    so we first try delete+create, falling back to deleting all points
    if the collection still exists with stale data.
    """
    try:
        client.delete_collection(name)
    except Exception:
        pass

    existing = [c.name for c in client.get_collections().collections]
    if name in existing:
        # Collection survived deletion (local mode quirk) — purge all points
        from qdrant_client.models import FilterSelector
        client.delete(
            collection_name=name,
            points_selector=FilterSelector(filter=Filter()),
        )
    else:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )


def chunks_to_points(chunks: list[Chunk], embeddings, file_hash: str) -> list[PointStruct]:
    """Convert chunks + embeddings into Qdrant PointStruct objects.

    Args:
        chunks: List of Chunk objects from Stage 1 chunking.
        embeddings: numpy array of shape (len(chunks), dim).
        file_hash: SHA256 hash of the source file.

    Returns:
        List of PointStruct ready for upsert.
    """
    points = []
    for chunk, embedding in zip(chunks, embeddings):
        point = PointStruct(
            id=chunk.chunk_id,
            vector=embedding.tolist(),
            payload={
                "project_id": chunk.project_id,
                "file_path": chunk.file_path,
                "chunk_index": chunk.chunk_index,
                "text": chunk.raw_text,
                "headings": chunk.headings,
                "token_count": chunk.token_count,
                "line_start": chunk.line_start,
                "line_end": chunk.line_end,
                "file_hash": file_hash,
                # Code/document metadata (defaults keep older points compatible)
                "file_name": chunk.file_name,
                "extension": chunk.extension,
                "language": chunk.language,
                "chunk_type": chunk.chunk_type,
                "source_class": chunk.source_class,
                "module": chunk.module,
                "class_name": chunk.class_name,
                "function_name": chunk.function_name,
                "symbols": chunk.symbols,
                "imports": chunk.imports,
                "exports": chunk.exports,
                "signature": chunk.signature,
            },
        )
        points.append(point)
    return points


def upsert_points(
    client: QdrantClient,
    collection_name: str,
    points: list[PointStruct],
    batch_size: int = 100,
) -> int:
    """Upsert points into Qdrant in batches.

    Returns: total number of points upserted.
    """
    total = 0
    for i in range(0, len(points), batch_size):
        batch = points[i : i + batch_size]
        client.upsert(collection_name=collection_name, points=batch)
        total += len(batch)
    return total


def delete_file_points(
    client: QdrantClient,
    collection_name: str,
    file_path: str,
) -> None:
    """Delete all points belonging to a specific file from Qdrant."""
    client.delete(
        collection_name=collection_name,
        points_selector=Filter(
            must=[FieldCondition(key="file_path", match=MatchValue(value=file_path))]
        ),
    )


#: Paths per delete request. Each `client.delete` is one HTTP round-trip to a
#: managed/external server, and Windows' default dynamic port range is only
#: 16,384 with sockets held in TIME_WAIT afterwards — so deleting one file per
#: request exhausts the range on a large project. Chunked (rather than one
#: unbounded request) to keep the filter body a sane size.
_DELETE_BATCH = 256


def delete_files_points(
    client: QdrantClient,
    collection_name: str,
    file_paths,
) -> int:
    """Delete every point belonging to ANY of ``file_paths``. Returns the count.

    The batched form of :func:`delete_file_points`. Re-indexing 38,286 files
    made 38,286 delete calls and died with ``WinError 10048`` (and, once the
    range was under pressure, ``WinError 10053``); this turns the same work into
    roughly ``len(paths) / 256`` requests.
    """
    paths = [p for p in dict.fromkeys(file_paths) if p]   # de-dupe, keep order
    if not paths:
        return 0
    for i in range(0, len(paths), _DELETE_BATCH):
        window = paths[i : i + _DELETE_BATCH]
        client.delete(
            collection_name=collection_name,
            points_selector=Filter(
                must=[FieldCondition(key="file_path", match=MatchAny(any=window))]
            ),
        )
    return len(paths)


def hash_file(file_path: Path) -> str:
    """Compute SHA256 hash of a file."""
    return hashlib.sha256(file_path.read_bytes()).hexdigest()


def apply_source_class_and_redaction(chunks, relative_path: str):
    """Assign ``source_class`` and redact secret VALUES in-place — the ONE
    authoritative indexing hygiene step (S1/A1).

    ``index_file`` (CLI / watcher / rebuild) and ``QdrantOwner`` (service) both
    call this, so a secret value can never be embedded or stored, and every
    point carries a real source class rather than the model default ``owned``.
    """
    from ragtools.source_class import classify_source_class
    from ragtools.secret_scan import redact_text

    sc = classify_source_class(relative_path)
    for chunk in chunks:
        chunk.source_class = sc
        chunk.text = redact_text(chunk.text)
        chunk.raw_text = redact_text(chunk.raw_text)
    return chunks


def index_file(
    client: QdrantClient,
    encoder: Encoder,
    collection_name: str,
    project_id: str,
    file_path: Path,
    relative_path: str,
    chunk_size: int = 400,
    chunk_overlap: int = 100,
    secret_allowlist: tuple[str, ...] = (),
) -> int:
    """Index a single file: chunk -> embed -> upsert.

    Secret-bearing files are skipped here too (defense in depth — the scanner
    already excludes them at discovery), so their contents never reach Qdrant.

    Returns: number of chunks indexed (0 if the file is secret or unsupported).
    """
    from ragtools.ignore import is_secret

    if is_secret(relative_path, secret_allowlist):
        return 0

    file_hash = hash_file(file_path)

    chunks = chunk_file(
        file_path=file_path,
        project_id=project_id,
        relative_path=relative_path,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )

    if not chunks:
        return 0

    # Source-class + secret redaction — the ONE authoritative hygiene step,
    # shared with the service (owner) index path so neither can be bypassed.
    apply_source_class_and_redaction(chunks, relative_path)

    # Encode the enriched text (with heading prefix) for embedding
    texts = [chunk.text for chunk in chunks]
    embeddings = encoder.encode_batch(texts)

    points = chunks_to_points(chunks, embeddings, file_hash)
    upsert_points(client, collection_name, points)

    return len(chunks)


def run_full_index(
    settings: Settings | None = None,
    project_id: str | None = None,
    ignore_rules: IgnoreRules | None = None,
    *,
    client=None,
    encoder=None,
) -> dict:
    """Run a full indexing pipeline (no state tracking, re-indexes everything).

    Args:
        settings: Configuration. Uses defaults if None.
        project_id: If provided, only index this project.
        client/encoder: Injected for testing / dispatch reuse; created if None.

    Returns:
        dict with indexing statistics.
    """
    if settings is None:
        settings = Settings()

    from ragtools.collection_router import build_router

    if client is None:
        client = settings.get_qdrant_client()
    if encoder is None:
        encoder = Encoder(settings.embedding_model)

    router, _registry, _frameworks = build_router(settings)
    try:
        for name in router.all_collections():
            ensure_collection(client, name, encoder.dimension)

        if ignore_rules is None:
            from ragtools.ignore import IgnoreRules
            ignore_rules = IgnoreRules(
                content_root=settings.content_root,
                global_patterns=settings.ignore_patterns,
                use_ragignore=settings.use_ragignore_files,
                secret_allowlist=settings.secret_allowlist,
            )

        files = scan_project(settings.content_root, project_id=project_id,
                             ignore_rules=ignore_rules,
                             include_code=getattr(settings, "index_source_code", False))

        stats = {
            "files_indexed": 0,
            "chunks_indexed": 0,
            "projects": set(),
        }

        for pid, file_path in files:
            relative_path = get_relative_path(file_path, settings.content_root)
            collection = router.write_collection(pid)
            ensure_collection(client, collection, encoder.dimension)
            count = index_file(
                client=client,
                encoder=encoder,
                collection_name=collection,
                project_id=pid,
                file_path=file_path,
                relative_path=relative_path,
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
                secret_allowlist=tuple(settings.secret_allowlist),
            )
            stats["files_indexed"] += 1
            stats["chunks_indexed"] += count
            stats["projects"].add(pid)

        stats["projects"] = sorted(stats["projects"])
        return stats
    finally:
        # The router owns SQLite handles; a leaked one keeps the .db locked.
        router.close()


# NOTE: ``index_by_strategy`` and ``run_per_project_index`` lived here as the
# opt-in cutover seam. They are gone: both `run_full_index` and
# `run_incremental_index` now resolve collections through
# :class:`~ragtools.collection_router.CollectionRouter`, so per-project indexing
# is not a separate pipeline to dispatch to — it is the same pipeline with a
# different router answer.
#
# Keeping them would have meant two per-project indexers, and the second one was
# strictly weaker: no state tracking (so no incremental and no delete
# detection), no progress/cancellation, and no redaction or source-class
# tagging. Anything that reached it silently lost those guarantees.


def run_incremental_index(
    settings: Settings | None = None,
    project_id: str | None = None,
    ignore_rules: IgnoreRules | None = None,
) -> dict:
    """Run incremental indexing: only process new, changed, or deleted files.

    Args:
        settings: Configuration. Uses defaults if None.
        project_id: If provided, only index this project.

    Returns:
        dict with indexing statistics:
        {
            "indexed": int,    # New or changed files processed
            "skipped": int,    # Unchanged files skipped
            "deleted": int,    # Removed files cleaned up
            "chunks_indexed": int,
            "projects": list[str],
        }
    """
    if settings is None:
        settings = Settings()

    from ragtools.collection_router import build_router

    client = settings.get_qdrant_client()
    encoder = Encoder(settings.embedding_model)
    state = IndexState(settings.state_db)

    # The CLI writes vectors too, so it resolves collections exactly the way the
    # service does. Without this, `rag index` under a per-project config wrote
    # everything into the shared collection and the results were invisible.
    router, _registry, _frameworks = build_router(settings)
    for name in router.all_collections():
        ensure_collection(client, name, encoder.dimension)

    if ignore_rules is None:
        from ragtools.ignore import IgnoreRules
        ignore_rules = IgnoreRules(
            content_root=settings.content_root,
            global_patterns=settings.ignore_patterns,
            use_ragignore=settings.use_ragignore_files,
            secret_allowlist=settings.secret_allowlist,
        )

    # Discover current files on disk
    files = scan_project(settings.content_root, project_id=project_id, ignore_rules=ignore_rules,
                         include_code=getattr(settings, "index_source_code", False))
    current_paths = {get_relative_path(fp, settings.content_root) for _, fp in files}

    # Detect deleted files (in state but not on disk)
    tracked_paths = state.get_all_paths()
    if project_id:
        # Only consider deletions within the specified project
        project_records = state.get_all_for_project(project_id)
        tracked_paths = {r["file_path"] for r in project_records}

    deleted_paths = tracked_paths - current_paths

    stats = {
        "indexed": 0,
        "skipped": 0,
        "deleted": 0,
        "chunks_indexed": 0,
        "projects": set(),
    }

    # Handle deleted files. The state row names the owning project — and so the
    # collection — so read it before `state.remove` drops the row.
    # Group by destination first, so this is a handful of requests rather than
    # one per removed file (see delete_files_points on why that matters).
    _drop: dict[str, list[str]] = {}
    for del_path in deleted_paths:
        record = state.get(del_path) or {}
        del_pid = record.get("project_id")
        try:
            targets = [router.write_collection(del_pid)] if del_pid else router.all_collections()
        except Exception:  # noqa: BLE001 — project deregistered: sweep everything
            targets = router.all_collections()
        for coll in targets:
            _drop.setdefault(coll, []).append(del_path)
    for coll, paths in _drop.items():
        delete_files_points(client, coll, paths)
    for del_path in deleted_paths:
        state.remove(del_path)
        stats["deleted"] += 1

    # Process current files
    for pid, file_path in files:
        relative_path = get_relative_path(file_path, settings.content_root)
        current_hash = IndexState.hash_file(file_path)

        if not state.file_changed(relative_path, current_hash):
            stats["skipped"] += 1
            continue

        collection = router.write_collection(pid)
        ensure_collection(client, collection, encoder.dimension)
        # File is new or changed — delete old chunks (if any), then re-index
        delete_file_points(client, collection, relative_path)

        count = index_file(
            client=client,
            encoder=encoder,
            collection_name=collection,
            project_id=pid,
            file_path=file_path,
            relative_path=relative_path,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            secret_allowlist=tuple(settings.secret_allowlist),
        )

        state.update(
            file_path=relative_path,
            project_id=pid,
            file_hash=current_hash,
            chunk_count=count,
        )

        stats["indexed"] += 1
        stats["chunks_indexed"] += count
        stats["projects"].add(pid)

    stats["projects"] = sorted(stats["projects"])
    state.close()
    # The router owns SQLite handles; a leaked one keeps the .db file locked.
    router.close()
    return stats
