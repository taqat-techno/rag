"""File discovery and project mapping.

Supports two modes:
  - Legacy: discover projects from subdirectories of content_root (v1 config)
  - Explicit: scan configured ProjectConfig entries (v2 config)
"""

from __future__ import annotations

import logging
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


def scan_project(
    content_root: str,
    project_id: str | None = None,
    ignore_rules: IgnoreRules | None = None,
) -> list[tuple[str, Path]]:
    """Scan for markdown files using legacy content_root discovery (v1).

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

    results = []
    for pid, project_dir in projects.items():
        for md_file in discover_markdown_files(project_dir, ignore_rules=ignore_rules):
            results.append((pid, md_file))
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
) -> list[tuple[str, Path]]:
    """Scan explicitly configured projects for markdown files (v2).

    Args:
        projects: List of ProjectConfig entries. Only enabled projects are scanned.
        global_ignore_patterns: Global ignore patterns (applied to all projects).
        use_ragignore: Whether to parse .ragignore files in project directories.

    Returns: list of (project_id, absolute_file_path) tuples.
    """
    from ragtools.ignore import IgnoreRules

    results = []
    for project in projects:
        if not project.enabled:
            continue

        project_path = Path(project.path)
        if not project_path.exists() or not project_path.is_dir():
            logger.warning("Project '%s' path does not exist: %s", project.id, project.path)
            continue

        # Merge global + per-project ignore patterns
        combined_patterns = list(global_ignore_patterns or []) + list(project.ignore_patterns)

        ignore_rules = IgnoreRules(
            content_root=project.path,
            global_patterns=combined_patterns,
            use_ragignore=use_ragignore,
        )

        for md_file in discover_markdown_files(project_path, ignore_rules=ignore_rules):
            results.append((project.id, md_file))

    return results


def get_project_relative_path(file_path: Path, project_path: str, project_id: str) -> str:
    """Get storage path for a file in an explicit project (v2).

    Returns: '{project_id}/{relative_from_project_root}'
    Example: file at C:/wiki/docs/readme.md with project path C:/wiki
    and project_id 'wiki' -> 'wiki/docs/readme.md'
    """
    root = Path(project_path).resolve()
    rel = file_path.resolve().relative_to(root).as_posix()
    return f"{project_id}/{rel}"
