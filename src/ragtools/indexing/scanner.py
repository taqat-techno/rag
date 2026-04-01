"""File discovery and project mapping."""

from pathlib import Path


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


SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "__pycache__", "node_modules",
    "site-packages", ".tox", ".mypy_cache", ".pytest_cache", ".hypothesis",
    "dist", "build", ".egg-info", ".stversions",
}


def discover_markdown_files(directory: Path) -> list[Path]:
    """Find all .md files in a directory recursively, skipping common noise dirs.

    Returns: sorted list of absolute Paths to .md files
    """
    results = []
    for md in directory.rglob("*.md"):
        # Skip files inside noise directories
        if not any(part in SKIP_DIRS for part in md.parts):
            results.append(md)
    return sorted(results)


def scan_project(content_root: str, project_id: str | None = None) -> list[tuple[str, Path]]:
    """Scan for markdown files, yielding (project_id, file_path) pairs.

    If project_id is specified, only scan that project.
    Otherwise scan all discovered projects.

    Returns: list of (project_id, absolute_file_path) tuples
    """
    projects = discover_projects(content_root)

    if project_id:
        if project_id not in projects:
            raise ValueError(f"Project '{project_id}' not found in {content_root}")
        projects = {project_id: projects[project_id]}

    results = []
    for pid, project_dir in projects.items():
        for md_file in discover_markdown_files(project_dir):
            results.append((pid, md_file))
    return results


def get_relative_path(file_path: Path, content_root: str) -> str:
    """Get relative path from content root for storage."""
    root = Path(content_root).resolve()
    return file_path.resolve().relative_to(root).as_posix()
