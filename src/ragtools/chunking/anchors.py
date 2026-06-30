"""Generic, chunker-agnostic line attribution.

Assigns each chunk a 1-based ``line_start``/``line_end`` by locating its raw text
in the source file. Works for any chunker (markdown / code / config) without
per-chunker surgery. Approximate — provenance is a *lead* (jump-to-file:line),
not an authoritative index — and degrades to 0 (unknown) when the text can't be
located (e.g. a chunk whose text was transformed).
"""

from __future__ import annotations

from pathlib import Path

# How many leading chars of a chunk to use as the search needle. Long enough to
# disambiguate, short enough to survive minor trailing differences.
_NEEDLE_LEN = 120


def attribute_line_spans(chunks, file_path: Path | str):
    """Set ``line_start``/``line_end`` on each chunk in file order. Mutates and
    returns ``chunks``."""
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return chunks

    cursor = 0  # monotonic: chunks are emitted in file order
    for c in chunks:
        raw = c.raw_text or ""
        needle = raw.strip()[:_NEEDLE_LEN]
        if not needle:
            continue
        idx = content.find(needle, cursor)
        if idx < 0:
            idx = content.find(needle)  # fall back to a global search
        if idx < 0:
            continue
        c.line_start = content.count("\n", 0, idx) + 1
        c.line_end = c.line_start + raw.count("\n")
        cursor = idx + len(needle)
    return chunks
