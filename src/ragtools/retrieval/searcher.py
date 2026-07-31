"""Search the knowledge base using query embeddings."""

import logging

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

from ragtools.collection_router import identity_from_name
from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.models import SearchResult
from ragtools.retrieval.scope import resolve_scope

logger = logging.getLogger("ragtools.retrieval.searcher")


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

    ``scope`` records WHICH KIND of collection answered — a project's own, or a
    framework corpus it links. The reader needs that: "your code does X" and
    "the framework you vendor does X" lead to different next actions, and the
    payload alone cannot say which, because a framework chunk carries the
    framework's id rather than the project's.

    ``scope_source`` names that source in terms the reader can act on — the
    project id, or the dependency's corpus id — never the ``proj_<uuid>``
    collection it happens to be stored in. Both come from
    :meth:`Searcher.collection_identities`.
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
        collections: list[str] | None = None,
        router=None,
    ):
        self.client = client
        self.encoder = encoder
        self.settings = settings or Settings()
        #: The routing authority, when the caller has one. It is what turns a
        #: collection NAME into what that collection IS — see
        #: :meth:`collection_identities`. Optional so a bare Searcher still
        #: works; without it the naming scheme is read instead, which knows the
        #: kind but not which project owns a ``proj_<uuid>``.
        self.router = router
        #: Routed collections this searcher reads when a call does not name its
        #: own. Every caller that built a bare Searcher — dev-search, MCP direct
        #: mode, the CLI's offline branch, the code graph — silently fell back
        #: to the legacy single-collection name, which under per_project does
        #: not exist. The query then raised, the exception was swallowed as
        #: "no matches", and `/api/dev-search` answered `count: 0` for a project
        #: holding 1,716 chunks.
        self.default_collections = list(collections or [])

    def resolve_collections(self, explicit: list[str] | None = None) -> list[str]:
        """Which collections a read touches. ONE place decides.

        The fallback is STRATEGY-AWARE, and that is the whole distinction the
        v3.4 code missed. Under ``shared`` the configured name IS the
        collection, so reading it is correct compatibility. Under
        ``per_project`` it names nothing that exists — the query raised, the
        caller swallowed it as "no matches", and ``/api/dev-search`` answered
        ``count: 0`` for a project holding 1,716 chunks.

        Refusing beats guessing: a wrong answer here is indistinguishable from
        a real one. Every reader (search, the code graph) comes through here so
        the branch cannot be re-implemented slightly differently elsewhere.
        """
        targets = list(explicit or self.default_collections)
        if targets:
            return targets
        if getattr(self.settings, "collection_strategy", "shared") == "shared":
            return [self.settings.collection_name]
        logger.warning(
            "read requested with no routed collections under per_project; "
            "refusing to guess — resolve them through CollectionRouter")
        return []

    def collection_identities(self, targets: list[str]) -> dict:
        """What each collection being read IS — never where it sat in the list.

        Provenance is a property of the STORE that answered, so it is derived
        from the store. The previous rule — "index 0 is the project, the rest
        are frameworks" — encoded the router's ordering *preference* as if it
        were a statement of kind, and an explicit multi-project search made it
        wrong for every project but the first.

        A labelling failure must never fail the search: if the router cannot
        answer, fall back to the naming scheme, which still gets the kind right
        and simply says less about the source.
        """
        if self.router is not None:
            try:
                return self.router.identify(targets)
            except Exception:  # noqa: BLE001 — see docstring
                logger.exception(
                    "could not identify the collections being read; "
                    "labelling from the naming scheme instead")
        return {name: identity_from_name(name) for name in targets}

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
                FILTERING ONLY. It says nothing about what any collection is;
                provenance comes from :meth:`collection_identities`. Conflating
                the two is the defect this flag used to carry.

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

        targets = self.resolve_collections(collections)
        if not targets:
            return []

        # Provenance comes from what each collection IS, resolved once for the
        # whole batch. The router's ordering (own collection first) is a ranking
        # preference and says nothing about kind — reading it as one is what
        # told users their own project's code came from a vendored dependency.
        identities = self.collection_identities(targets)
        scoped: list = []
        for collection in targets:
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
            identity = identities.get(collection) or identity_from_name(collection)
            scoped.extend((p, identity.scope, identity.source) for p in hits)

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
