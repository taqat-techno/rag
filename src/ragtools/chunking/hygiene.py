"""Chunk content-hygiene filters.

Drops content-free chunks (separators, punctuation-only, near-empty doc
fragments) before they are embedded or stored — generically across markdown,
code, and config. A content-free chunk wastes an embedding slot and can score
spuriously HIGH (a bare ``---`` separator outranking real content was observed
in the field).
"""

from __future__ import annotations

import re

_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
_WORD_RE = re.compile(r"[A-Za-z0-9]{2,}")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")

# Documentation chunks need at least this many real words (len >= 2, URLs
# stripped) to be kept — so badge rows, lone links and one-word fragments go.
_MIN_DOC_WORDS = 2

# A bare re-export pointer (no logic) — e.g. ``export default AdminImageUpload``
# or ``export { a, b }`` on its own. Low-value as a standalone retrievable chunk.
_BARE_EXPORT_RE = re.compile(r"\Aexport\s+(default\s+[\w$.]+|\{[^}]*\})\s*;?\s*\Z", re.IGNORECASE)
_COMMENT_LINE_RE = re.compile(r"\A\s*(//|#|/\*|\*|<!--|--)")


def _is_comment_only(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return bool(lines) and all(_COMMENT_LINE_RE.match(ln) for ln in lines)


def is_meaningful_chunk(raw_text: str, chunk_type: str = "documentation") -> bool:
    """Return False for content-free chunks that must not be indexed.

    Universal rule (all chunk types): the chunk must contain at least one
    alphanumeric character — this drops separators / horizontal rules
    (``---``, ``***``, ``===``, ``| --- |``) and whitespace/punctuation-only
    chunks regardless of type.

    Documentation rule (stricter): after stripping URLs, require at least
    ``_MIN_DOC_WORDS`` alphanumeric words of length >= 2, so badge rows, lone
    links and one-word fragments are dropped. Code/config/comment chunks keep
    only the lax universal rule (a one-line constant like ``x = 1`` is
    meaningful and must survive).
    """
    if not raw_text or not raw_text.strip():
        return False
    if not _ALNUM_RE.search(raw_text):
        return False
    if chunk_type == "documentation":
        cleaned = _URL_RE.sub(" ", raw_text)
        if len(_WORD_RE.findall(cleaned)) < _MIN_DOC_WORDS:
            return False
    elif chunk_type in ("code", "comment"):
        stripped = raw_text.strip()
        # Bare re-export pointers and comment-only banners carry no logic/content
        # as standalone chunks (e.g. ``export default X`` or ``// ── Import ──``).
        if _BARE_EXPORT_RE.match(stripped):
            return False
        if _is_comment_only(stripped) and len(_WORD_RE.findall(stripped)) < 3:
            return False
    return True


def filter_chunks(chunks):
    """Drop content-free chunks from a chunk list.

    Order and ``chunk_index`` values are preserved (indices may become
    non-contiguous — harmless, since chunk IDs are derived from the index and
    the indexer deletes-then-upserts per file).
    """
    return [c for c in chunks if is_meaningful_chunk(c.raw_text, c.chunk_type)]
