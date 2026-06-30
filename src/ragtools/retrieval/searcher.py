"""Search the knowledge base using query embeddings."""

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.models import SearchResult


def _score_to_confidence(score: float) -> str:
    """Map a similarity score to a confidence label."""
    if score >= 0.7:
        return "HIGH"
    elif score >= 0.5:
        return "MODERATE"
    return "LOW"


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

        Returns:
            List of SearchResult objects, sorted by score descending.
        """
        top_k = top_k or self.settings.top_k
        # Use a None sentinel so an explicit 0.0 disables thresholding (the dev
        # pipeline relies on this to threshold *after* reranking instead of before).
        threshold = self.settings.score_threshold if score_threshold is None else score_threshold

        query_vector = self.encoder.encode_query(query)

        # Build filter — multi-project takes precedence over single-project.
        must = []
        should = []
        if project_ids:
            # Qdrant treats repeated ``should`` clauses as OR. One FieldCondition
            # per project, all in ``should`` with must-match-one semantics.
            should = [
                FieldCondition(key="project_id", match=MatchValue(value=pid))
                for pid in project_ids if pid
            ]
        elif project_id:
            must.append(
                FieldCondition(key="project_id", match=MatchValue(value=project_id))
            )

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

        points = self.client.query_points(
            collection_name=self.settings.collection_name,
            query=query_vector.tolist(),
            query_filter=query_filter,
            limit=fetch_limit,
            score_threshold=threshold,
            with_payload=True,
        ).points

        if exclude_generated:
            from ragtools.source_class import GENERATED, classify_source_class
            points = [
                p for p in points
                if classify_source_class((p.payload or {}).get("file_path", "")) != GENERATED
            ][:top_k]
        else:
            points = points[:top_k]

        from ragtools.secret_scan import redact_text

        results = []
        for point in points:
            payload = point.payload
            # Serve-time secret redaction (defense in depth): masks secret values
            # even in points indexed before content-redaction shipped.
            served_text = redact_text(payload.get("text", "") or "")
            results.append(
                SearchResult(
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
                )
            )

        return results
