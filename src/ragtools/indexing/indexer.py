"""Full indexing pipeline: scan -> chunk -> embed -> upsert to Qdrant.

Supports both full and incremental indexing modes.
"""

import hashlib
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    Filter,
    FieldCondition,
    MatchValue,
    PointStruct,
    VectorParams,
)

from ragtools.chunking.markdown import chunk_markdown_file
from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.indexing.scanner import (
    get_relative_path,
    scan_project,
)
from ragtools.indexing.state import IndexState
from ragtools.models import Chunk


def ensure_collection(client: QdrantClient, name: str, dim: int) -> None:
    """Create the collection if it doesn't exist.

    Uses cosine distance and the specified vector dimension.
    Skips creation if the collection already exists.
    """
    existing = [c.name for c in client.get_collections().collections]
    if name in existing:
        return

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
                "file_hash": file_hash,
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


def hash_file(file_path: Path) -> str:
    """Compute SHA256 hash of a file."""
    return hashlib.sha256(file_path.read_bytes()).hexdigest()


def index_file(
    client: QdrantClient,
    encoder: Encoder,
    collection_name: str,
    project_id: str,
    file_path: Path,
    relative_path: str,
    chunk_size: int = 400,
    chunk_overlap: int = 100,
) -> int:
    """Index a single Markdown file: chunk -> embed -> upsert.

    Returns: number of chunks indexed.
    """
    file_hash = hash_file(file_path)

    chunks = chunk_markdown_file(
        file_path=file_path,
        project_id=project_id,
        relative_path=relative_path,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )

    if not chunks:
        return 0

    # Encode the enriched text (with heading prefix) for embedding
    texts = [chunk.text for chunk in chunks]
    embeddings = encoder.encode_batch(texts)

    points = chunks_to_points(chunks, embeddings, file_hash)
    upsert_points(client, collection_name, points)

    return len(chunks)


def run_full_index(
    settings: Settings | None = None,
    project_id: str | None = None,
) -> dict:
    """Run a full indexing pipeline (no state tracking, re-indexes everything).

    Args:
        settings: Configuration. Uses defaults if None.
        project_id: If provided, only index this project.

    Returns:
        dict with indexing statistics.
    """
    if settings is None:
        settings = Settings()

    client = settings.get_qdrant_client()
    encoder = Encoder(settings.embedding_model)

    ensure_collection(client, settings.collection_name, encoder.dimension)

    files = scan_project(settings.content_root, project_id=project_id)

    stats = {
        "files_indexed": 0,
        "chunks_indexed": 0,
        "projects": set(),
    }

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


def run_incremental_index(
    settings: Settings | None = None,
    project_id: str | None = None,
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

    client = settings.get_qdrant_client()
    encoder = Encoder(settings.embedding_model)
    state = IndexState(settings.state_db)

    ensure_collection(client, settings.collection_name, encoder.dimension)

    # Discover current files on disk
    files = scan_project(settings.content_root, project_id=project_id)
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

    # Handle deleted files
    for del_path in deleted_paths:
        delete_file_points(client, settings.collection_name, del_path)
        state.remove(del_path)
        stats["deleted"] += 1

    # Process current files
    for pid, file_path in files:
        relative_path = get_relative_path(file_path, settings.content_root)
        current_hash = IndexState.hash_file(file_path)

        if not state.file_changed(relative_path, current_hash):
            stats["skipped"] += 1
            continue

        # File is new or changed — delete old chunks (if any), then re-index
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
    return stats
