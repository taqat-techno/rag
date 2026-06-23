"""chunk_file — route a file to the right chunker based on its classification.

This is the single entry point the indexing pipeline should call. It replaces
direct calls to ``chunk_markdown_file`` so that source code and config files
are chunked with structure awareness while documentation keeps the existing
heading-based behavior.
"""

from __future__ import annotations

from pathlib import Path

from ragtools.chunking.code import chunk_code_file
from ragtools.chunking.config_files import chunk_config_file
from ragtools.chunking.languages import CODE, CONFIG, DOCUMENTATION, classify_file
from ragtools.chunking.markdown import chunk_markdown_file
from ragtools.models import Chunk


def chunk_file(
    file_path: Path,
    project_id: str,
    relative_path: str,
    chunk_size: int = 400,
    chunk_overlap: int = 100,
) -> list[Chunk]:
    """Chunk any supported file, routing by classification.

    - documentation (md/README/text) → heading-aware markdown chunker
    - code (py/js/ts/java/go/...)     → code-aware chunker
    - config (json/yaml/toml/xml/...) → config chunker

    Unsupported files return an empty list. ``project_id`` doubles as the
    module label stored on each chunk.
    """
    file_path = Path(file_path)
    fc = classify_file(file_path)
    if fc is None:
        return []

    if fc.chunk_type == DOCUMENTATION:
        chunks = chunk_markdown_file(
            file_path=file_path,
            project_id=project_id,
            relative_path=relative_path,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        # markdown chunker predates the metadata fields — enrich them here.
        for c in chunks:
            c.language = fc.language
            c.chunk_type = DOCUMENTATION
            c.file_name = file_path.name
            c.extension = fc.extension
            c.module = project_id
        return chunks

    if fc.chunk_type == CODE:
        return chunk_code_file(
            file_path=file_path,
            project_id=project_id,
            relative_path=relative_path,
            language=fc.language,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            module=project_id,
        )

    if fc.chunk_type == CONFIG:
        return chunk_config_file(
            file_path=file_path,
            project_id=project_id,
            relative_path=relative_path,
            language=fc.language,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            module=project_id,
        )

    return []
