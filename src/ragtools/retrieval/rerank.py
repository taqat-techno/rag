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

    return bonus


def adjusted_score(r: SearchResult) -> float:
    return r.score + priority_bonus(r)


def rerank(results: list[SearchResult]) -> list[SearchResult]:
    """Return results reordered by (semantic score + priority bonus), descending.

    Stable for equal adjusted scores (preserves the original relative order).
    Does not mutate the input list or its members.
    """
    indexed = list(enumerate(results))
    indexed.sort(key=lambda pair: (-adjusted_score(pair[1]), pair[0]))
    return [r for _, r in indexed]


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
