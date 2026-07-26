"""Recognise a vendored framework so it is indexed once, not once per project.

A project that vendors a framework (an Odoo core, a copied SDK) is mostly not
its own code: on this machine the `khayrgate` project is 38,136 files, and the
overwhelming majority of them are the Odoo core. Indexing that per project is
the same corpus embedded again and again — the cost the collection-per-project
model exists to avoid.

The pieces to do better already existed and were unreachable: `FrameworkRegistry`
deduplicates by build identity, `framework_collection_name` derives the shared
collection, and the router already searches a project's linked corpora. What was
missing is the step that turns "this path is a framework" into a registration —
and `ProjectConfig.dependency_paths`, the field meant to declare it, was read by
nothing.

This module is that step, and only that step: pure detection returning a
description. It writes nothing, spawns nothing, and never touches Qdrant, so it
is fully testable from a temp directory.

Identity, and why it matters
----------------------------
``build_id`` is the dedup key when known: two checkouts of the same build
resolve to ONE collection however different their paths look. Its absence is
also information — a git checkout with no recorded build is not the same thing
as a packaged release, and `framework_collection_name` keeps them apart.

Plan: docs/planning/RAG_STABILITY_HARDENING_PLAN.md (H4)
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

#: Detectors, most specific first. Each takes a root and returns a
#: :class:`FrameworkInfo` or ``None``. Registering a new framework is adding a
#: function — no edits to the dispatch below.
_DETECTORS: list[tuple[str, Callable[[Path], Optional["FrameworkInfo"]]]] = []


@dataclass(frozen=True)
class FrameworkInfo:
    """What was found at a path. Feeds ``FrameworkRegistry.register``."""

    name: str
    version: str
    edition: str
    build_id: Optional[str]
    root: str
    detector: str = ""

    def as_registration(self) -> dict:
        """Keyword arguments for :meth:`FrameworkRegistry.register`."""
        return {
            "name": self.name,
            "version": self.version,
            "edition": self.edition,
            "build_id": self.build_id,
            "canonical_root": self.root,
        }


def register_detector(name: str):
    """Decorator: add a detector. Order of registration is priority order."""
    def _wrap(fn):
        _DETECTORS.append((name, fn))
        return fn
    return _wrap


def _read(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


# --- Odoo -----------------------------------------------------------------

_ODOO_VERSION_RE = re.compile(r"version_info\s*=\s*\(([^)]*)\)")


def _odoo_build_id(root: Path) -> Optional[str]:
    """Odoo's own dedup key when present.

    ``repos_heads`` is written by Odoo's build tooling and pins the exact SHAs
    of every repo in the build — the strongest identity available, and the one
    that makes two differently-named copies of one build share a collection.
    Falling back to a git HEAD keeps checkouts distinguishable without pretending
    they are packaged builds.
    """
    for candidate in ("repos_heads", ".repos_heads", "odoo/repos_heads"):
        f = root / candidate
        if f.is_file():
            content = _read(f).strip()
            if content:
                import hashlib
                return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    head = root / ".git" / "HEAD"
    if head.is_file():
        text = _read(head).strip()
        if text and not text.startswith("ref:"):
            return text[:16]
        if text.startswith("ref:"):
            ref = (root / ".git" / text.split(" ", 1)[-1].strip())
            if ref.is_file():
                sha = _read(ref).strip()
                if sha:
                    return sha[:16]
    return None


@register_detector("odoo")
def _detect_odoo(root: Path) -> Optional[FrameworkInfo]:
    release = root / "odoo" / "release.py"
    if not release.is_file() and not (root / "odoo-bin").is_file():
        return None

    version, edition = "", "community"
    text = _read(release) if release.is_file() else ""
    m = _ODOO_VERSION_RE.search(text)
    if m:
        parts = [p.strip().strip("'\"") for p in m.group(1).split(",") if p.strip()]
        nums = [p for p in parts[:2] if p.lstrip("-").isdigit()]
        version = ".".join(nums) if nums else ""
    if re.search(r"version_info.*\+e|'e'|\"e\"", text) or (root / "odoo" / "enterprise").exists():
        edition = "enterprise"

    return FrameworkInfo(
        name="odoo", version=version, edition=edition,
        build_id=_odoo_build_id(root), root=str(root), detector="odoo",
    )


# --- Node / npm -----------------------------------------------------------


@register_detector("npm")
def _detect_npm(root: Path) -> Optional[FrameworkInfo]:
    pkg = root / "package.json"
    if not pkg.is_file():
        return None
    try:
        data = json.loads(_read(pkg) or "{}")
    except ValueError:
        return None
    name = str(data.get("name") or root.name)
    return FrameworkInfo(
        name=name, version=str(data.get("version") or ""), edition="npm",
        build_id=None, root=str(root), detector="npm",
    )


# --- Python distribution --------------------------------------------------


@register_detector("python")
def _detect_python(root: Path) -> Optional[FrameworkInfo]:
    for meta in ("pyproject.toml", "setup.cfg"):
        f = root / meta
        if not f.is_file():
            continue
        text = _read(f)
        name = re.search(r'^\s*name\s*=\s*["\']?([\w.\-]+)', text, re.M)
        version = re.search(r'^\s*version\s*=\s*["\']?([\w.\-]+)', text, re.M)
        return FrameworkInfo(
            name=(name.group(1) if name else root.name),
            version=(version.group(1) if version else ""),
            edition="python", build_id=None, root=str(root), detector="python",
        )
    return None


# --- Dispatch -------------------------------------------------------------


def detect_framework(root) -> Optional[FrameworkInfo]:
    """Identify the framework at ``root``, or ``None`` if it is not one.

    ``None`` is a real answer, not a failure: a dependency path that is just a
    folder of shared files still gets indexed as a corpus, it simply carries no
    version identity. The caller decides (see :func:`describe_dependency`).
    """
    path = Path(root)
    if not path.is_dir():
        return None
    for _name, detector in _DETECTORS:
        try:
            found = detector(path)
        except Exception:  # noqa: BLE001 — a broken detector must not block others
            continue
        if found is not None:
            return found
    return None


def describe_dependency(root) -> Optional[FrameworkInfo]:
    """Always describe a real directory — detected, or a generic corpus.

    Used for declared ``dependency_paths``: the owner has said "this is a shared
    dependency", so it is one whether or not a detector recognises the flavour.

    **The generic fallback is keyed by resolved path, not by directory name.**
    Without a build id, identity would collapse to ``(name, "", "generic")``, so
    every project that declared a folder called ``odoo`` — a very common layout,
    where the core sits at ``<project>/odoo`` and the checkout root is the
    project itself — would land in ONE collection. Two different vendored cores
    would then be merged, and project A's search would return project B's code.
    Duplicate storage is the safe failure here; cross-project bleed is not.
    """
    path = Path(root)
    if not path.is_dir():
        return None
    detected = detect_framework(path)
    if detected is not None:
        return detected
    resolved = str(path.resolve())
    return FrameworkInfo(
        name=path.name or "dependency", version="", edition="generic",
        build_id=hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16],
        root=resolved, detector="generic",
    )


def detector_names() -> list[str]:
    """Registered detectors, in priority order."""
    return [name for name, _ in _DETECTORS]


# --- Declared dependency roots -------------------------------------------


@dataclass(frozen=True)
class DependencyRoot:
    """A validated, resolved dependency root of one project."""

    declared: str          #: exactly what the config said
    path: Path             #: fully resolved (symlinks/junctions followed)
    inside_project: bool   #: does it live under the project root?
    relative: str          #: posix path relative to the project root, or ""

    @property
    def exclusion_globs(self) -> list[str]:
        """Patterns that keep this root out of the project's own scan.

        Empty when the root is outside the project: the project scan is rooted
        at the project directory and never reaches it, so there is nothing to
        exclude and a bogus relative glob would only risk a false match.
        """
        if not self.inside_project or not self.relative:
            return []
        return [f"{self.relative}/", f"{self.relative}/**"]


def resolve_dependency_roots(project_path, declared) -> tuple[list[DependencyRoot], list[str]]:
    """Validate declared dependency paths. Returns ``(roots, problems)``.

    Every rule here exists because getting it wrong is silent and expensive:

    * **Resolved, not literal.** Comparisons use ``Path.resolve()``, so a
      symlink, a Windows junction, ``..`` segments and drive-letter case all
      collapse to one identity. Two spellings of the same directory must not
      become two framework collections.
    * **The project root itself is refused.** ``dependency_paths = ["."]``
      would exclude *every* file from the project scan and move the whole
      project into a "framework" corpus — total data loss dressed up as a
      config choice.
    * **An ancestor of the project is refused** for the same reason.
    * **Nested declarations collapse to the outermost.** Declaring both
      ``vendor`` and ``vendor/odoo`` must not index the inner tree twice.
    * **Missing paths are reported, not fatal.** A typo should be visible
      without failing the whole index.

    ``problems`` are human-readable; the caller decides whether to log, surface
    on /health, or refuse.
    """
    root = Path(project_path).resolve()
    roots: list[DependencyRoot] = []
    problems: list[str] = []
    seen: set[Path] = set()

    for raw in (declared or []):
        text = str(raw).strip()
        if not text:
            continue
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:                       # noqa: PERF203
            problems.append(f"{text!r}: cannot be resolved ({exc})")
            continue

        if resolved == root:
            problems.append(
                f"{text!r}: is the project root itself — that would exclude every "
                "file from the project and index nothing as project-owned code"
            )
            continue
        if root.is_relative_to(resolved):
            problems.append(
                f"{text!r}: contains the project root — a dependency cannot be a "
                "parent of the project it belongs to"
            )
            continue
        if not resolved.is_dir():
            problems.append(f"{text!r}: does not exist (resolved to {resolved})")
            continue
        if resolved in seen:
            continue

        seen.add(resolved)
        inside = resolved.is_relative_to(root)
        relative = resolved.relative_to(root).as_posix() if inside else ""
        roots.append(DependencyRoot(declared=text, path=resolved,
                                    inside_project=inside, relative=relative))

    # Collapse nested declarations to the outermost: indexing `vendor` and
    # `vendor/odoo` separately would embed the inner tree twice.
    outermost: list[DependencyRoot] = []
    for candidate in sorted(roots, key=lambda r: len(str(r.path))):
        if any(candidate.path.is_relative_to(kept.path) for kept in outermost):
            problems.append(
                f"{candidate.declared!r}: already covered by "
                f"{[k.declared for k in outermost if candidate.path.is_relative_to(k.path)][0]!r}"
            )
            continue
        outermost.append(candidate)
    return outermost, problems


def exclusion_globs_for(project_path, declared) -> list[str]:
    """Scanner globs that keep declared dependency roots out of a project scan."""
    roots, _problems = resolve_dependency_roots(project_path, declared)
    globs: list[str] = []
    for root in roots:
        globs.extend(root.exclusion_globs)
    return globs
