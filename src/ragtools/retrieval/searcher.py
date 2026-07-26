"""Search the knowledge base using query embeddings."""

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.models import SearchResult
from ragtools.retrieval.scope import resolve_scope


def _score_to_confidence(score: float) -> str:
    """Map a similarity score to a confidence label."""
    if score >= 0.7:
        return "HIGH"
    elif score >= 0.5:
        return "MODERATE"
    return "LOW"


def point_to_search_result(point, scope: str = "project",
                           scope_source: str = "") -> SearchResult:
    """Map a Qdrant point to a :class:`SearchResult` (serve-time redaction applied).

    Shared by the single-collection :class:`Searcher` and the multi-collection
    per-project search path, so both return the same result type regardless of
    which collection model produced the hit.

    ``scope`` records WHICH collection answered — the project's own, or a
    framework corpus it links. The reader needs that: "your code does X" and
    "the framework you vendor does X" lead to different next actions, and the
    payload alone cannot say which, because a framework chunk carries the
    framework's id rather than the project's.
    """
    from ragtools.secret_scan import redact_text

    payload = point.payload or {}
    served_text = redact_text(payload.get("text", "") or "")
    return SearchResult(
        chunk_id=str(point.id),
        score=point.score,
        text=served_text,
        raw_text=served_text,
        file_path=payload.get("file_path", ""),
        project_id=payload.get("project_id", ""),
        headings=payload.get("headings", []),
        confidence=_score_to_confidence(point.score),
        line_start=payload.get("line_start", 0) or 0,
        line_end=payload.get("line_end", 0) or 0,
        language=payload.get("language", "") or "",
        chunk_type=payload.get("chunk_type", "documentation") or "documentation",
        source_class=payload.get("source_class", "owned") or "owned",
        class_name=payload.get("class_name"),
        function_name=payload.get("function_name"),
        symbols=payload.get("symbols", []) or [],
        imports=payload.get("imports", []) or [],
        exports=payload.get("exports", []) or [],
        signature=payload.get("signature", "") or "",
        scope=scope,
        scope_source=scope_source,
    )


class Searcher:
    """Searches the Qdrant knowledge base for relevant chunks."""

    def __init__(
        self,
        client: QdrantClient,
        encoder: Encoder,
        settings: Settings | None = None,
    ):
        self.client = client
        self.encoder = encoder
        self.settings = settings or Settings()

    def search(
        self,
        query: str,
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        top_k: int | None = None,
        score_threshold: float | None = None,
        chunk_types: list[str] | None = None,
        exclude_generated: bool = True,
        allow_unscoped: bool = True,
        collections: list[str] | None = None,
        collection_scoped: bool = False,
    ) -> list[SearchResult]:
        """Search for chunks relevant to the query.

        Args:
            query: Natural language search query.
            project_id: Optional single-project filter.
            project_ids: Optional multi-project filter (union). Takes precedence
                when both are provided. Use this for "search across projects
                A, B, C" without making N separate calls.
            top_k: Number of results (default from config).
            score_threshold: Minimum score (default from config).
            chunk_types: Optional filter on chunk_type (e.g. ["code"],
                ["documentation"]). Used by the layered dev-search pipeline.
            collections: Collections to search. Defaults to the single
                configured collection. Under the per-project model the caller
                (the owner, via :class:`~ragtools.collection_router.CollectionRouter`)
                passes the project's own collection followed by the framework
                corpora it references; results are merged and ranked together,
                so a project sees its dependencies without them being copied
                into its own collection.
            collection_scoped: True when ``collections`` already expresses the
                scope (the per-project model). The ``project_id`` payload filter
                is then omitted — it would be redundant for the project's own
                collection and actively WRONG for a framework corpus, whose
                chunks carry the framework's id, not the project's.

        Returns:
            List of SearchResult objects, sorted by score descending.
        """
        top_k = top_k or self.settings.top_k
        # Use a None sentinel so an explicit 0.0 disables thresholding (the dev
        # pipeline relies on this to threshold *after* reranking instead of before).
        threshold = self.settings.score_threshold if score_threshold is None else score_threshold

        # Fail-closed scope resolution (S1/A2): a requested-but-empty scope
        # (``[]``, ``[""]``) or a blank id is REFUSED here — it never silently
        # widens to a global search. Resolved before embedding so a bad scope
        # fails fast. ``allow_unscoped`` (default True at this primitive) is what
        # the router/profile layer will tighten to fail closed at the boundary.
        decision = resolve_scope(
            project_id, project_ids, allow_unscoped=allow_unscoped
        )

        query_vector = self.encoder.encode_query(query)

        # Build filter — multi-project is an OR over ``should``; single is a
        # ``must``; an explicitly-unscoped decision applies no project condition.
        must = []
        should = []
        # Under the per-project model the chosen collections ARE the scope, so
        # no project_id condition is added: it is redundant for the project's
        # own collection and would exclude every hit from a framework corpus,
        # whose chunks carry the framework's id rather than the project's.
        if not decision.unscoped and not collection_scoped:
            pids = decision.project_ids
            if len(pids) == 1:
                must.append(
                    FieldCondition(key="project_id", match=MatchValue(value=pids[0]))
                )
            else:
                should = [
                    FieldCondition(key="project_id", match=MatchValue(value=pid))
                    for pid in pids
                ]

        if chunk_types:
            # chunk_type is an OR within itself; combine with project via must.
            must.append(
                Filter(should=[
                    FieldCondition(key="chunk_type", match=MatchValue(value=ct))
                    for ct in chunk_types if ct
                ])
            )

        query_filter = None
        if must or should:
            query_filter = Filter(must=must or None, should=should or None)

        # Over-fetch when excluding generated artifacts so dropping them still
        # leaves ``top_k`` real results (coverage/build mirrors otherwise displace
        # owned source — and can outrank it on code-token queries).
        fetch_limit = (top_k * 3 + 10) if exclude_generated else top_k

        targets = collections or [self.settings.collection_name]

        # The router puts the project's OWN collection first and its linked
        # framework corpora after it, so position identifies the scope.
        scoped: list = []
        for index, collection in enumerate(targets):
            try:
                hits = self.client.query_points(
                    collection_name=collection,
                    query=query_vector.tolist(),
                    query_filter=query_filter,
                    limit=fetch_limit,
                    score_threshold=threshold,
                    with_payload=True,
                ).points
            except Exception:  # noqa: BLE001
                # A framework corpus that has not been indexed yet must not
                # take down search of the project's own content. A missing
                # collection contributes nothing; it is not an error.
                continue
            is_framework = collection.startswith("fw_") or (
                collection_scoped and index > 0
            )
            scope = "framework" if is_framework else "project"
            scoped.extend(
                (p, scope, collection if is_framework else "") for p in hits
            )

        # Merge across collections: scores are cosine similarities from one
        # shared embedding model, so they are directly comparable. Ties keep
        # their original order, which puts the project's own collection first
        # (the router orders it that way deliberately).
        if len(targets) > 1:
            scoped.sort(key=lambda item: item[0].score, reverse=True)

        if exclude_generated:
            from ragtools.source_class import GENERATED, classify_source_class
            scoped = [
                item for item in scoped
                if classify_source_class((item[0].payload or {}).get("file_path", "")) != GENERATED
            ][:top_k]
        else:
            scoped = scoped[:top_k]

        # One mapper, shared with the per-project path — the inline copy that
        # used to live here could (and did) drift from it field by field.
        return [point_to_search_result(p, scope, source) for p, scope, source in scoped]
