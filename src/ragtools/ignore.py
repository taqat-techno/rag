"""Ignore rules engine — 3-layer file exclusion for indexing and watching.

Layers (all additive — if ANY layer matches, file is ignored):
  1. Built-in defaults (hardcoded, not user-mutable)
  2. Global config patterns (from config.toml [ignore].patterns)
  3. Per-directory .ragignore files (gitignore syntax, ! negation supported)

Uses pathspec library for gitignore-style matching.
"""

import re
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
    ".claude/",
    # File patterns
    "*.pyc",
    "*.pyo",
    # Generated / minified / vendored code artifacts — supported extensions but
    # noise for retrieval. Excluded by default; override with a negated
    # .ragignore rule if a project genuinely needs them indexed.
    "*.min.js",
    "*.min.css",
    "*.bundle.js",
    "*.map",
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
    "pnpm-lock.yaml",
    "*.lock",
    "vendor/",
    ".next/",
    ".nuxt/",
    "target/",
    "bin/",
    "obj/",
    # Files already read by Claude directly (no need to index)
    "CLAUDE.md",
]

RAGIGNORE_FILENAME = ".ragignore"


# --- Secret / credential exclusion (P0 security) -----------------------------
# Secret-bearing files are NEVER indexed by default — their contents (API keys,
# private keys, tokens, credentials) must not be embedded or stored in Qdrant.
# This is a dedicated layer, separate from BUILTIN_PATTERNS, enforced at
# discovery (scanner), storage (indexer), and the watcher. An explicit allowlist
# (gitignore-style globs, from config) can re-include a path a project needs.
#
# Balanced policy:
#   * SECRET_PATTERNS below match for ALL file types (specific secret artifacts).
#   * A broad ``*secret*`` / ``*credential*`` name match applies ONLY to
#     non-source files, so legitimate source modules (e.g. ``secret_manager.py``)
#     remain indexable.
SECRET_PATTERNS = [
    # dotenv
    ".env", ".env.*", "*.env",
    # private keys / certs / keystores
    "*.pem", "*.key", "*.p12", "*.pfx", "*.pkcs12", "*.keystore", "*.jks", "*.ppk",
    "id_rsa", "id_rsa.*", "id_dsa", "id_ecdsa", "id_ed25519",
    # credential stores / directories
    ".aws/", ".ssh/", ".gnupg/",
    ".netrc", ".pgpass", "*.pgpass", ".npmrc", ".pypirc",
    # infra state / vars (frequently contain secrets)
    "*.tfvars", "*.tfstate", "*.tfstate.*",
    # explicit secret / credential files
    "*.secret", "*.secrets",
    "secrets.json", "secrets.yaml", "secrets.yml",
    "credential.json", "credentials", "credentials.json", "credentials.yaml", "credentials.yml",
    # secret/credential directory (symmetry with `credentials`: a bare name
    # matches a directory OR a file of that name at any depth).
    "secrets",
]

_SECRET_SPEC = pathspec.PathSpec.from_lines("gitignore", SECRET_PATTERNS)
_SECRET_BROAD_RE = re.compile(r"(secret|credential)", re.IGNORECASE)

# For the broad *secret*/*credential* NAME match: exempt true prose docs and
# logic source modules (e.g. secret_manager.py) — named, but not secret stores.
# Everything else named secret/credential (config/data, and data-bearing
# scripts/SQL such as secrets.sh or db_credentials.sql) is treated as a secret.
_PROSE_LANGUAGES = frozenset({"markdown", "restructuredtext"})
_LOGIC_CODE_LANGUAGES = frozenset({
    "python", "javascript", "typescript", "java", "go", "csharp", "php",
    "rust", "kotlin", "scala", "swift", "c", "cpp",
})


def is_secret(file_path: Path | str, allowlist: "list[str] | tuple[str, ...]" = ()) -> bool:
    """Return True if a file is secret-bearing and must never be indexed.

    Specific secret artifacts (dotenv, keys, credential stores) are denied for
    all file types, case-insensitively. Broad ``*secret*`` / ``*credential*``
    name matches are denied for config/data files and data-bearing scripts
    (e.g. ``secrets.sh``, ``db_credentials.sql``); prose docs (md/rst) and logic
    source modules (e.g. ``secret_manager.py``) stay indexable. An explicit
    ``allowlist`` of gitignore-style globs re-includes specific paths.
    """
    p = Path(file_path)
    rel_l = p.as_posix().lower()
    name_l = p.name.lower()

    # Case-insensitive throughout — the target filesystem (Windows) is too.
    if allowlist:
        allow_spec = pathspec.PathSpec.from_lines("gitignore", [a.lower() for a in allowlist])
        if allow_spec.match_file(rel_l) or allow_spec.match_file(name_l):
            return False

    if _SECRET_SPEC.match_file(rel_l) or _SECRET_SPEC.match_file(name_l):
        return True

    if _SECRET_BROAD_RE.search(name_l):
        # Prose docs and logic source modules named secret/credential are not
        # secret stores; everything else (config/data, data-bearing scripts) is.
        from ragtools.chunking.languages import classify_file

        fc = classify_file(p)
        if fc is None:
            return True
        if fc.language not in _PROSE_LANGUAGES and fc.language not in _LOGIC_CODE_LANGUAGES:
            return True

    return False


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
        secret_allowlist: list[str] | None = None,
    ):
        self.content_root = Path(content_root).resolve()
        self.use_ragignore = use_ragignore
        self._secret_allowlist = secret_allowlist or []

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

    def is_secret(self, file_path: Path | str) -> bool:
        """True if the file is secret-bearing and must never be indexed.

        Honors this instance's secret allowlist (from config).
        """
        return is_secret(file_path, self._secret_allowlist)

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
