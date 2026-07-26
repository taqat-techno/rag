"""Catalog operations for shared dependencies — pure, no I/O beyond `stat`.

Every surface that can change the catalog (HTTP API, admin panel, MCP) routes
through these functions, so a rule cannot be enforced in one place and missed in
another. That has already bitten this feature once: the dry-run preview and the
writer were separate code paths, and a preview that validates differently from
the writer blesses a path the indexer then rejects.

Two kinds of validity, deliberately separate:

* **Catalog validity** — is this a folder, and is it already in the catalog
  under another id? Project-independent.
* **Link validity** — may THIS project use it? A dependency that is a project's
  own root, or a parent of it, is fine as a catalog entry and illegal as a link
  for that project. So a perfectly valid entry can be un-linkable to one project
  and fine for every other, and the UI must say which.

Plan: docs/planning/RAG_DEPENDENCY_CATALOG_PLAN.md
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from ragtools.config import (
    DependencyConfig,
    ProjectConfig,
    _dependency_path_key,
    _slugify_dependency_id,
    _unique_dependency_id,
)

__all__ = [
    "CatalogError", "LinkCheck", "normalize_id", "find_by_path",
    "validate_new_entry", "check_link", "check_links", "validate_link_set",
    "resolve_project_dependency_paths", "projects_using", "unlink_everywhere",
    # Re-exported so every caller has ONE import site for catalog identity.
    "_dependency_path_key", "_unique_dependency_id",
]


class CatalogError(ValueError):
    """A catalog rule was violated. The message is user-facing."""


@dataclass(frozen=True)
class LinkCheck:
    """Whether one project may link one dependency, and why not."""

    dependency_id: str
    ok: bool
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return not self.ok


def normalize_id(raw: str) -> str:
    """Canonical catalog id, or raise if nothing usable remains."""
    slug = _slugify_dependency_id(raw or "")
    if not slug or slug == "dependency" and not (raw or "").strip():
        raise CatalogError("A dependency id is required.")
    return slug


def find_by_path(
    dependencies: Iterable[DependencyConfig], path: str
) -> Optional[DependencyConfig]:
    """The catalog entry for a folder, whatever spelling was used."""
    key = _dependency_path_key(path)
    return next((d for d in dependencies if _dependency_path_key(d.path) == key), None)


def validate_new_entry(
    dependencies: Iterable[DependencyConfig],
    dep_id: str,
    path: str,
    *,
    exclude_id: str | None = None,
) -> str:
    """Check an add/edit and return the resolved absolute path.

    ``exclude_id`` lets an edit keep its own id/path without colliding with
    itself.
    """
    existing = list(dependencies)
    dep_id = normalize_id(dep_id)

    if not (path or "").strip():
        raise CatalogError("A folder path is required.")
    candidate = Path(path.strip())
    if not candidate.is_absolute():
        raise CatalogError(
            f"{path!r} must be an absolute path — a catalog entry is shared by "
            "many projects, so it has no project to be relative to."
        )
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise CatalogError(f"{path!r} cannot be resolved ({exc}).") from exc
    if not resolved.is_dir():
        raise CatalogError(f"{path!r} does not exist or is not a folder.")

    for entry in existing:
        if entry.id == exclude_id:
            continue
        if entry.id == dep_id:
            raise CatalogError(f"A dependency with id {dep_id!r} already exists.")
        if _dependency_path_key(entry.path) == _dependency_path_key(resolved):
            raise CatalogError(
                f"That folder is already in the catalog as {entry.id!r} "
                f"({entry.name}). Link that one instead of adding it twice."
            )
    return str(resolved)


def check_link(project: ProjectConfig, entry: DependencyConfig) -> LinkCheck:
    """May ``project`` use ``entry``?

    The rules are the same ones ``resolve_dependency_roots`` enforces at sync
    time; applying them at link time is what turns a silent, delayed failure
    into an answer at the moment of choosing.
    """
    try:
        dep = Path(entry.path).resolve()
    except OSError as exc:
        return LinkCheck(entry.id, False, f"cannot be resolved ({exc})")
    if not dep.is_dir():
        return LinkCheck(entry.id, False, "the folder no longer exists on disk")

    try:
        root = Path(project.path).resolve()
    except OSError as exc:
        return LinkCheck(entry.id, False, f"the project path cannot be resolved ({exc})")

    if dep == root:
        return LinkCheck(
            entry.id, False,
            "this is the project's own folder — linking it would exclude every "
            "file from the project and index nothing as project-owned code",
        )
    if root.is_relative_to(dep):
        return LinkCheck(
            entry.id, False,
            "this folder contains the project — a dependency cannot be a parent "
            "of the project that uses it",
        )
    return LinkCheck(entry.id, True)


def check_links(
    project: ProjectConfig, dependencies: Iterable[DependencyConfig]
) -> list[LinkCheck]:
    """Link verdicts for every catalog entry, for the project edit form."""
    return [check_link(project, entry) for entry in dependencies]


def validate_link_set(
    project: ProjectConfig,
    dependencies: Iterable[DependencyConfig],
    requested: Iterable[str],
) -> list[str]:
    """Validate a full replacement link list; return it de-duplicated.

    Replacing the whole set (rather than add/remove deltas) is what makes the
    multi-select honest: what the form shows IS what gets stored, with no
    invisible leftovers from a previous edit.
    """
    catalog = {entry.id: entry for entry in dependencies}
    out: list[str] = []
    for dep_id in requested:
        dep_id = (dep_id or "").strip()
        if not dep_id or dep_id in out:
            continue
        entry = catalog.get(dep_id)
        if entry is None:
            raise CatalogError(
                f"No dependency named {dep_id!r} in the catalog. "
                f"Add it on the Dependencies page first."
            )
        verdict = check_link(project, entry)
        if verdict.blocked:
            raise CatalogError(f"{entry.name!r} cannot be used here: {verdict.reason}.")
        out.append(dep_id)
    return out


def resolve_project_dependency_paths(
    project: ProjectConfig, dependencies: Iterable[DependencyConfig] | None
) -> list[str]:
    """Every dependency root a project uses — catalog links plus legacy paths.

    THE single definition of "what does this project treat as a dependency".
    The scanner (which excludes these roots from the project's own files) and
    the sync (which indexes them into a shared corpus) must agree exactly: if
    the scanner excludes a tree the sync does not index, the content vanishes
    from search; if the sync indexes a tree the scanner does not exclude, every
    hit appears twice. One function, both callers.
    """
    catalog = {entry.id: entry for entry in (dependencies or [])}
    paths: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        key = _dependency_path_key(value)
        if key not in seen:
            seen.add(key)
            paths.append(value)

    for dep_id in project.dependencies or []:
        entry = catalog.get(dep_id)
        # Unknown or disabled contributes nothing, and is NOT fatal here: the
        # API refuses bad links at write time, and a sync must never fail
        # wholesale because one entry was removed underneath it.
        if entry is not None and entry.enabled:
            _add(entry.path)

    for raw in project.dependency_paths or []:
        text = str(raw).strip()
        if not text:
            continue
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = Path(project.path) / candidate
        _add(str(candidate))
    return paths


def projects_using(
    projects: Iterable[ProjectConfig], dep_id: str
) -> list[str]:
    """Ids of the projects linking a catalog entry."""
    return [p.id for p in projects if dep_id in (p.dependencies or [])]


def unlink_everywhere(projects: Iterable[ProjectConfig], dep_id: str) -> list[str]:
    """Drop a catalog id from every project. Returns the projects changed."""
    changed = []
    for project in projects:
        if dep_id in (project.dependencies or []):
            project.dependencies = [d for d in project.dependencies if d != dep_id]
            changed.append(project.id)
    return changed
