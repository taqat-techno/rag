"""File classification registry — maps files to language + chunk_type.

Single source of truth for "what files can be indexed" and how each one
should be chunked. Used by:
  - the scanner (which files to discover)
  - the chunk_file dispatcher (which chunker to route to)
  - the watcher (which file changes to react to)

Chunk types (the ``chunk_type`` metadata field):
  - ``code``           — source code (functions, classes, statements)
  - ``config``         — structured config/data (json, yaml, toml, xml, ...)
  - ``documentation``  — prose (markdown, README, plain text)
  - ``comment``        — comment-only chunks extracted from source files

The classification is intentionally extension/filename driven (no content
sniffing) so it is cheap and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# chunk_type constants
CODE = "code"
CONFIG = "config"
DOCUMENTATION = "documentation"
COMMENT = "comment"


@dataclass(frozen=True)
class FileClass:
    """The classification of a single file."""

    language: str       # e.g. "python", "typescript", "yaml", "markdown"
    chunk_type: str     # one of CODE / CONFIG / DOCUMENTATION
    extension: str      # normalized extension incl. dot, or "" for extensionless


# Extension -> (language, chunk_type).
# Extensions are lower-cased and include the leading dot.
_EXTENSION_MAP: dict[str, tuple[str, str]] = {
    # --- Source code ---
    ".py": ("python", CODE),
    ".js": ("javascript", CODE),
    ".jsx": ("javascript", CODE),
    ".mjs": ("javascript", CODE),
    ".cjs": ("javascript", CODE),
    ".ts": ("typescript", CODE),
    ".tsx": ("typescript", CODE),
    ".java": ("java", CODE),
    ".go": ("go", CODE),
    ".cs": ("csharp", CODE),
    ".php": ("php", CODE),
    ".html": ("html", CODE),
    ".htm": ("html", CODE),
    ".css": ("css", CODE),
    ".scss": ("scss", CODE),
    ".sql": ("sql", CODE),
    ".sh": ("shell", CODE),
    ".bash": ("shell", CODE),
    ".rs": ("rust", CODE),
    ".kt": ("kotlin", CODE),
    ".kts": ("kotlin", CODE),
    ".scala": ("scala", CODE),
    ".swift": ("swift", CODE),
    ".c": ("c", CODE),
    ".h": ("c", CODE),
    ".cpp": ("cpp", CODE),
    ".cc": ("cpp", CODE),
    ".cxx": ("cpp", CODE),
    ".hpp": ("cpp", CODE),
    ".prisma": ("prisma", CODE),  # Prisma schema (model/enum declarations)
    # --- Structured config / data ---
    ".json": ("json", CONFIG),
    ".yaml": ("yaml", CONFIG),
    ".yml": ("yaml", CONFIG),
    ".xml": ("xml", CONFIG),
    ".toml": ("toml", CONFIG),
    ".ini": ("ini", CONFIG),
    ".cfg": ("ini", CONFIG),
    ".dockerfile": ("dockerfile", CONFIG),
    # --- Documentation ---
    ".md": ("markdown", DOCUMENTATION),
    ".markdown": ("markdown", DOCUMENTATION),
    ".rst": ("restructuredtext", DOCUMENTATION),
    ".txt": ("text", DOCUMENTATION),
}

# Exact filenames (no extension, or names that override extension rules).
# Keyed by the lower-cased file name.
_FILENAME_MAP: dict[str, tuple[str, str]] = {
    "dockerfile": ("dockerfile", CONFIG),
    "requirements.txt": ("requirements", CONFIG),
    "requirements-dev.txt": ("requirements", CONFIG),
    "pyproject.toml": ("toml", CONFIG),
    "package.json": ("json", CONFIG),
    "makefile": ("makefile", CODE),
    "readme": ("markdown", DOCUMENTATION),
    "readme.md": ("markdown", DOCUMENTATION),
    "readme.txt": ("text", DOCUMENTATION),
    "readme.rst": ("restructuredtext", DOCUMENTATION),
}

# Languages handled by the structural code chunker.
# (Everything else classified as CODE falls back to a generic line chunker.)
STRUCTURED_CODE_LANGUAGES = frozenset(
    {
        "python",
        "javascript",
        "typescript",
        "java",
        "go",
        "csharp",
        "php",
        "css",
        "scss",
    }
)


def classify_file(path: Path | str) -> FileClass | None:
    """Classify a file by its name/extension.

    Returns a :class:`FileClass`, or ``None`` if the file type is not
    supported for indexing.

    Resolution order:
      1. Exact filename match (``Dockerfile``, ``requirements.txt``, ...)
      2. ``*.dockerfile`` compound suffix
      3. Extension match
    """
    p = Path(path)
    name_lower = p.name.lower()

    # 1. Exact filename (and README without extension).
    if name_lower in _FILENAME_MAP:
        language, chunk_type = _FILENAME_MAP[name_lower]
        return FileClass(language=language, chunk_type=chunk_type, extension=p.suffix.lower())

    # README.<anything> is documentation regardless of extension.
    if name_lower.startswith("readme"):
        return FileClass(language="markdown", chunk_type=DOCUMENTATION, extension=p.suffix.lower())

    # 2. Compound suffix like ``service.dockerfile``.
    if name_lower.endswith(".dockerfile"):
        return FileClass(language="dockerfile", chunk_type=CONFIG, extension=".dockerfile")

    # 3. Plain extension.
    ext = p.suffix.lower()
    if ext in _EXTENSION_MAP:
        language, chunk_type = _EXTENSION_MAP[ext]
        return FileClass(language=language, chunk_type=chunk_type, extension=ext)

    return None


def is_supported(path: Path | str) -> bool:
    """True if the file can be indexed."""
    return classify_file(path) is not None


def is_documentation(path: Path | str) -> bool:
    """True if the file is documentation (markdown/README/text)."""
    fc = classify_file(path)
    return fc is not None and fc.chunk_type == DOCUMENTATION


def supported_extensions() -> set[str]:
    """Return the set of supported file extensions (incl. leading dot)."""
    return set(_EXTENSION_MAP.keys())


def supported_filenames() -> set[str]:
    """Return the set of supported exact filenames (lower-cased)."""
    return set(_FILENAME_MAP.keys())
