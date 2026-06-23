"""Chunking for structured config / data files.

Handles json, yaml/yml, toml, xml, ini, dockerfile, requirements.txt, .env and
similar. The aim is structure-aware grouping (top-level keys, sections, or
directives) packed up to ``chunk_size`` tokens, falling back to line packing
when the structure can't be parsed cheaply.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ragtools.chunking.common import build_chunk
from ragtools.chunking.languages import CONFIG
from ragtools.chunking.metadata import estimate_tokens
from ragtools.models import Chunk


def chunk_config_file(
    file_path: Path,
    project_id: str,
    relative_path: str,
    language: str,
    *,
    chunk_size: int = 400,
    chunk_overlap: int = 100,
    module: str = "",
) -> list[Chunk]:
    """Chunk a config/data file into structure-aware chunks."""
    module = module or project_id
    source = file_path.read_text(encoding="utf-8", errors="replace")
    if not source.strip():
        return []

    if language == "json":
        segments = _json_segments(source)
    elif language in ("yaml",):
        segments = _yaml_segments(source)
    elif language in ("toml", "ini", "dotenv"):
        segments = _section_segments(source)
    else:
        # xml, dockerfile, requirements, makefile, and any fallback → line packing
        segments = _line_segments(source, chunk_size)

    if not segments:
        segments = _line_segments(source, chunk_size)

    return _pack(
        segments,
        project_id=project_id,
        relative_path=relative_path,
        language=language,
        module=module,
        file_name=Path(relative_path).name,
        extension=file_path.suffix.lower(),
        chunk_size=chunk_size,
    )


# --- segment producers: each returns list[(label, text, symbols)] ---


def _json_segments(source: str) -> list[tuple[str, str, list[str]]]:
    try:
        data = json.loads(source)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return [("", source.strip(), [])]
    segments = []
    for key, value in data.items():
        text = json.dumps({key: value}, indent=2, ensure_ascii=False)
        segments.append((str(key), text, [str(key)]))
    return segments


_YAML_TOP_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+):")


def _yaml_segments(source: str) -> list[tuple[str, str, list[str]]]:
    lines = source.split("\n")
    segments: list[tuple[str, str, list[str]]] = []
    current_key = ""
    buf: list[str] = []

    def flush() -> None:
        text = "\n".join(buf).strip("\n")
        if text.strip():
            segments.append((current_key, text, [current_key] if current_key else []))

    for line in lines:
        m = _YAML_TOP_KEY_RE.match(line)
        if m:  # new top-level key starts
            if buf:
                flush()
            buf = []
            current_key = m.group(1)
        buf.append(line)
    if buf:
        flush()
    return segments


_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _section_segments(source: str) -> list[tuple[str, str, list[str]]]:
    """TOML/INI sections delimited by ``[section]`` headers."""
    lines = source.split("\n")
    segments: list[tuple[str, str, list[str]]] = []
    current = "preamble"
    buf: list[str] = []

    def flush() -> None:
        text = "\n".join(buf).strip("\n")
        if text.strip():
            segments.append((current, text, [current]))

    for line in lines:
        m = _SECTION_RE.match(line)
        if m:
            if buf:
                flush()
            buf = [line]
            current = m.group(1)
        else:
            buf.append(line)
    if buf:
        flush()
    return segments


def _line_segments(source: str, chunk_size: int) -> list[tuple[str, str, list[str]]]:
    """Greedy line packing for unstructured config (xml, dockerfile, ...)."""
    lines = source.split("\n")
    segments: list[tuple[str, str, list[str]]] = []
    buf: list[str] = []
    tokens = 0
    for line in lines:
        lt = estimate_tokens(line)
        if tokens + lt > chunk_size and buf:
            segments.append(("", "\n".join(buf).strip("\n"), []))
            buf = []
            tokens = 0
        buf.append(line)
        tokens += lt
    if buf and "\n".join(buf).strip():
        segments.append(("", "\n".join(buf).strip("\n"), []))
    return segments


def _pack(
    segments: list[tuple[str, str, list[str]]],
    *,
    project_id: str,
    relative_path: str,
    language: str,
    module: str,
    file_name: str,
    extension: str,
    chunk_size: int,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    index = 0

    buf_text: list[str] = []
    buf_labels: list[str] = []
    buf_symbols: list[str] = []
    buf_tokens = 0

    def emit() -> None:
        nonlocal index, buf_text, buf_labels, buf_symbols, buf_tokens
        raw = "\n\n".join(buf_text).strip()
        if raw:
            headings = [lbl for lbl in buf_labels if lbl][:4]
            chunks.append(build_chunk(
                project_id=project_id,
                file_path=relative_path,
                chunk_index=index,
                raw_text=raw,
                language=language,
                chunk_type=CONFIG,
                file_name=file_name,
                extension=extension,
                module=module,
                headings=headings,
                symbols=_dedup(buf_symbols),
            ))
            index += 1
        buf_text = []
        buf_labels = []
        buf_symbols = []
        buf_tokens = 0

    for label, text, symbols in segments:
        st = estimate_tokens(text)
        if buf_tokens + st > chunk_size and buf_text:
            emit()
        buf_text.append(text)
        if label:
            buf_labels.append(label)
        buf_symbols.extend(symbols)
        buf_tokens += st

    emit()
    return chunks


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out
