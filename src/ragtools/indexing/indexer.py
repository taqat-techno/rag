"""Full indexing pipeline: scan -> chunk -> embed -> upsert to Qdrant."""

import hashlib
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
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
    """Run a full indexing pipeline.

    Scans all projects (or a specific one), chunks all .md files,
    embeds them, and upserts into Qdrant.

    Args:
        settings: Configuration. Uses defaults if None.
        project_id: If provided, only index this project.

    Returns:
        dict with indexing statistics:
        {
            "files_indexed": int,
            "chunks_indexed": int,
            "projects": list[str],
        }
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
