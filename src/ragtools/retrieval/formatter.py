"""Format search results into context blocks for Claude."""

import re

from ragtools.models import SearchResult

_VERSION_SUFFIX_RE = re.compile(r"_v\d+")


def _loc(r: SearchResult) -> str:
    """Source path with a 1-based line span suffix (when known) for jump-to-source."""
    src = f"{r.project_id}/{r.file_path}" if r.project_id else r.file_path
    start = getattr(r, "line_start", 0) or 0
    end = getattr(r, "line_end", 0) or 0
    if start:
        return f"{src}:L{start}-{end}" if end and end != start else f"{src}:L{start}"
    return src


def _class_tag(r: SearchResult) -> str:
    """Short tag for non-owned content (vendor/generated) so the agent can discount it."""
    sc = getattr(r, "source_class", "owned") or "owned"
    return f" [{sc}]" if sc != "owned" else ""


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
            f"[{i}] Source: {_loc(r)}{_class_tag(r)}"
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
        # Compact header: [N] project/file.md:Lstart-end > Heading (score):
        heading = r.headings[-1] if r.headings else ""
        lines.append(f"[{i}] {_loc(r)}{_class_tag(r)} > {heading} ({r.score:.2f}):")
        lines.append(_truncate(r.text))
        lines.append("")

    return "\n".join(lines).rstrip()


def format_definitions(symbol: str, defs: list) -> str:
    """Format symbol-definition leads (cross-file code graph) for agent output."""
    if not defs:
        return (
            f"[CODE GRAPH] No definition found for '{symbol}'. It may not exist, may "
            "live in an unindexed file (check the project's Mode/coverage), or be "
            "defined dynamically. This is NOT proof of absence — confirm with grep/LSP."
        )
    lines = [
        f"[CODE GRAPH] Definition leads for '{symbol}' ({len(defs)} found) — "
        "discovery, not authority; verify exact definitions with an LSP:"
    ]
    for d in defs:
        loc = f"{d.get('project_id', '')}/{d['file_path']}" if d.get("project_id") else d["file_path"]
        if d.get("line_start"):
            loc += f":L{d['line_start']}"
        if d.get("function_name"):
            kind = f"def {d['function_name']}"
        elif d.get("class_name"):
            kind = f"class {d['class_name']}"
        else:
            kind = d.get("signature") or "symbol"
        lines.append(f"* {loc} — {kind} [{d.get('match', 'mention')}]")
    return "\n".join(lines)


def format_secret_audit(result: dict) -> str:
    """Format a secret-audit result for agent output — file:line + rule, no values."""
    findings = result.get("findings", [])
    scanned = result.get("scanned", 0)
    if not findings:
        return f"[SECRET AUDIT] Scanned {scanned} chunks — no secrets detected in the index."
    lines = [
        f"[SECRET AUDIT] {len(findings)} file(s) contain (or had) secret material "
        f"out of {scanned} chunks scanned. Rotate the credential and scrub the source:"
    ]
    for f in findings:
        loc = f"{f.get('project_id', '')}/{f.get('file_path', '')}"
        if f.get("line_start"):
            loc += f":L{f['line_start']}"
        rules = ", ".join(f.get("rules") or []) or "redacted"
        marker = f" (+{f['redacted_markers']} already masked)" if f.get("redacted_markers") else ""
        lines.append(f"* {loc} — {rules}{marker}")
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


def format_dev_context(results: list[SearchResult], query: str, triggers: list[str] | None = None,
                       warnings: list[str] | None = None, code_indexed: "bool | None" = None) -> str:
    """Format retrieved project context for a development/feature request.

    Produces the required "Project Context Mode" response scaffold:

        Relevant Files:
        Existing Implementation:
        Recommended Changes:    (assistant fills from the request)
        Sample Code:            (assistant fills from the request)

    The "Relevant Files" and "Existing Implementation" sections are grounded in
    the retrieved chunks so the generated answer references actual repository
    files. The latter two sections are scaffolds the assistant completes.

    ``warnings`` (e.g. "this project is in Docs mode; source code is not indexed")
    are surfaced at the very top so the agent reads them before the results.
    """
    trigger_note = f" (triggers: {', '.join(triggers)})" if triggers else ""
    warn_block = ""
    if warnings:
        warn_block = "\n".join(f"[PROJECT CONTEXT — WARNING] {w}" for w in warnings) + "\n\n"

    if not results:
        # Separate "not indexed" (can't answer — wrong Mode) from "not found"
        # (honest absence — code IS indexed) so the agent doesn't read an empty
        # result as "the feature does not exist".
        if code_indexed is False:
            body = (
                f"[PROJECT CONTEXT — code not indexed]{trigger_note}\n"
                "This project indexes documentation only (Docs mode); its source "
                "code is NOT indexed. An empty result here is NOT evidence the "
                f"feature is absent for: '{query}'. Switch the project to Code or "
                "General mode and re-index to search code, or use grep/LSP now."
            )
        elif code_indexed is True:
            body = (
                f"[PROJECT CONTEXT — not found]{trigger_note}\n"
                f"Source code IS indexed for this project, and no code matched: "
                f"'{query}'. Treat this as a genuine not-found — the symbol/feature "
                "is likely absent (confirm with grep/LSP before concluding)."
            )
        else:
            body = (
                f"[PROJECT CONTEXT — no matches]{trigger_note}\n"
                f"No indexed project files matched: '{query}'. "
                "State that no project-specific implementation was found, then proceed "
                "with general guidance clearly labeled as not grounded in the repository."
            )
        return warn_block + body

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
        if r.signature:
            bits.append(r.signature)
        if r.class_name:
            bits.append(f"class {r.class_name}")
        if r.function_name:
            bits.append(f"fn {r.function_name}")
        if r.imports:
            bits.append("imports: " + ", ".join(r.imports[:5]))
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
        sym = f" | {' > '.join(r.headings)}" if r.headings else ""
        lines.append(f"[{i}] {_loc(r)}{_class_tag(r)}{sym} ({r.chunk_type}, {_score_label(r)}):")
        lines.append(_truncate(r.text, 800))
        lines.append("")

    lines.append("Recommended Changes:")
    lines.append("* (Assistant: list concrete edits to the files above, citing paths.)")
    lines.append("")
    lines.append("Sample Code:")
    lines.append("* (Assistant: provide an implementation example consistent with the existing patterns.)")

    return warn_block + "\n".join(lines).rstrip()


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
