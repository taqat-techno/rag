"""Reciprocal Rank Fusion for cross-collection retrieval (RAG v3, Stage S10).

Results from different collections carry non-comparable scores (different
corpora, possibly different models). RRF fuses ranked lists using only RANK, so
it is invariant to score scale — the safe default for merging a project's
collection with framework/dependency collections. The additive priority
reranker (rerank.py) then operates on the fused ranks, not raw cosine values.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S10 -> G10)
"""

from __future__ import annotations

from typing import Callable, Hashable, Iterable, Sequence


def rrf_scores(
    ranked_lists: Iterable[Sequence],
    *,
    key: Callable[[object], Hashable],
    k: int = 60,
) -> dict:
    """Reciprocal-rank-fusion score per item id: sum of ``1 / (k + rank + 1)``.

    ``rank`` is 0-based within each list; ``+1`` makes the top item contribute
    ``1/(k+1)``. An item appearing in several lists accumulates their terms.
    """
    scores: dict = {}
    for lst in ranked_lists:
        for rank, item in enumerate(lst):
            kid = key(item)
            scores[kid] = scores.get(kid, 0.0) + 1.0 / (k + rank + 1)
    return scores


def reciprocal_rank_fusion(
    ranked_lists: Iterable[Sequence],
    *,
    key: Callable[[object], Hashable],
    k: int = 60,
) -> list:
    """Fuse ranked lists by RRF and return one deduped, ordered list of items.

    The same logical item across collections (same ``key``) fuses into a single
    entry (the first occurrence is kept). Ordering is by descending RRF score;
    ties break by first appearance (stable), so the result is deterministic.
    """
    lists = [list(lst) for lst in ranked_lists]
    scores = rrf_scores(lists, key=key, k=k)

    # First occurrence of each id, in insertion order across all lists.
    first: dict = {}
    for lst in lists:
        for item in lst:
            kid = key(item)
            if kid not in first:
                first[kid] = item

    # Stable sort by score desc keeps insertion order for equal scores.
    return sorted(first.values(), key=lambda it: scores[key(it)], reverse=True)
