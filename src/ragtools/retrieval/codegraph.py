"""Cross-file code-graph primitives (v1): symbol definition lookup.

Generic, LSP-complementary **discovery** (not authority): finds where a symbol is
likely defined and returns file:line leads. For exact definitions/rename/refs use
a language server — treat these as leads, not an index.

Lookup strategy (the recall fix):
  1. **Lexical** — a payload-filtered scroll on the symbol metadata already stored
     on each chunk (``function_name`` / ``class_name`` / ``symbols`` / ``exports``).
     This resolves *extracted* symbols directly, regardless of how the defining
     chunk embeds — fixing the prior "semantic-search-then-filter" recall hole
     where terse definition chunks never reached the semantic top-k.
  2. **Semantic fallback** — only when the lexical pass finds nothing (fuzzy or
     case-variant queries), seed a semantic search and filter by defined symbols.
"""

from __future__ import annotations

import re

_SPLIT_RE = re.compile(r"[._/\-]")


def _expand(names) -> set:
    """Lowercase a set of names plus their dotted/qualified parts (len > 2)."""
    out: set = set()
    for n in names:
        if not n:
            continue
        nl = str(n).lower()
        out.add(nl)
        for p in _SPLIT_RE.split(nl):
            if len(p) > 2:
                out.add(p)
    return out


def _def_dict(payload: dict, match: str, score: float = 0.0) -> dict:
    return {
        "file_path": payload.get("file_path", ""),
        "project_id": payload.get("project_id", ""),
        "line_start": payload.get("line_start", 0) or 0,
        "line_end": payload.get("line_end", 0) or 0,
        "class_name": payload.get("class_name"),
        "function_name": payload.get("function_name"),
        "signature": payload.get("signature", ""),
        "language": payload.get("language", ""),
        "source_class": payload.get("source_class", "owned"),
        "score": score,
        "match": match,
    }


def _classify_match(payload: dict, sym: str) -> str:
    """definition-strong (name/export match) vs mention (symbols list only)."""
    s = sym.lower()
    fn = (payload.get("function_name") or "").lower()
    cn = (payload.get("class_name") or "").lower()
    exports = {e.lower() for e in (payload.get("exports") or [])}
    if s in (fn, cn) or s in exports:
        return "definition"
    return "mention"


def _lexical_definitions(searcher, sym: str, project_id: str | None, top_k: int) -> list[dict]:
    """Payload-filtered scroll: points whose stored symbol metadata matches ``sym``."""
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

    must = []
    if project_id:
        must.append(FieldCondition(key="project_id", match=MatchValue(value=project_id)))
    should = [
        FieldCondition(key="function_name", match=MatchValue(value=sym)),
        FieldCondition(key="class_name", match=MatchValue(value=sym)),
        FieldCondition(key="symbols", match=MatchAny(any=[sym])),
        FieldCondition(key="exports", match=MatchAny(any=[sym])),
    ]
    flt = Filter(must=must or None, should=should)
    try:
        points, _ = searcher.client.scroll(
            collection_name=searcher.settings.collection_name,
            scroll_filter=flt, with_payload=True, limit=max(top_k, 50),
        )
    except Exception:
        return []

    out = [_def_dict(p.payload or {}, _classify_match(p.payload or {}, sym)) for p in points]
    # definition-strong first, then mentions; stable within group.
    out.sort(key=lambda d: d["match"] != "definition")
    # de-dupe by (file, line)
    seen: set = set()
    uniq: list[dict] = []
    for d in out:
        key = (d["file_path"], d["line_start"])
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq[:top_k]


def _semantic_definitions(searcher, sym: str, project_id: str | None, top_k: int) -> list[dict]:
    """Fallback: seed a semantic search for the symbol, filter by defined symbols."""
    results = searcher.search(query=sym, project_id=project_id, top_k=top_k, score_threshold=0.0)
    target = _expand([sym])
    out: list[dict] = []
    for r in results:
        strong = _expand(list(r.exports or [])
                         + ([r.function_name] if r.function_name else [])
                         + ([r.class_name] if r.class_name else []))
        weak = _expand(list(r.symbols or []))
        is_strong = bool(target & strong)
        if not is_strong and not (target & weak):
            continue
        payload = {
            "file_path": r.file_path, "project_id": r.project_id,
            "line_start": getattr(r, "line_start", 0), "line_end": getattr(r, "line_end", 0),
            "class_name": r.class_name, "function_name": r.function_name,
            "signature": r.signature, "language": r.language,
            "source_class": getattr(r, "source_class", "owned"),
        }
        out.append(_def_dict(payload, "definition" if is_strong else "mention", r.score))
    out.sort(key=lambda d: (d["match"] != "definition", -d["score"]))
    return out


def find_definitions(searcher, symbol: str, project_id: str | None = None, top_k: int = 25) -> list[dict]:
    """Return likely definition sites for ``symbol`` as file:line leads.

    Lexical-first (resolves extracted symbols regardless of embedding), with a
    semantic fallback for fuzzy queries. Empty/blank symbols return ``[]``.
    """
    sym = (symbol or "").strip()
    if not sym:
        return []
    lexical = _lexical_definitions(searcher, sym, project_id, top_k)
    if lexical:
        return lexical
    return _semantic_definitions(searcher, sym, project_id, top_k)
