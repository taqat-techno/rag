"""File discovery and project mapping.

Supports two modes:
  - Legacy: discover projects from subdirectories of content_root (v1 config)
  - Explicit: scan configured ProjectConfig entries (v2 config)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ragtools.config import ProjectConfig
    from ragtools.ignore import IgnoreRules

logger = logging.getLogger("ragtools.indexing")


# --- Legacy (v1) functions — kept for backward compatibility ---


def discover_projects(content_root: str) -> dict[str, Path]:
    """Map immediate subdirectories to project IDs (legacy v1 behavior).

    Each immediate subdirectory of content_root is treated as a project.
    The directory name becomes the project_id.

    Returns: dict mapping project_id -> directory Path
    """
    root = Path(content_root).resolve()
    projects = {}
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.name.startswith((".", "_")):
            projects[child.name] = child
    return projects


def discover_markdown_files(
    directory: Path,
    ignore_rules: IgnoreRules | None = None,
) -> list[Path]:
    """Find all .md files in a directory recursively, respecting ignore rules.

    Args:
        directory: Directory to scan.
        ignore_rules: Ignore rules engine. If None, uses default built-in rules.

    Returns: sorted list of absolute Paths to .md files
    """
    if ignore_rules is None:
        from ragtools.ignore import IgnoreRules as IR
        ignore_rules = IR(content_root=directory)

    results = []
    for md in directory.rglob("*.md"):
        if not ignore_rules.is_ignored(md, directory):
            results.append(md)
    return sorted(results)


def discover_indexable_files(
    directory: Path,
    ignore_rules: IgnoreRules | None = None,
    mode: str = "general",
    dep_spec=None,
) -> list[Path]:
    """Find all supported files in a directory recursively, respecting ignore rules.

    "Supported" is defined by ``chunking.languages.classify_file`` — Markdown,
    source code, and config/data files. This is the broadened replacement for
    ``discover_markdown_files`` used by the indexing pipeline.

    Args:
        directory: Directory to scan.
        ignore_rules: Ignore rules engine. If None, uses default built-in rules.
        mode: Project Mode — ``"docs"`` (documentation only), ``"code"``
            (source/config/code only), or ``"general"`` (both). See
            ``config.mode_indexes``.
        dep_spec: Optional per-project dependency matcher (a ``pathspec`` from
            ``source_class.dependency_spec``). Files matching it are external
            dependency / co-located framework source and are excluded by default
            (owned-only indexing).

    Returns: sorted list of absolute Paths to indexable files.
    """
    from ragtools.chunking.languages import classify_file, DOCUMENTATION
    from ragtools.config import mode_indexes

    if ignore_rules is None:
        from ragtools.ignore import IgnoreRules as IR
        ignore_rules = IR(content_root=directory)

    results = []
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        fc = classify_file(path)
        if fc is None:
            continue
        if not mode_indexes(mode, fc.chunk_type == DOCUMENTATION):
            continue
        if ignore_rules.is_secret(path):
            continue
        if ignore_rules.is_ignored(path, directory):
            continue
        # Owned-only default: skip external dependency / co-located framework
        # source (declared dependency_paths + git submodules).
        if dep_spec is not None and dep_spec.match_file(path.relative_to(directory).as_posix()):
            continue
        results.append(path)
    return sorted(results)


def scan_project(
    content_root: str,
    project_id: str | None = None,
    ignore_rules: IgnoreRules | None = None,
    include_code: bool = True,
) -> list[tuple[str, Path]]:
    """Scan for indexable files using legacy content_root discovery (v1).

    If project_id is specified, only scan that project.
    Otherwise scan all discovered projects.

    Returns: list of (project_id, absolute_file_path) tuples
    """
    if ignore_rules is None:
        from ragtools.ignore import IgnoreRules as IR
        ignore_rules = IR(content_root=content_root)

    projects = discover_projects(content_root)

    if project_id:
        if project_id not in projects:
            raise ValueError(f"Project '{project_id}' not found in {content_root}")
        projects = {project_id: projects[project_id]}

    # v1 legacy path has no per-project Mode; map the global include_code bool:
    # True -> "general" (index everything, the historical behavior), False -> "docs".
    mode = "general" if include_code else "docs"
    results = []
    for pid, project_dir in projects.items():
        for f in discover_indexable_files(project_dir, ignore_rules=ignore_rules, mode=mode):
            results.append((pid, f))
    return results


def get_relative_path(file_path: Path, content_root: str) -> str:
    """Get relative path from content root for storage (legacy v1)."""
    root = Path(content_root).resolve()
    return file_path.resolve().relative_to(root).as_posix()


# --- Explicit project (v2) functions ---


def scan_configured_projects(
    projects: list[ProjectConfig],
    global_ignore_patterns: list[str] | None = None,
    use_ragignore: bool = True,
    secret_allowlist: list[str] | None = None,
) -> list[tuple[str, Path]]:
    """Scan explicitly configured projects for markdown files (v2).

    Handles nested project paths: if project A = C:/docs and project B = C:/docs/sub,
    files in sub/ are only assigned to project B (the deeper one). Project A
    automatically excludes files that belong to a more specific child project.

    Args:
        projects: List of ProjectConfig entries. Only enabled projects are scanned.
        global_ignore_patterns: Global ignore patterns (applied to all projects).
        use_ragignore: Whether to parse .ragignore files in project directories.

    Returns: list of (project_id, absolute_file_path) tuples.
    """
    from ragtools.ignore import IgnoreRules

    # Build resolved path map for all projects (including disabled, for exclusion)
    all_resolved = {p.id: Path(p.path).resolve() for p in projects}

    results = []
    for project in projects:
        if not project.enabled:
            continue

        project_path = Path(project.path).resolve()
        if not project_path.exists() or not project_path.is_dir():
            logger.warning("Project '%s' path does not exist: %s", project.id, project.path)
            continue

        # Find child project paths nested inside this project
        child_paths = [
            rp for pid, rp in all_resolved.items()
            if pid != project.id and _is_subpath(rp, project_path)
        ]

        # Merge global + per-project ignore patterns
        combined_patterns = list(global_ignore_patterns or []) + list(project.ignore_patterns)

        ignore_rules = IgnoreRules(
            content_root=project.path,
            global_patterns=combined_patterns,
            use_ragignore=use_ragignore,
            secret_allowlist=secret_allowlist,
        )

        # Owned-only default: external dependency / co-located framework roots
        # (declared dependency_paths + git submodules) are excluded.
        from ragtools.source_class import dependency_spec
        proj_dep_spec = dependency_spec(project_path, project.dependency_paths)

        # Per-project Mode (docs / code / general) governs what is indexed.
        for found in discover_indexable_files(
            project_path, ignore_rules=ignore_rules, mode=project.mode, dep_spec=proj_dep_spec
        ):
            # Skip files that belong to a more specific child project
            if child_paths:
                file_resolved = found.resolve()
                owned_by_child = any(
                    str(file_resolved).startswith(str(cp) + os.sep) or file_resolved == cp
                    for cp in child_paths
                )
                if owned_by_child:
                    continue
            results.append((project.id, found))

    return results


def _is_subpath(path: Path, parent: Path) -> bool:
    """Check if path is inside parent directory (strict — not equal)."""
    try:
        path.relative_to(parent)
        return path != parent
    except ValueError:
        return False


def get_project_relative_path(file_path: Path, project_path: str, project_id: str) -> str:
    """Get storage path for a file in an explicit project (v2).

    Returns: '{project_id}/{relative_from_project_root}'
    Example: file at C:/wiki/docs/readme.md with project path C:/wiki
    and project_id 'wiki' -> 'wiki/docs/readme.md'
    """
    root = Path(project_path).resolve()
    rel = file_path.resolve().relative_to(root).as_posix()
    return f"{project_id}/{rel}"
