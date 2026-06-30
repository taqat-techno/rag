"""Source-class classification: project-owned vs external-dependency vs generated.

This is the axis orthogonal to **Mode** (which file *types* are indexed) and to
**secret** exclusion. Source-class answers *which instances* of those types are
the project's own code versus vendored/co-located framework/dependency source
versus build/generated output — so indexing can default to **owned-only** and
ranking can down-weight the rest.

Detection is generic and extensible — there are **no framework-specific rules in
core**. Signals:
  * conventional dependency directories (``node_modules/``, ``vendor/``, ...)
  * build / generated output (``dist/``, ``build/``, ``*.min.js``, ...)
  * git submodule boundaries (parsed from ``.gitmodules``)
  * a per-project ``dependency_paths`` list a user *or a profile* declares
    (e.g. a co-located framework core — Odoo's ``odoo/`` is one example, never
    hardcoded here).
"""

from __future__ import annotations

from pathlib import Path

import pathspec

OWNED = "owned"
DEPENDENCY = "dependency"
GENERATED = "generated"
SECRET = "secret"

SOURCE_CLASSES = (OWNED, DEPENDENCY, GENERATED, SECRET)

# Conventional external-dependency directories (any depth). Most are also in the
# built-in ignore list; kept here so the *label* is correct even if one is
# explicitly re-included for indexing.
_DEPENDENCY_DIRS = [
    "node_modules/", "bower_components/", "jspm_packages/",
    "site-packages/", "vendor/", "third_party/", "third-party/",
    "Pods/", "Carthage/", "Godeps/", ".gradle/",
]

# Build / generated output (any depth).
_GENERATED_PATTERNS = [
    "dist/", "build/", "out/", ".next/", ".nuxt/", "target/",
    "coverage/", "__generated__/", "generated/",
    "*.min.js", "*.min.css", "*.bundle.js", "*.map",
]

_DEP_DIR_SPEC = pathspec.PathSpec.from_lines("gitignore", _DEPENDENCY_DIRS)
_GEN_SPEC = pathspec.PathSpec.from_lines("gitignore", _GENERATED_PATTERNS)
_EMPTY_SPEC = pathspec.PathSpec.from_lines("gitignore", [])


def parse_gitmodules(project_root: Path | str) -> list[str]:
    """Return submodule paths (as directory globs) declared in ``.gitmodules``.

    Git submodules are external dependencies by definition. Best-effort —
    returns ``[]`` if there is no ``.gitmodules`` or it can't be read.
    """
    gm = Path(project_root) / ".gitmodules"
    if not gm.is_file():
        return []
    paths: list[str] = []
    try:
        for line in gm.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("path") and "=" in s:
                p = s.split("=", 1)[1].strip()
                if p:
                    paths.append(p.rstrip("/") + "/")
    except OSError:
        pass
    return paths


def dependency_spec(
    project_root: Path | str,
    configured_globs: "tuple[str, ...] | list[str]" = (),
) -> pathspec.PathSpec:
    """Compile the per-project dependency matcher: declared globs + submodules.

    ``configured_globs`` come from ``ProjectConfig.dependency_paths`` (or a
    profile). Submodule paths are auto-discovered from ``.gitmodules``.
    """
    globs = list(configured_globs) + parse_gitmodules(project_root)
    return pathspec.PathSpec.from_lines("gitignore", globs) if globs else _EMPTY_SPEC


def classify_source_class(
    rel_path: Path | str,
    dep_spec: "pathspec.PathSpec | None" = None,
) -> str:
    """Classify a path's source class (precedence: secret > dependency > generated > owned).

    ``rel_path`` is relative to the project root. ``dep_spec`` is the per-project
    dependency matcher from :func:`dependency_spec` (declared globs + submodules);
    pass ``None`` to skip the per-project layer (conventional/build detection
    still applies).
    """
    from ragtools.ignore import is_secret

    rel = Path(rel_path).as_posix()
    if is_secret(rel):
        return SECRET
    if dep_spec is not None and dep_spec.match_file(rel):
        return DEPENDENCY
    if _DEP_DIR_SPEC.match_file(rel):
        return DEPENDENCY
    if _GEN_SPEC.match_file(rel):
        return GENERATED
    return OWNED
