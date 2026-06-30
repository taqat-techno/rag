"""Priority reranking for development retrieval.

The product requirement defines a context-prioritization order:

    1. Existing project source code
    2. Existing APIs
    3. Existing workflows
    4. Architecture documents
    5. Markdown documentation
    6. General LLM knowledge

We can't reorder purely by category (that would surface an irrelevant code
chunk above a highly-relevant doc), so we apply a *modest* additive bonus on
top of the semantic score. Among results of comparable relevance, source code
and APIs float to the top; weak matches stay down regardless of category.
"""

from __future__ import annotations

import re

from ragtools.models import SearchResult

# Category bonuses (added to the cosine score, which is ~0..1).
CODE_BONUS = 0.15
API_BONUS = 0.06
WORKFLOW_BONUS = 0.04
ARCHITECTURE_BONUS = 0.05
CONFIG_BONUS = 0.02
DOC_BONUS = 0.0

# Source-class adjustment (added on top of the category bonus). Project-owned
# content is the neutral baseline; external dependency / generated / (stray)
# secret content is modestly down-weighted so it cannot outrank comparable owned
# content — generic across stacks (a vendored README must not beat your module's).
SOURCE_CLASS_BONUS = {
    "owned": 0.0,
    "dependency": -0.12,
    "generated": -0.10,
    "secret": -0.20,
}

_API_RE = re.compile(r"\b(api|endpoint|route|router|controller|handler|view|resource|rest|graphql)\b", re.IGNORECASE)
_WORKFLOW_RE = re.compile(r"\b(workflow|pipeline|task|job|scheduler|queue|service|orchestrat)\w*", re.IGNORECASE)
_ARCH_RE = re.compile(r"\b(architecture|brd|design|adr|decision|spec|rfc|diagram)\b", re.IGNORECASE)


def _haystack(r: SearchResult) -> str:
    return " ".join([
        r.file_path or "",
        " ".join(r.headings or []),
        " ".join(r.symbols or []),
    ])


def priority_bonus(r: SearchResult) -> float:
    """Compute the additive priority bonus for one result."""
    text = _haystack(r)
    bonus = 0.0

    if r.chunk_type == "code":
        bonus += CODE_BONUS
        if _API_RE.search(text):
            bonus += API_BONUS
        elif _WORKFLOW_RE.search(text):
            bonus += WORKFLOW_BONUS
    elif r.chunk_type == "config":
        bonus += CONFIG_BONUS
        if _WORKFLOW_RE.search(text):
            bonus += WORKFLOW_BONUS
    elif r.chunk_type in ("documentation", "comment"):
        if _ARCH_RE.search(text):
            bonus += ARCHITECTURE_BONUS
        else:
            bonus += DOC_BONUS

    # Down-weight non-owned content (dependency / generated / stray secret).
    bonus += SOURCE_CLASS_BONUS.get(getattr(r, "source_class", "owned"), 0.0)

    return bonus


def adjusted_score(r: SearchResult) -> float:
    return r.score + priority_bonus(r)


# Exact-identifier boost: when a query token exactly matches a symbol the chunk
# *defines*, lift it — dense embeddings routinely miss exact identifiers (e.g. a
# query for ``disaster.disaster`` not surfacing the file that defines it). This
# is a rerank bonus over existing payload metadata, NOT a second retriever / BM25.
IDENTIFIER_BONUS = 0.12
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")
_SPLIT_RE = re.compile(r"[._/\-]")


def query_tokens(query: str) -> set:
    """Lowercased identifier-like tokens (len > 2) from a query."""
    return {t.lower() for t in _IDENT_RE.findall(query or "") if len(t) > 2}


def identifier_match_bonus(r: SearchResult, q_tokens: set) -> float:
    """Boost when a query token exactly matches a symbol the chunk defines.

    Matches against the chunk's ``symbols``/``exports``/``function_name``/
    ``class_name`` (and the dotted/qualified parts thereof). Returns
    ``IDENTIFIER_BONUS`` on any match, else 0.
    """
    if not q_tokens:
        return 0.0
    names: set = set()
    for s in (r.symbols or []):
        names.add(s.lower())
    for s in (r.exports or []):
        names.add(s.lower())
    if r.function_name:
        names.add(r.function_name.lower())
    if r.class_name:
        names.add(r.class_name.lower())
    expanded = set(names)
    for n in list(names):
        for part in _SPLIT_RE.split(n):
            if len(part) > 2:
                expanded.add(part)
    return IDENTIFIER_BONUS if (q_tokens & expanded) else 0.0


def rerank(results: list[SearchResult]) -> list[SearchResult]:
    """Return results reordered by (semantic score + priority bonus), descending.

    Stable for equal adjusted scores (preserves the original relative order).
    Does not mutate the input list or its members.
    """
    indexed = list(enumerate(results))
    indexed.sort(key=lambda pair: (-adjusted_score(pair[1]), pair[0]))
    return [r for _, r in indexed]


def cap_per_file(results: list[SearchResult], max_per_file: int = 3) -> list[SearchResult]:
    """Keep at most ``max_per_file`` results per file, preserving rank order.

    Prevents one document from dominating a result set (the "9 of 10 chunks from
    one file" failure) and forces source diversity. Input is assumed already
    ranked; the highest-ranked chunks of each file survive.
    """
    if max_per_file <= 0:
        return list(results)
    seen: dict[str, int] = {}
    out: list[SearchResult] = []
    for r in results:
        n = seen.get(r.file_path, 0)
        if n < max_per_file:
            out.append(r)
            seen[r.file_path] = n + 1
    return out


def dedup_by_chunk_id(*result_lists: list[SearchResult]) -> list[SearchResult]:
    """Merge several result lists, keeping the highest-raw-score entry per chunk_id.

    Order is undefined (callers sort). Used by both the codebase-first path
    (then reranked) and the flat path (then sorted by raw score).
    """
    by_id: dict[str, SearchResult] = {}
    for results in result_lists:
        for r in results:
            existing = by_id.get(r.chunk_id)
            if existing is None or r.score > existing.score:
                by_id[r.chunk_id] = r
    return list(by_id.values())


def merge_and_rerank(*result_lists: list[SearchResult]) -> list[SearchResult]:
    """Combine several result lists (dedup by chunk_id), then priority-rerank.

    Used by the layered dev-search pipeline: search code, then docs, then
    architecture, then combine + rerank into a single ordered context set.
    """
    return rerank(dedup_by_chunk_id(*result_lists))
