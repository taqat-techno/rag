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
from ragtools.retrieval.rerank import merge_and_rerank
from ragtools.retrieval.searcher import Searcher


@dataclass
class DevSearchResult:
    """The output of the layered dev-search pipeline."""

    query: str
    results: list[SearchResult]
    triggers: list[str] = field(default_factory=list)
    is_dev_request: bool = False
    layers: dict[str, int] = field(default_factory=dict)  # layer name -> hit count


def dev_search(
    searcher: Searcher,
    query: str,
    *,
    project_id: str | None = None,
    project_ids: list[str] | None = None,
    top_k: int | None = None,
    per_layer_k: int = 10,
) -> DevSearchResult:
    """Run the codebase-first layered retrieval pipeline.

    Each layer is a filtered semantic search; the layers are merged and
    reranked into a single priority-ordered context set.
    """
    top_k = top_k or searcher.settings.top_k

    def _layer(chunk_types: list[str]) -> list[SearchResult]:
        return searcher.search(
            query=query,
            project_id=project_id,
            project_ids=project_ids,
            top_k=per_layer_k,
            chunk_types=chunk_types,
        )

    # Step 1-3: code, documentation, config (architecture docs ride within docs
    # and are boosted at rerank time).
    code_hits = _layer(["code"])
    doc_hits = _layer(["documentation", "comment"])
    config_hits = _layer(["config"])

    # Step 4: combine + rerank by context priority.
    combined = merge_and_rerank(code_hits, doc_hits, config_hits)[:top_k]

    return DevSearchResult(
        query=query,
        results=combined,
        triggers=matched_triggers(query),
        is_dev_request=detect_dev_intent(query),
        layers={
            "code": len(code_hits),
            "documentation": len(doc_hits),
            "config": len(config_hits),
            "combined": len(combined),
        },
    )
