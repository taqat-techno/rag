"""Heading-aware Markdown chunking."""

import hashlib
import re
from pathlib import Path

from ragtools.chunking.metadata import estimate_tokens, extract_frontmatter
from ragtools.models import Chunk

# Pre-compiled regex patterns for hot paths
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
_PARAGRAPH_RE = re.compile(r'\n\n+')
_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')


def chunk_markdown_file(
    file_path: Path,
    project_id: str,
    relative_path: str,
    chunk_size: int = 400,
    chunk_overlap: int = 100,
) -> list[Chunk]:
    """Chunk a Markdown file into semantically meaningful pieces.

    Strategy:
    1. Extract frontmatter (if any)
    2. Split content into sections by heading boundaries
    3. For each section, if it fits in chunk_size, emit as one chunk
    4. If a section exceeds chunk_size, split at paragraph boundaries with overlap
    5. Prepend heading hierarchy to each chunk's text field
    6. Store raw text (without headings) in raw_text field

    Args:
        file_path: Absolute path to the .md file
        project_id: Project identifier
        relative_path: Path relative to content root (for storage)
        chunk_size: Target chunk size in estimated tokens
        chunk_overlap: Overlap in estimated tokens for split sections

    Returns: list of Chunk objects
    """
    metadata, body = extract_frontmatter(file_path)
    sections = _split_by_headings(body)

    chunks = []
    chunk_index = 0

    for headings, section_text in sections:
        section_text = section_text.strip()
        if not section_text:
            continue

        tokens = estimate_tokens(section_text)

        if tokens <= chunk_size:
            # Section fits in one chunk
            chunks.append(_make_chunk(
                project_id=project_id,
                file_path=relative_path,
                chunk_index=chunk_index,
                raw_text=section_text,
                headings=headings,
            ))
            chunk_index += 1
        else:
            # Section too large — split at paragraph boundaries
            sub_chunks = _split_large_section(
                section_text, chunk_size, chunk_overlap
            )
            for sub_text in sub_chunks:
                chunks.append(_make_chunk(
                    project_id=project_id,
                    file_path=relative_path,
                    chunk_index=chunk_index,
                    raw_text=sub_text,
                    headings=headings,
                ))
                chunk_index += 1

    return chunks


def _split_by_headings(content: str) -> list[tuple[list[str], str]]:
    """Split markdown content into sections by heading boundaries.

    Returns list of (heading_hierarchy, section_text) tuples.
    Content before the first heading gets an empty heading list.

    Heading hierarchy tracks the current nesting:
    - "## Foo" → ["## Foo"]
    - "### Bar" under "## Foo" → ["## Foo", "### Bar"]
    - "## Baz" resets → ["## Baz"]
    """
    # Find all heading positions
    headings_found = []
    for match in _HEADING_RE.finditer(content):
        level = len(match.group(1))
        title = match.group(2).strip()
        start = match.start()
        end = match.end()
        headings_found.append((level, title, start, end))

    if not headings_found:
        # No headings — entire content is one section
        return [([], content)]

    sections = []
    current_hierarchy: list[tuple[int, str]] = []

    # Content before first heading
    pre_content = content[:headings_found[0][2]].strip()
    if pre_content:
        sections.append(([], pre_content))

    for i, (level, title, start, end) in enumerate(headings_found):
        # Update hierarchy: remove headings at same or deeper level
        current_hierarchy = [
            (l, t) for l, t in current_hierarchy if l < level
        ]
        current_hierarchy.append((level, title))

        # Extract section text (between this heading and next heading)
        if i + 1 < len(headings_found):
            section_text = content[end:headings_found[i + 1][2]]
        else:
            section_text = content[end:]

        section_text = section_text.strip()

        # Format heading hierarchy as list of strings
        hierarchy = [f"{'#' * l} {t}" for l, t in current_hierarchy]

        if section_text:
            sections.append((hierarchy, section_text))

    return sections


def _split_large_section(
    text: str,
    chunk_size: int,
    chunk_overlap: int,
) -> list[str]:
    """Split a large section into smaller chunks at paragraph boundaries.

    Falls back to sentence-level splitting if paragraphs are still too large.
    """
    paragraphs = _PARAGRAPH_RE.split(text)

    chunks = []
    current_parts: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_tokens = estimate_tokens(para)

        if para_tokens > chunk_size:
            # Single paragraph exceeds chunk size — flush current, then split paragraph
            if current_parts:
                chunks.append("\n\n".join(current_parts))
                current_parts = []
                current_tokens = 0

            # Split oversized paragraph by sentences
            sentence_chunks = _split_by_sentences(para, chunk_size, chunk_overlap)
            chunks.extend(sentence_chunks)
            continue

        if current_tokens + para_tokens > chunk_size and current_parts:
            # Would exceed — flush current chunk
            chunks.append("\n\n".join(current_parts))

            # Overlap: keep last paragraph(s) up to chunk_overlap tokens
            overlap_parts = []
            overlap_tokens = 0
            for p in reversed(current_parts):
                p_tokens = estimate_tokens(p)
                if overlap_tokens + p_tokens > chunk_overlap:
                    break
                overlap_parts.insert(0, p)
                overlap_tokens += p_tokens

            current_parts = overlap_parts
            current_tokens = overlap_tokens

        current_parts.append(para)
        current_tokens += para_tokens

    if current_parts:
        chunks.append("\n\n".join(current_parts))

    return chunks


def _split_by_sentences(
    text: str,
    chunk_size: int,
    chunk_overlap: int,
) -> list[str]:
    """Last-resort splitting by sentence boundaries."""
    # Simple sentence splitting — split on . ! ? followed by space or newline
    sentences = _SENTENCE_RE.split(text)

    chunks = []
    current_parts: list[str] = []
    current_tokens = 0

    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue

        sent_tokens = estimate_tokens(sent)

        if current_tokens + sent_tokens > chunk_size and current_parts:
            chunks.append(" ".join(current_parts))

            # Overlap
            overlap_parts = []
            overlap_tokens = 0
            for s in reversed(current_parts):
                s_tokens = estimate_tokens(s)
                if overlap_tokens + s_tokens > chunk_overlap:
                    break
                overlap_parts.insert(0, s)
                overlap_tokens += s_tokens

            current_parts = overlap_parts
            current_tokens = overlap_tokens

        current_parts.append(sent)
        current_tokens += sent_tokens

    if current_parts:
        chunks.append(" ".join(current_parts))

    return chunks


def _make_chunk(
    project_id: str,
    file_path: str,
    chunk_index: int,
    raw_text: str,
    headings: list[str],
) -> Chunk:
    """Create a Chunk with deterministic ID and heading-enriched text."""
    chunk_id = _make_chunk_id(project_id, file_path, chunk_index)

    # Prepend heading hierarchy to text for embedding context
    if headings:
        # Strip # markers for the prefix, keep them in headings list
        heading_names = [h.lstrip("#").strip() for h in headings]
        prefix = " > ".join(heading_names) + "\n\n"
        text = prefix + raw_text
    else:
        text = raw_text

    return Chunk(
        chunk_id=chunk_id,
        project_id=project_id,
        file_path=file_path,
        chunk_index=chunk_index,
        text=text,
        raw_text=raw_text,
        headings=headings,
        token_count=estimate_tokens(raw_text),
    )


def _make_chunk_id(project_id: str, file_path: str, chunk_index: int) -> str:
    """Generate deterministic chunk ID as a valid UUID string.

    Qdrant requires string point IDs to be valid UUIDs.
    We take the first 32 hex chars of the SHA256 hash and format as UUID.
    """
    raw = f"{project_id}::{file_path}::{chunk_index}"
    hex32 = hashlib.sha256(raw.encode()).hexdigest()[:32]
    return f"{hex32[:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:32]}"
