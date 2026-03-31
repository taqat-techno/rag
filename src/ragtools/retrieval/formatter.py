"""Format search results into context blocks for Claude."""

from ragtools.models import SearchResult


def format_context(results: list[SearchResult], query: str) -> str:
    """Format search results into a context block for Claude.

    Produces a structured text block with:
    - Confidence assessment
    - Source-attributed chunks
    - Clear notice when retrieval fails or is weak

    Args:
        results: Search results from Searcher.
        query: The original query string.

    Returns:
        Formatted context string for injection into Claude prompt.
    """
    if not results:
        return (
            f"[RAG NOTICE] No relevant local content found for: '{query}'. "
            "Answer based on general knowledge only, and clearly note that "
            "no project-specific information was available."
        )

    top_score = results[0].score
    confidence = _overall_confidence(top_score)

    lines = [
        f"[RAG CONTEXT — {confidence}] "
        f"Retrieved {len(results)} chunks for query: '{query}'",
        f"Top score: {top_score:.3f}. "
        "Use retrieved content as source of truth for project-specific facts.",
        "---",
    ]

    for i, r in enumerate(results, 1):
        source = (
            f"[{i}] Source: {r.project_id}/{r.file_path}"
            f" | Section: {' > '.join(r.headings) if r.headings else 'N/A'}"
            f" | Score: {r.score:.3f} ({r.confidence})"
        )
        lines.append(source)
        lines.append(r.text)
        lines.append("---")

    return "\n".join(lines)


def format_context_brief(results: list[SearchResult], query: str) -> str:
    """Compact format — just the text chunks with minimal metadata.

    Useful for CLI search output or debugging.
    """
    if not results:
        return f"No results for: '{query}'"

    lines = []
    for i, r in enumerate(results, 1):
        heading_str = " > ".join(r.headings) if r.headings else "N/A"
        lines.append(f"[{i}] ({r.score:.3f}) {r.project_id}/{r.file_path} | {heading_str}")
        lines.append(f"    {r.text[:200]}{'...' if len(r.text) > 200 else ''}")
        lines.append("")
    return "\n".join(lines)


def _overall_confidence(top_score: float) -> str:
    """Determine overall confidence label from top result score."""
    if top_score >= 0.7:
        return "HIGH CONFIDENCE"
    elif top_score >= 0.5:
        return "MODERATE CONFIDENCE"
    return "LOW CONFIDENCE"
