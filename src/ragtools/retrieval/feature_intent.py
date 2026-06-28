"""Detect development/feature-request intent in a user prompt.

When a prompt looks like a development request (new feature, bug fix,
refactor, API/workflow change, architecture review), the RAG layer should
retrieve project code/docs *before* answering. This module centralizes the
keyword/phrase detection so the MCP tool, the service, and the tests all agree
on what counts as a development request.
"""

from __future__ import annotations

import re

# Multi-word trigger phrases (matched as substrings, case-insensitive).
TRIGGER_PHRASES = [
    "add feature",
    "new feature",
    "implement",
    "implementation",
    "create endpoint",
    "add endpoint",
    "create api",
    "add api",
    "modify workflow",
    "change workflow",
    "workflow enhancement",
    "enhance workflow",
    "extend module",
    "enhance system",
    "architecture review",
    "architecture change",
    "refactor",
    "bug fix",
    "fix the bug",
    "fix a bug",
    "api modification",
    "modify the api",
    "add a route",
    "create a service",
]

# Single-word triggers (matched on word boundaries).
TRIGGER_WORDS = [
    "implement",
    "refactor",
    "endpoint",
    "feature",
    "debug",
    "integrate",
    "migrate",
]

_PHRASE_RES = [re.compile(re.escape(p), re.IGNORECASE) for p in TRIGGER_PHRASES]
_WORD_RES = [re.compile(rf"\b{re.escape(w)}\w*\b", re.IGNORECASE) for w in TRIGGER_WORDS]


def detect_dev_intent(prompt: str) -> bool:
    """Return True if the prompt is a development/feature request.

    Used to decide whether to run the layered codebase-first retrieval
    pipeline (Project Context Mode) before generating an answer.
    """
    if not prompt:
        return False
    return bool(matched_triggers(prompt))


def matched_triggers(prompt: str) -> list[str]:
    """Return the list of trigger phrases/words found in the prompt."""
    if not prompt:
        return []
    hits: list[str] = []
    for phrase, rx in zip(TRIGGER_PHRASES, _PHRASE_RES):
        if rx.search(prompt):
            hits.append(phrase)
    for word, rx in zip(TRIGGER_WORDS, _WORD_RES):
        if rx.search(prompt) and word not in hits:
            hits.append(word)
    # de-dup, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out
