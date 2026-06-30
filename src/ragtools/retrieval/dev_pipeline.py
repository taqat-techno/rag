"""Layered codebase-first retrieval pipeline (Project Context Mode).

Implements the development search strategy:

    Step 1: search project codebase embeddings   (chunk_type = code)
    Step 2: search project documentation          (chunk_type = documentation)
    Step 3: search architecture / config / BRD     (chunk_type = config + arch docs)
    Step 4: combine + rerank by context priority
    Step 5: hand the ranked context to the caller for answer generation

The reranking (see ``retrieval.rerank``) enforces the priority order:
source code > APIs > workflows > architecture > markdown docs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ragtools.models import SearchResult
from ragtools.retrieval.feature_intent import detect_dev_intent, matched_triggers
from ragtools.retrieval.rerank import (
    adjusted_score, cap_per_file, dedup_by_chunk_id, identifier_match_bonus,
    merge_and_rerank, query_tokens,
)

# Max chunks any single file may contribute to a dev-search result set, so one
# large file can't crowd out cross-file context (source diversity).
MAX_CHUNKS_PER_FILE = 3

# Minimum owned code/config results guaranteed (at the front) for a dev-intent
# result set, so rich docs can't bury the owning source. Generic across stacks.
CODE_QUOTA = 3


def _apply_code_quota(results: "list[SearchResult]", top_k: int,
                      min_code: int = CODE_QUOTA) -> "list[SearchResult]":
    """Move up to ``min_code`` top-ranked code/config chunks to the front so they
    survive the ``top_k`` cut even when higher-scoring docs are present. The rest
    keep their rank order. No-op when there is no code/config in the set."""
    code = [r for r in results if r.chunk_type in ("code", "config")]
    if not code:
        return results
    guaranteed = code[:min_code]
    gids = {r.chunk_id for r in guaranteed}
    rest = [r for r in results if r.chunk_id not in gids]
    return guaranteed + rest
from ragtools.retrieval.searcher import Searcher


@dataclass
class DevSearchResult:
    """The output of the layered dev-search pipeline."""

    query: str
    results: list[SearchResult]
    triggers: list[str] = field(default_factory=list)
    is_dev_request: bool = False
    strategy: str = "codebase-first"  # "codebase-first" (dev) | "flat" (non-dev)
    layers: dict[str, int] = field(default_factory=dict)  # layer name -> hit count
    warnings: list[str] = field(default_factory=list)  # e.g. docs-mode code search
    # Whether code is indexed for the scoped project(s): True (code/general),
    # False (docs-only → "not indexed"), or None (unscoped → unknown). Lets the
    # caller separate "not found" from "not indexed".
    code_indexed: "bool | None" = None


def dev_search(
    searcher: Searcher,
    query: str,
    *,
    project_id: str | None = None,
    project_ids: list[str] | None = None,
    top_k: int | None = None,
    per_layer_k: int = 10,
    force_dev: bool = False,
) -> DevSearchResult:
    """Run the codebase-first layered retrieval pipeline.

    Each layer is a filtered semantic search; the layers are merged and
    reranked into a single priority-ordered context set.

    ``force_dev=True`` makes the pipeline code-first regardless of query phrasing
    — the dedicated dev endpoint/tool passes this, because *choosing* the dev
    path is itself the intent signal. (Descriptive code queries like "SMS
    dispatch gateway service" don't trip the action-verb intent detector, which
    otherwise silently degrades dev-search to a flat doc-first search.)
    """
    top_k = top_k or searcher.settings.top_k

    def _layer(chunk_types: list[str]) -> list[SearchResult]:
        # score_threshold=0.0 disables per-layer filtering so the rerank bonus
        # (applied below) decides survival — borderline code is not dropped
        # before it can be boosted. Final thresholding is on the adjusted score.
        return searcher.search(
            query=query,
            project_id=project_id,
            project_ids=project_ids,
            top_k=per_layer_k,
            chunk_types=chunk_types,
            score_threshold=0.0,
        )

    # Step 1-3: code, documentation, config (architecture docs ride within docs
    # and are boosted at rerank time).
    code_hits = _layer(["code"])
    doc_hits = _layer(["documentation", "comment"])
    config_hits = _layer(["config"])

    # Step 4: intent selects the ranking strategy — this is what makes the
    # feature-intent detector load-bearing rather than a passive annotation:
    #   * dev request   -> codebase-first: rerank by the context-priority bonus,
    #     then threshold on the adjusted score.
    #   * non-dev query -> flat: plain semantic relevance by raw score, no
    #     code-first bonus (don't force code to the top for a non-dev question).
    is_dev = force_dev or detect_dev_intent(query)
    threshold = searcher.settings.score_threshold
    if is_dev:
        merged = merge_and_rerank(code_hits, doc_hits, config_hits)
    else:
        # flat: dedup, then order by RAW score directly — no code-first bonus,
        # not even as a tie-break on equal scores.
        merged = sorted(dedup_by_chunk_id(code_hits, doc_hits, config_hits),
                        key=lambda r: r.score, reverse=True)

    q_tokens = query_tokens(query) if is_dev else set()
    combined: list[SearchResult] = []
    for r in merged:
        if is_dev:
            # category/source-class bonus + exact-identifier boost
            r.adjusted_score = adjusted_score(r) + identifier_match_bonus(r, q_tokens)
        else:
            r.adjusted_score = r.score
        if r.adjusted_score >= threshold:
            combined.append(r)
    # Re-sort by the final adjusted score (so an identifier-boosted hit ranks
    # correctly), cap per-file for source diversity.
    combined.sort(key=lambda r: r.adjusted_score if r.adjusted_score is not None else r.score,
                  reverse=True)
    combined = cap_per_file(combined, MAX_CHUNKS_PER_FILE)
    # Code-first guarantee: for dev intent, reserve the front of the result set
    # for owned source so rich NL docs (which embed closer to NL queries than
    # terse code) can't crowd the owning .ts/.py/.go out of the top_k.
    if is_dev:
        combined = _apply_code_quota(combined, top_k)
    combined = combined[:top_k]

    # This pipeline IS the code-context search (Project Context Mode). If a
    # scoped project is in Docs mode, its source code was never indexed — warn
    # the caller so it doesn't read the docs-only results as "no code found",
    # then still return whatever docs matched.
    warnings: list[str] = []
    code_indexed: "bool | None" = None
    scoped = list(project_ids) if project_ids else ([project_id] if project_id else [])
    if scoped:
        by_id = {p.id: p for p in searcher.settings.projects}
        modes = [by_id[pid].mode for pid in scoped if pid in by_id]
        for pid in scoped:
            proj = by_id.get(pid)
            if proj is not None and proj.mode == "docs":
                warnings.append(
                    f"Project '{pid}' is in Docs mode; source code is not indexed."
                )
        # Code is indexed only if every scoped project indexes code (code/general).
        if modes:
            code_indexed = all(m in ("code", "general") for m in modes)

    return DevSearchResult(
        query=query,
        results=combined,
        triggers=matched_triggers(query),
        is_dev_request=is_dev,
        strategy="codebase-first" if is_dev else "flat",
        layers={
            "code": len(code_hits),
            "documentation": len(doc_hits),
            "config": len(config_hits),
            "combined": len(combined),
        },
        warnings=warnings,
        code_indexed=code_indexed,
    )
