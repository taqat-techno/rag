"""Shared chunk-construction helpers used by all chunkers.

Keeps the deterministic chunk-ID algorithm and the Chunk builder in one place
so the markdown, code, and config chunkers stay consistent.
"""

from __future__ import annotations

import hashlib

from ragtools.chunking.metadata import estimate_tokens
from ragtools.models import Chunk


def make_chunk_id(project_id: str, file_path: str, chunk_index: int) -> str:
    """Generate a deterministic chunk ID as a valid UUID string.

    Identical algorithm to ``chunking.markdown._make_chunk_id`` — Qdrant
    requires string point IDs to be valid UUIDs, so we take the first 32 hex
    chars of the SHA256 hash and format as a UUID.
    """
    raw = f"{project_id}::{file_path}::{chunk_index}"
    hex32 = hashlib.sha256(raw.encode()).hexdigest()[:32]
    return f"{hex32[:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:32]}"


def build_chunk(
    *,
    project_id: str,
    file_path: str,
    chunk_index: int,
    raw_text: str,
    language: str,
    chunk_type: str,
    file_name: str,
    extension: str,
    module: str,
    headings: list[str] | None = None,
    class_name: str | None = None,
    function_name: str | None = None,
    symbols: list[str] | None = None,
) -> Chunk:
    """Construct a fully-populated Chunk.

    The embedded ``text`` field is enriched with a lightweight context header
    (``language file_name > symbol path``) so the embedding captures where the
    chunk lives, while ``raw_text`` keeps the original content for display.
    """
    headings = headings or []
    symbols = symbols or []

    header_bits = [b for b in [language, file_name] if b]
    header = " ".join(header_bits)
    if headings:
        header = f"{header} > {' > '.join(headings)}" if header else " > ".join(headings)

    text = f"{header}\n\n{raw_text}" if header else raw_text

    return Chunk(
        chunk_id=make_chunk_id(project_id, file_path, chunk_index),
        project_id=project_id,
        file_path=file_path,
        chunk_index=chunk_index,
        text=text,
        raw_text=raw_text,
        headings=headings,
        token_count=estimate_tokens(raw_text),
        file_name=file_name,
        extension=extension,
        language=language,
        chunk_type=chunk_type,
        module=module,
        class_name=class_name,
        function_name=function_name,
        symbols=symbols,
    )
