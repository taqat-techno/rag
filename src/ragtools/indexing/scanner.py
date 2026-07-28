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


class SkipLedger:
    """Paths a scan could not or would not read, and why.

    Skips used to be invisible. `discover_indexable_files` had no exception
    handling at all, so a path the OS refused to describe raised straight out of
    the scan, through the per-project loop, and into the one blanket
    ``except Exception`` around the whole startup sync — where it was logged as
    "Startup sync failed (non-fatal)". One untraversable junction inside one
    project therefore stopped indexing for EVERY project, on every boot, and
    said so in a line containing the word "non-fatal".

    Counting them is the other half. A scan that silently drops files reports
    the same success as one that read everything, so the number is surfaced and
    the first few examples are named.
    """

    __slots__ = ("unreadable", "loops", "examples")

    def __init__(self) -> None:
        self.unreadable: int = 0
        self.loops: int = 0
        self.examples: list[str] = []

    def record(self, path, reason: str, kind: str = "unreadable") -> None:
        if kind == "loop":
            self.loops += 1
        else:
            self.unreadable += 1
        if len(self.examples) < 5:
            self.examples.append(f"{path}: {reason}")

    @property
    def total(self) -> int:
        return self.unreadable + self.loops

    def describe(self) -> str:
        parts = []
        if self.unreadable:
            parts.append(f"{self.unreadable} unreadable")
        if self.loops:
            parts.append(f"{self.loops} already-visited (link loop)")
        detail = ", ".join(parts)
        if self.examples:
            detail += " — e.g. " + "; ".join(self.examples)
        return detail

    def log(self, scope: str) -> None:
        if self.total:
            logger.warning("Scan of %s skipped %s", scope, self.describe())


def _inspect(path: Path, ledger: SkipLedger):
    """``(is_regular_file, identity)`` from a SINGLE stat, or ``(False, None)``.

    One stat, not two. ``is_file()`` stats internally and so does any separate
    identity lookup, which on a real corpus of ~38,000 files is 38,000 wasted
    syscalls per scan for no added information.

    **The OS refusing to answer is a skip, not a crash.** Statting resolves a
    reparse point, and Windows declines to traverse some of them — observed in
    the field as ``[WinError 448] The provided mount point is not trusted`` on a
    junction inside a generated ``node_modules`` tree, which propagated out of
    the entire scan.

    Catches ``OSError`` broadly rather than a Windows error code on purpose: the
    POSIX equivalents (``EACCES`` mid-walk, a dangling symlink, a path deleted
    between listing and statting, ``ELOOP``) are the same class of "the
    filesystem will not answer" and deserve the same answer. A fix shaped around
    one platform's error number would leave the others raising.

    Identity is ``(st_dev, st_ino)`` — the filesystem's own answer, and equal for
    two paths naming one file through a link, which is exactly the case being
    guarded. Filesystems without inodes report ``st_ino == 0``; there the
    resolved path is the best available answer, and where even that fails the
    caller keeps the file. Losing a real file to over-eager de-duplication would
    be much worse than indexing one twice.
    """
    import stat as _stat

    try:
        info = path.stat()
    except OSError as exc:
        ledger.record(path, f"{type(exc).__name__}: {exc}")
        return False, None

    if not _stat.S_ISREG(info.st_mode):
        return False, None
    if info.st_ino:
        return True, (info.st_dev, info.st_ino)
    try:
        return True, str(path.resolve()).casefold()
    except OSError:
        return True, None


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
    ledger: "SkipLedger | None" = None,
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

    ledger = ledger if ledger is not None else SkipLedger()
    results = []
    #: Identity of every file already yielded, so a junction that points back up
    #: the tree cannot yield the same file repeatedly under different paths.
    #:
    #: `rglob` follows junctions and reparse points, and nothing stopped it
    #: recursing through a loop. Measured on a self-referential junction: the
    #: scan reached depth 23 and returned 23 copies of one file — bounded only
    #: by Windows' path-length limit, so raising that limit deepens the loop
    #: rather than fixing it. Every copy would be chunked, embedded and stored
    #: separately.
    seen: set = set()
    for path in directory.rglob("*"):
        is_file, key = _inspect(path, ledger)
        if not is_file:
            continue
        if key is not None:
            if key in seen:
                ledger.record(path, "already reached by another path", kind="loop")
                continue
            seen.add(key)
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
    dependencies: list | None = None,
    ledger: "SkipLedger | None" = None,
) -> list[tuple[str, Path]]:
    """Scan explicitly configured projects for markdown files (v2).

    Handles nested project paths: if project A = C:/docs and project B = C:/docs/sub,
    files in sub/ are only assigned to project B (the deeper one). Project A
    automatically excludes files that belong to a more specific child project.

    Args:
        projects: List of ProjectConfig entries. Only enabled projects are scanned.
        global_ignore_patterns: Global ignore patterns (applied to all projects).
        use_ragignore: Whether to parse .ragignore files in project directories.
        dependencies: The shared-dependency catalog (`Settings.dependencies`).
            Required for projects that link catalog entries rather than
            declaring raw paths — without it their dependency roots are NOT
            excluded here, and the same files end up both in the project's own
            collection and in the shared corpus.

    Returns: list of (project_id, absolute_file_path) tuples.
    """
    from ragtools.ignore import IgnoreRules

    # Build resolved path map for all projects (including disabled, for exclusion)
    all_resolved = {p.id: Path(p.path).resolve() for p in projects}

    ledger = ledger if ledger is not None else SkipLedger()
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
        #
        # Declared roots are resolved first (symlinks, junctions, `..`, absolute
        # vs relative, drive-letter case) and validated — a declaration naming
        # the project root itself is REFUSED rather than silently excluding
        # every file in the project. Resolution also means an absolute path and
        # its relative spelling produce the same exclusion instead of only the
        # literal string matching.
        from ragtools.dependency_catalog import resolve_project_dependency_paths
        from ragtools.frameworks import exclusion_globs_for
        from ragtools.source_class import dependency_spec
        # Catalog links AND legacy raw paths — the same union the sync indexes.
        declared_roots = resolve_project_dependency_paths(project, dependencies)
        proj_dep_spec = dependency_spec(
            project_path,
            list(project.dependency_paths) + exclusion_globs_for(
                project_path, declared_roots),
        )

        # Per-project Mode (docs / code / general) governs what is indexed.
        #
        # One project's failure must not become every project's failure. The
        # inner scan is already tolerant of individual unreadable paths, but a
        # project whose ROOT becomes unusable mid-scan — a disconnected network
        # share, a revoked permission, an ejected volume — can still raise, and
        # the only handler above this is the blanket one around the entire
        # startup sync. That is how a single bad junction stopped indexing for
        # all twenty-five projects while logging "non-fatal".
        project_ledger = SkipLedger()
        try:
            found_files = discover_indexable_files(
                project_path, ignore_rules=ignore_rules, mode=project.mode,
                dep_spec=proj_dep_spec, ledger=project_ledger,
            )
        except Exception as exc:  # noqa: BLE001 — isolate the project, keep the rest
            logger.warning("Project '%s' could not be scanned (%s: %s); "
                           "continuing with the remaining projects",
                           project.id, type(exc).__name__, exc)
            ledger.record(project_path, f"project scan failed: {exc}")
            continue
        project_ledger.log(f"project '{project.id}'")
        ledger.unreadable += project_ledger.unreadable
        ledger.loops += project_ledger.loops
        ledger.examples.extend(project_ledger.examples[:5 - len(ledger.examples)])

        for found in found_files:
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
