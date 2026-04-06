"""File discovery and project mapping."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ragtools.ignore import IgnoreRules


def discover_projects(content_root: str) -> dict[str, Path]:
    """Map immediate subdirectories to project IDs.

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
    """Scan for markdown files, yielding (project_id, file_path) pairs.

    If project_id is specified, only scan that project.
    Otherwise scan all discovered projects.

    Args:
        content_root: Root directory containing project subdirectories.
        project_id: Optional single project to scan.
        ignore_rules: Ignore rules engine. If None, uses default built-in rules.

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
    """Get relative path from content root for storage."""
    root = Path(content_root).resolve()
    return file_path.resolve().relative_to(root).as_posix()
