"""Helpers for bulk-adding projects from a glob pattern.

Motivation
----------
Users with many sibling folders to index (e.g. `D:/Work/*/docs`) previously
had to run `rag project add` once per folder or maintain fragile batch
scripts. This module turns a glob pattern into a reviewed add-plan that the
CLI executes in one pass.

Design
------
Pure helpers only — no subprocess, no HTTP, no file writes. The CLI command
composes these with its own side-effects. That keeps the logic fully
testable (see tests/test_project_glob.py) and reusable by a future batch
endpoint in the service.
"""

from __future__ import annotations

import glob
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from ragtools.config import ProjectConfig


class PlanKind(str, Enum):
    """What will happen to a matched path when the plan is executed."""

    NEW = "NEW"              # fresh project, unique id
    RENAMED = "RENAMED"      # fresh project but id collided, auto-suffixed
    DUPLICATE = "DUPLICATE"  # path already registered — skip
    INVALID = "INVALID"      # path doesn't exist or isn't a directory — skip


@dataclass
class PlannedAdd:
    """One row in the plan. Represents the *intent*, not the result."""

    path: Path          # absolute, resolved
    kind: PlanKind
    project_id: str = ""
    name: str = ""
    reason: str = ""    # human-readable explanation for DUPLICATE/RENAMED/INVALID

    @property
    def actionable(self) -> bool:
        """True if this entry should be submitted as an add."""
        return self.kind in (PlanKind.NEW, PlanKind.RENAMED)


# ---------------------------------------------------------------------------
# Glob expansion
# ---------------------------------------------------------------------------


def expand_glob(
    pattern: str,
    excludes: Sequence[str] = (),
) -> List[Path]:
    """Return the directories matched by `pattern`, filtered and sorted.

    - Uses Python's `glob.glob(recursive=True)` semantics (`**` walks trees).
    - Keeps only entries that are existing directories.
    - Drops anything matching ANY of the `excludes` glob patterns.
    - Results are returned sorted for deterministic plan output.
    """
    raw = glob.glob(pattern, recursive=True)
    dirs = [Path(p).resolve() for p in raw if Path(p).is_dir()]

    if excludes:
        excluded: set[Path] = set()
        for ex in excludes:
            for match in glob.glob(ex, recursive=True):
                p = Path(match).resolve()
                if p.is_dir():
                    excluded.add(p)
        dirs = [d for d in dirs if d not in excluded]

    # Deduplicate (a path can match multiple glob branches) and sort.
    seen: set[Path] = set()
    unique: List[Path] = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    unique.sort()
    return unique


# ---------------------------------------------------------------------------
# ID slugification + collision handling
# ---------------------------------------------------------------------------


_SLUG_INVALID = re.compile(r"[^a-z0-9-]")
_SLUG_COLLAPSE = re.compile(r"-+")


def slugify_id(text: str) -> str:
    """Turn a folder basename or display name into a project id.

    Matches the same rules `rag project add` uses (lowercase, hyphen-only).
    Returns "" if the text has no alphanumerics — the caller decides what
    to do with an empty id.
    """
    lowered = text.lower()
    hyphenated = _SLUG_INVALID.sub("-", lowered)
    collapsed = _SLUG_COLLAPSE.sub("-", hyphenated).strip("-")
    return collapsed


def _disambiguate(candidate: str, taken: Iterable[str]) -> str:
    """If `candidate` is taken, append `-2`, `-3`, ... until unused."""
    taken_set = set(taken)
    if candidate not in taken_set:
        return candidate
    i = 2
    while f"{candidate}-{i}" in taken_set:
        i += 1
    return f"{candidate}-{i}"


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------


def derive_plan(
    paths: Sequence[Path],
    existing: Sequence[ProjectConfig],
    name_prefix: str = "",
) -> List[PlannedAdd]:
    """Turn a list of candidate paths into a reviewed add-plan.

    Args:
        paths: absolute paths the caller wants to add (already resolved).
        existing: currently-configured projects; used to detect duplicate
            paths and reserved ids.
        name_prefix: optional string prepended to every display name. Empty
            by default. The id is *not* prefixed — we slugify the prefix
            only for id disambiguation purposes so user-facing ids stay
            short.

    Returns:
        A PlannedAdd for every input path (never drops entries — caller
        may want to display INVALID/DUPLICATE rows as feedback).
    """
    existing_paths = {Path(p.path).resolve() for p in existing}
    reserved_ids: set[str] = {p.id for p in existing}

    plan: List[PlannedAdd] = []
    for p in paths:
        resolved = p.resolve() if not p.is_absolute() else p
        if not resolved.exists() or not resolved.is_dir():
            plan.append(PlannedAdd(
                path=resolved,
                kind=PlanKind.INVALID,
                reason="Path is not an existing directory",
            ))
            continue

        if resolved in existing_paths:
            plan.append(PlannedAdd(
                path=resolved,
                kind=PlanKind.DUPLICATE,
                reason="Already registered",
            ))
            continue

        base_name = resolved.name or "project"
        base_id = slugify_id(base_name) or "project"
        unique_id = _disambiguate(base_id, reserved_ids)
        kind = PlanKind.RENAMED if unique_id != base_id else PlanKind.NEW
        reason = f"id '{base_id}' taken, renamed to '{unique_id}'" if kind == PlanKind.RENAMED else ""

        display_name = f"{name_prefix}{base_name}" if name_prefix else base_name

        plan.append(PlannedAdd(
            path=resolved,
            kind=kind,
            project_id=unique_id,
            name=display_name,
            reason=reason,
        ))
        reserved_ids.add(unique_id)
        existing_paths.add(resolved)  # protect against same-path twice in input

    return plan


def plan_summary(plan: Sequence[PlannedAdd]) -> dict[str, int]:
    """Count entries by kind for reporting."""
    summary: dict[str, int] = {k.value: 0 for k in PlanKind}
    for entry in plan:
        summary[entry.kind.value] += 1
    return summary
