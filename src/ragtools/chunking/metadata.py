"""Frontmatter and metadata extraction from Markdown files."""

from pathlib import Path
from typing import Any

import frontmatter


def extract_frontmatter(file_path: Path) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter and body content from a Markdown file.

    Returns: (metadata_dict, body_content_string)
    If no frontmatter exists or parsing fails, returns ({}, full_content).
    """
    try:
        post = frontmatter.load(str(file_path))
        return dict(post.metadata), post.content
    except Exception:
        # Frontmatter parsing failed (malformed YAML) — treat as no frontmatter
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return {}, content


def estimate_tokens(text: str) -> int:
    """Estimate token count using word count as proxy.

    Rough heuristic: 1 word ≈ 1.33 tokens, so word_count * 1.33.
    For chunk sizing purposes this is good enough.
    """
    words = len(text.split())
    return int(words * 1.33)
