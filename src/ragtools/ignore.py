"""Ignore rules engine — 3-layer file exclusion for indexing and watching.

Layers (all additive — if ANY layer matches, file is ignored):
  1. Built-in defaults (hardcoded, not user-mutable)
  2. Global config patterns (from config.toml [ignore].patterns)
  3. Per-directory .ragignore files (gitignore syntax, ! negation supported)

Uses pathspec library for gitignore-style matching.
"""

from pathlib import Path

import pathspec

# Built-in defaults: superset of old SKIP_DIRS + common extras.
# These are always active and cannot be disabled.
BUILTIN_PATTERNS = [
    # Directories (original SKIP_DIRS)
    ".git/",
    ".hg/",
    ".svn/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "node_modules/",
    "site-packages/",
    ".tox/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".hypothesis/",
    "dist/",
    "build/",
    "*.egg-info/",
    ".stversions/",
    # Additional defaults
    ".cache/",
    # File patterns
    "*.pyc",
    "*.pyo",
]

RAGIGNORE_FILENAME = ".ragignore"


class IgnoreRules:
    """Three-layer ignore rule engine.

    Args:
        content_root: Root directory for resolving .ragignore files.
        global_patterns: Extra patterns from config (on top of built-ins).
        use_ragignore: Whether to parse .ragignore files on disk.
    """

    def __init__(
        self,
        content_root: Path | str = ".",
        global_patterns: list[str] | None = None,
        use_ragignore: bool = True,
    ):
        self.content_root = Path(content_root).resolve()
        self.use_ragignore = use_ragignore

        # Layer 3 (lowest priority): built-in defaults
        self._builtin_spec = pathspec.PathSpec.from_lines(
            "gitignore", BUILTIN_PATTERNS
        )

        # Layer 2: global config patterns
        self._global_patterns = global_patterns or []
        self._global_spec = pathspec.PathSpec.from_lines(
            "gitignore", self._global_patterns
        ) if self._global_patterns else None

        # Layer 1 (highest priority): per-directory .ragignore
        # Cache: directory Path -> compiled PathSpec (or None)
        self._ragignore_cache: dict[Path, pathspec.PathSpec | None] = {}

    def is_ignored(self, file_path: Path | str, content_root: Path | str | None = None) -> bool:
        """Check if a file should be excluded from indexing.

        Args:
            file_path: Absolute or relative path to check.
            content_root: Override content root (uses self.content_root if None).

        Returns:
            True if the file should be ignored.
        """
        return self.get_reason(file_path, content_root) is not None

    def get_reason(self, file_path: Path | str, content_root: Path | str | None = None) -> str | None:
        """Return why a file is ignored, or None if it's not ignored.

        Returns a human-readable string like:
            "built-in: .git/"
            "config: drafts/"
            ".ragignore: /path/to/.ragignore: *.tmp"
        """
        root = Path(content_root).resolve() if content_root else self.content_root
        path = Path(file_path)

        # Make path relative to root for matching
        if path.is_absolute():
            try:
                rel = path.relative_to(root)
            except ValueError:
                # Path is outside content root — check using just the path parts
                rel = path
        else:
            rel = path

        rel_posix = rel.as_posix()

        # Check built-in defaults
        if self._builtin_spec.match_file(rel_posix):
            # Find which built-in pattern matched
            for pattern in BUILTIN_PATTERNS:
                spec = pathspec.PathSpec.from_lines("gitignore", [pattern])
                if spec.match_file(rel_posix):
                    return f"built-in: {pattern}"
            return "built-in"

        # Check global config patterns
        if self._global_spec and self._global_spec.match_file(rel_posix):
            for pattern in self._global_patterns:
                spec = pathspec.PathSpec.from_lines("gitignore", [pattern])
                if spec.match_file(rel_posix):
                    return f"config: {pattern}"
            return "config"

        # Check .ragignore files
        if self.use_ragignore:
            result = self._check_ragignore(path, root, rel_posix)
            if result:
                return result

        return None

    def _check_ragignore(self, file_path: Path, root: Path, rel_posix: str) -> str | None:
        """Check .ragignore files from the file's directory up to content root."""
        if file_path.is_absolute():
            check_dir = file_path.parent
        else:
            check_dir = (root / file_path).parent

        check_dir = check_dir.resolve()
        root_resolved = root.resolve()

        # Walk from file's directory up to content root
        while True:
            spec = self._load_ragignore(check_dir)
            if spec is not None:
                # Make path relative to the .ragignore's directory for matching
                try:
                    local_rel = file_path.resolve().relative_to(check_dir) if file_path.is_absolute() else (root / file_path).resolve().relative_to(check_dir)
                    if spec.match_file(local_rel.as_posix()):
                        ragignore_path = check_dir / RAGIGNORE_FILENAME
                        return f".ragignore: {ragignore_path}"
                except ValueError:
                    pass

            # Stop at content root
            if check_dir == root_resolved or check_dir.parent == check_dir:
                break
            check_dir = check_dir.parent

        return None

    def _load_ragignore(self, directory: Path) -> pathspec.PathSpec | None:
        """Load and cache a .ragignore file from a directory."""
        if directory in self._ragignore_cache:
            return self._ragignore_cache[directory]

        ragignore_path = directory / RAGIGNORE_FILENAME
        if ragignore_path.is_file():
            try:
                lines = ragignore_path.read_text(encoding="utf-8", errors="replace").splitlines()
                spec = pathspec.PathSpec.from_lines("gitignore", lines)
                self._ragignore_cache[directory] = spec
                return spec
            except Exception:
                self._ragignore_cache[directory] = None
                return None
        else:
            self._ragignore_cache[directory] = None
            return None

    def clear_cache(self) -> None:
        """Clear the .ragignore file cache. Call when .ragignore files change."""
        self._ragignore_cache.clear()

    def get_all_patterns(self) -> dict[str, list[str]]:
        """Return all active patterns grouped by layer. Useful for `rag ignore list`."""
        result: dict[str, list[str]] = {
            "built-in": list(BUILTIN_PATTERNS),
            "config": list(self._global_patterns),
        }

        if self.use_ragignore:
            ragignore_files: dict[str, list[str]] = {}
            # Scan content_root for .ragignore files
            for ragignore in self.content_root.rglob(RAGIGNORE_FILENAME):
                try:
                    lines = [
                        line.strip() for line in
                        ragignore.read_text(encoding="utf-8", errors="replace").splitlines()
                        if line.strip() and not line.strip().startswith("#")
                    ]
                    if lines:
                        ragignore_files[str(ragignore)] = lines
                except Exception:
                    pass
            result["ragignore_files"] = ragignore_files  # type: ignore[assignment]

        return result
