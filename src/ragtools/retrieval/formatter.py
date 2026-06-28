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


def format_dev_context(results: list[SearchResult], query: str, triggers: list[str] | None = None) -> str:
    """Format retrieved project context for a development/feature request.

    Produces the required "Project Context Mode" response scaffold:

        Relevant Files:
        Existing Implementation:
        Recommended Changes:    (assistant fills from the request)
        Sample Code:            (assistant fills from the request)

    The "Relevant Files" and "Existing Implementation" sections are grounded in
    the retrieved chunks so the generated answer references actual repository
    files. The latter two sections are scaffolds the assistant completes.
    """
    trigger_note = f" (triggers: {', '.join(triggers)})" if triggers else ""

    if not results:
        return (
            f"[PROJECT CONTEXT — no matches]{trigger_note}\n"
            f"No indexed project files matched: '{query}'. "
            "State that no project-specific implementation was found, then proceed "
            "with general guidance clearly labeled as not grounded in the repository."
        )

    # Unique files in priority-ranked order.
    files: list[str] = []
    seen_files: set[str] = set()
    per_file_summary: dict[str, SearchResult] = {}
    for r in results:
        key = f"{r.project_id}/{r.file_path}" if r.project_id else r.file_path
        if key not in seen_files:
            seen_files.add(key)
            files.append(key)
            per_file_summary[key] = r

    lines = [
        f"[PROJECT CONTEXT — {len(results)} chunks across {len(files)} files]{trigger_note}",
        "Ground your answer in these existing implementations. Prefer extending "
        "the patterns below over inventing new designs.",
        "",
        "Relevant Files:",
    ]
    for f in files:
        lines.append(f"* {f}")

    lines.append("")
    lines.append("Existing Implementation:")
    for f in files:
        r = per_file_summary[f]
        bits = []
        if r.language:
            bits.append(r.language)
        if r.class_name:
            bits.append(f"class {r.class_name}")
        if r.function_name:
            bits.append(f"fn {r.function_name}")
        if r.symbols:
            bits.append("symbols: " + ", ".join(r.symbols[:6]))
        elif r.headings:
            bits.append(" > ".join(r.headings))
        descriptor = " | ".join(bits) if bits else "see retrieved chunk"
        disp_score = r.adjusted_score if r.adjusted_score is not None else r.score
        lines.append(f"* {f} — {descriptor} ({_confidence_band(disp_score)}, {_score_label(r)})")

    lines.append("")
    lines.append("--- Retrieved chunks ---")
    for i, r in enumerate(results, 1):
        source = f"{r.project_id}/{r.file_path}" if r.project_id else r.file_path
        sym = f" | {' > '.join(r.headings)}" if r.headings else ""
        lines.append(f"[{i}] {source}{sym} ({r.chunk_type}, {_score_label(r)}):")
        lines.append(_truncate(r.text, 800))
        lines.append("")

    lines.append("Recommended Changes:")
    lines.append("* (Assistant: list concrete edits to the files above, citing paths.)")
    lines.append("")
    lines.append("Sample Code:")
    lines.append("* (Assistant: provide an implementation example consistent with the existing patterns.)")

    return "\n".join(lines).rstrip()


def _score_label(r: SearchResult) -> str:
    """Show the rerank when the adjusted score differs from the raw score.

    Keeps the displayed number consistent with the (adjusted) ranking order so
    the agent isn't shown a LOW raw score ranked above a MODERATE one with no
    explanation.
    """
    if r.adjusted_score is not None and abs(r.adjusted_score - r.score) > 1e-9:
        return f"{r.score:.2f}→{r.adjusted_score:.2f} reranked"
    return f"{r.score:.2f}"


def _confidence_band(score: float) -> str:
    """HIGH/MODERATE/LOW band for the (possibly reranked) display score, so the
    confidence WORD stays consistent with the adjusted ranking."""
    if score >= 0.7:
        return "HIGH"
    if score >= 0.5:
        return "MODERATE"
    return "LOW"


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
