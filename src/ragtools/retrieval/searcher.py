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

        Returns:
            List of SearchResult objects, sorted by score descending.
        """
        top_k = top_k or self.settings.top_k
        threshold = score_threshold or self.settings.score_threshold

        query_vector = self.encoder.encode_query(query)

        # Build filter — multi-project takes precedence over single-project.
        query_filter = None
        if project_ids:
            # Qdrant treats repeated ``should`` clauses as OR. One FieldCondition
            # per project, all in ``should`` with must-match-one semantics.
            query_filter = Filter(
                should=[
                    FieldCondition(key="project_id", match=MatchValue(value=pid))
                    for pid in project_ids if pid
                ]
            )
        elif project_id:
            query_filter = Filter(
                must=[FieldCondition(key="project_id", match=MatchValue(value=project_id))]
            )

        points = self.client.query_points(
            collection_name=self.settings.collection_name,
            query=query_vector.tolist(),
            query_filter=query_filter,
            limit=top_k,
            score_threshold=threshold,
            with_payload=True,
        ).points

        results = []
        for point in points:
            payload = point.payload
            results.append(
                SearchResult(
                    chunk_id=str(point.id),
                    score=point.score,
                    text=payload.get("text", ""),
                    raw_text=payload.get("text", ""),
                    file_path=payload.get("file_path", ""),
                    project_id=payload.get("project_id", ""),
                    headings=payload.get("headings", []),
                    confidence=_score_to_confidence(point.score),
                )
            )

        return results
