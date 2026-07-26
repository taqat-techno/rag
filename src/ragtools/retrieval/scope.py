"""Fail-closed scope resolution (RAG v3, Stage S1 / A2).

A requested-but-empty project scope, or an unscoped call without an explicit
opt-in, must never silently widen to a global search. This module is the pure,
Qdrant-free contract for that decision; :class:`~ragtools.retrieval.searcher.Searcher`
builds the actual query filter from a :class:`ScopeDecision`.

Invariant (plan I2/I3): an unresolved project fails closed; another project is
never searched without explicit, authorized intent. The single sanctioned way
to search everything is ``allow_unscoped=True`` — a decision the router/profile
layer (S10/S12) makes, never an accident of empty input.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S1/A2 -> G1)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


class ScopeUnresolvedError(ValueError):
    """A search scope could not be resolved and must not widen to global.

    Raised for a requested-but-empty scope (``[]``, blanks) or an unscoped
    call without ``allow_unscoped=True``. Callers surface this as an explicit
    refusal (e.g. MCP ``SCOPE_UNRESOLVED``), never as a silent global search.
    """


@dataclass(frozen=True)
class ScopeDecision:
    """Resolved search scope. ``project_ids`` is empty iff ``unscoped``."""

    project_ids: tuple[str, ...]
    unscoped: bool


def _clean(values: Sequence[str]) -> list[str]:
    return [v.strip() for v in values if v and v.strip()]


def resolve_scope(
    project_id: Optional[str] = None,
    project_ids: Optional[Sequence[str]] = None,
    *,
    allow_unscoped: bool = False,
) -> ScopeDecision:
    """Resolve a fail-closed search scope from caller input.

    * ``project_ids`` (non-None) takes precedence; blanks are stripped. If it
      resolves to nothing, that is a requested-but-empty scope → **refuse**.
    * else a non-blank ``project_id`` → single-project scope.
    * else a blank ``project_id`` (requested but empty) → **refuse**.
    * else both absent → refuse, unless ``allow_unscoped`` explicitly permits a
      global search.

    Raises :class:`ScopeUnresolvedError` on any refusal.
    """
    if project_ids is not None:
        cleaned = _clean(project_ids)
        if not cleaned:
            raise ScopeUnresolvedError(
                "empty project scope requested (project_ids resolved to no "
                "valid ids); refusing to widen to a global search"
            )
        return ScopeDecision(project_ids=tuple(cleaned), unscoped=False)

    if project_id is not None:
        pid = project_id.strip()
        if not pid:
            raise ScopeUnresolvedError(
                "blank project scope requested (project_id is empty); refusing "
                "to widen to a global search"
            )
        return ScopeDecision(project_ids=(pid,), unscoped=False)

    if allow_unscoped:
        return ScopeDecision(project_ids=(), unscoped=True)

    raise ScopeUnresolvedError(
        "no project scope resolved; refusing to search globally. Pass an "
        "explicit project/projects, or opt in to an authorized global search."
    )
