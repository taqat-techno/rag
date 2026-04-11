"""Format search results into context blocks for Claude."""

import re

from ragtools.models import SearchResult

_VERSION_SUFFIX_RE = re.compile(r"_v\d+")


def format_context(results: list[SearchResult], query: str) -> str:
    """Format search results into a full context block for the admin UI.

    Produces a structured text block with:
    - Confidence assessment
    - Source-attributed chunks (full text, no truncation)
    - Clear notice when retrieval fails or is weak

    Used by: admin panel search page, CLI search.
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


def format_context_compact(results: list[SearchResult], query: str) -> str:
    """Token-efficient format for MCP tool output.

    Differences from format_context():
    - Deduplicates versioned document results (v1/v2 of same section)
    - Truncates each chunk to ~150 tokens (600 chars)
    - One-line header per result
    - No verbose confidence wrapper (scores speak for themselves)
    - Confidence warning only if top score < 0.3

    Used by: MCP search_knowledge_base tool.
    """
    if not results:
        return f"No relevant content found for: '{query}'."

    # Deduplicate versioned documents before formatting
    results = _deduplicate(results)

    top_score = results[0].score if results else 0

    lines = []

    # Only warn if retrieval is very weak
    if top_score < 0.3:
        lines.append(f"[LOW RELEVANCE] Top score {top_score:.2f} — results may not be relevant.")
        lines.append("")

    for i, r in enumerate(results, 1):
        # Compact header: [N] project/file.md > Heading (score):
        heading = r.headings[-1] if r.headings else ""
        source = f"{r.project_id}/{r.file_path}" if r.project_id else r.file_path
        lines.append(f"[{i}] {source} > {heading} ({r.score:.2f}):")
        lines.append(_truncate(r.text))
        lines.append("")

    return "\n".join(lines).rstrip()


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


def _truncate(text: str, max_chars: int = 600) -> str:
    """Truncate text to max_chars, cutting at a sentence boundary if possible."""
    if len(text) <= max_chars:
        return text
    # Try to cut at the last sentence boundary within the limit
    cut = text[:max_chars].rfind(". ")
    if cut > max_chars // 2:
        return text[: cut + 1] + " [...]"
    return text[:max_chars] + " [...]"


def _deduplicate(results: list[SearchResult]) -> list[SearchResult]:
    """Remove near-duplicate results from versioned documents.

    When both proposal.md and proposal_v2.md produce results for the same
    section, keep only the highest-scoring one. This prevents the same table
    appearing 3 times in output with slightly different numbers.
    """
    seen: dict[str, SearchResult] = {}
    for r in results:
        # Normalize: strip version suffixes like _v2, _v3 from file path
        normalized_path = _VERSION_SUFFIX_RE.sub("", r.file_path)
        first_heading = r.headings[0] if r.headings else ""
        key = f"{normalized_path}::{first_heading}"
        if key not in seen or r.score > seen[key].score:
            seen[key] = r
    return sorted(seen.values(), key=lambda r: r.score, reverse=True)


def _overall_confidence(top_score: float) -> str:
    """Determine overall confidence label from top result score."""
    if top_score >= 0.7:
        return "HIGH CONFIDENCE"
    elif top_score >= 0.5:
        return "MODERATE CONFIDENCE"
    return "LOW CONFIDENCE"
