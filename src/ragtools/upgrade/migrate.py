"""Config migration and PATH repair — the parts of an upgrade that touch state.

Two jobs, deliberately separate from :mod:`ragtools.upgrade.scan`:

**Config v2 → v3.** The installed configuration on this machine is a pure v2
file: fifteen projects, each with a ``mode`` and a ``dependency_paths`` list, and
*no* ``storage_backend``, ``collection_strategy`` or ``[[dependencies]]``. The
migration adds what v3 needs and folds legacy dependency paths into the catalog —
reusing the validator that already does exactly that, rather than writing a
second implementation of the same rule.

**PATH repair.** The installed directory appears sixteen times on this machine's
PATH because ``NeedsAddPath`` returned true on every upgrade. Deduplication has
to be resolved-and-case-folded: two of those entries differ only in case and name
the same directory, and which one wins decides which ``rag`` runs.

Every function here is pure or explicitly transactional. Nothing writes a config
without first producing the exact bytes it would write, so a dry run and a real
run cannot disagree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

#: The config version this release reads and writes.
CONFIG_VERSION = 3

#: Keys a v3 config carries that a v2 config does not. Absent means "the v2
#: default", which is why each has an explicit value here rather than being
#: left for a code default to guess later.
V3_DEFAULTS = {
    # Embedded is the honest default: it needs nothing, and the scale warning
    # tells the truth when the index outgrows it. Choosing `managed` for the
    # user would download a binary they did not ask for.
    "storage_backend": "embedded",
    "collection_strategy": "per_project",
}


@dataclass
class MigrationResult:
    """What a migration did, or would do. Same object for dry and real runs."""

    changed: bool = False
    from_version: int = 0
    to_version: int = CONFIG_VERSION
    added_keys: list[str] = field(default_factory=list)
    adopted_dependencies: list[str] = field(default_factory=list)
    project_count: int = 0
    notes: list[str] = field(default_factory=list)
    #: The exact document that would be written.
    document: dict = field(default_factory=dict)


def migrate_config(document: dict) -> MigrationResult:
    """Return the v3 form of a parsed config document. Never writes.

    Idempotent: migrating an already-migrated document changes nothing, so an
    interrupted upgrade can re-run the whole step safely.
    """
    from ragtools.config import ProjectConfig, Settings

    result = MigrationResult()
    doc = {k: v for k, v in document.items()}
    result.from_version = int(doc.get("version", 1) or 1)
    result.project_count = len(doc.get("projects", []) or [])

    for key, value in V3_DEFAULTS.items():
        if key not in doc:
            doc[key] = value
            result.added_keys.append(key)

    # Fold legacy `dependency_paths` into the catalog using the SAME validator
    # the runtime uses, so a migrated file and a loaded file agree by
    # construction rather than by two implementations happening to match.
    projects = [ProjectConfig(**p) for p in (doc.get("projects") or [])]
    if projects:
        settings = Settings(projects=projects,
                            dependencies=[_dep(d) for d in (doc.get("dependencies") or [])])
        if settings.dependencies:
            doc["dependencies"] = [d.model_dump(exclude_none=True) for d in settings.dependencies]
            doc["projects"] = [p.model_dump(exclude_none=True) for p in settings.projects]
            result.adopted_dependencies = [d.id for d in settings.dependencies]
            if result.from_version < CONFIG_VERSION:
                result.notes.append(
                    f"{len(settings.dependencies)} shared dependency entr"
                    f"{'y' if len(settings.dependencies) == 1 else 'ies'} adopted from "
                    "per-project dependency_paths"
                )

    doc["version"] = CONFIG_VERSION
    result.document = doc
    result.changed = (
        result.from_version != CONFIG_VERSION
        or bool(result.added_keys)
        or bool(result.adopted_dependencies)
    )
    return result


def _dep(raw: dict):
    from ragtools.config import DependencyConfig

    return DependencyConfig(**raw)


# --- PATH -----------------------------------------------------------------


@dataclass
class PathRepair:
    """A proposed PATH rewrite. ``entries`` is the full replacement value."""

    original: list[str] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.removed)

    def render(self, separator: Optional[str] = None) -> str:
        return (separator or os.pathsep).join(self.entries)


def _key(entry: str) -> str:
    try:
        resolved = str(Path(entry).expanduser().resolve())
    except OSError:
        resolved = entry
    return os.path.normcase(resolved.rstrip("\\/"))


def repair_path(path_value: str, *, keep: Optional[str] = None) -> PathRepair:
    """Remove duplicate product entries, preserving order and everything else.

    Rules, each one earned:

    * identity is the **resolved, case-folded** directory — two entries on this
      machine differ only in case and name one directory;
    * the **first** occurrence is kept, so ordering semantics are unchanged for
      every other tool on the system;
    * non-product entries are never reordered or removed — a PATH cleanup that
      touches unrelated entries is a much worse bug than the one being fixed;
    * ``keep`` lets the installer pin the canonical spelling it just installed
      to, rather than whichever casing happened to appear first.
    """
    repair = PathRepair(original=[e for e in path_value.split(os.pathsep)])
    keep_key = _key(keep) if keep else None
    seen: set[str] = set()
    out: list[str] = []

    for entry in repair.original:
        stripped = entry.strip()
        if not stripped:
            # Preserve empty segments: on Windows a trailing separator is
            # common and removing it is a visible, pointless change.
            out.append(entry)
            continue
        key = _key(stripped)
        is_product = "ragtools" in os.path.normcase(stripped)
        if not is_product:
            out.append(entry)
            continue
        if key in seen:
            repair.removed.append(entry)
            continue
        seen.add(key)
        # Normalise to the caller's preferred spelling when it names this
        # directory, so `where rag` and the installer agree.
        out.append(keep if (keep_key is not None and key == keep_key) else entry)

    repair.entries = out
    return repair
