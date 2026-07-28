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
#:
#: Re-exported, not redefined. It is a fact about the schema, so it belongs to
#: the module that owns the schema; a second literal here is precisely how the
#: writers came to disagree with the migrator in the first place.
from ragtools.config import CONFIG_VERSION  # noqa: E402  (re-export)

#: Keys a v3 config carries that a v2 config does not. Absent means "the v2
#: default", which is why each has an explicit value here rather than being
#: left for a code default to guess later.
V3_DEFAULTS = {
    # Embedded is the honest default: it needs nothing, and the scale warning
    # tells the truth when the index outgrows it. Choosing `managed` for the
    # user would download a binary they did not ask for — and no release ships
    # one, so `managed` cannot be a default in any case.
    "storage_backend": "embedded",
}

#: The collection layout a migrated config adopts.
#:
#: `shared`, deliberately, even though `per_project` is the v3 model. Changing
#: the layout changes *where the vectors live*, so `index_identity` correctly
#: refuses to trust the state DB afterwards and forces a full re-index — tens of
#: thousands of files, at first boot, on a machine the user has just upgraded
#: and is waiting on. An upgrade that silently starts hours of work is not an
#: upgrade the user consented to.
#:
#: It also would not reliably fix the thing it appears to fix. Splitting one
#: collection into twenty-five only helps if no SINGLE project exceeds the
#: engine's limit, and a project that vendors a framework can exceed it alone —
#: so the forced re-index can cost hours and leave the warning exactly where it
#: was.
#:
#: The layout is therefore reachable, recommended when it would genuinely help,
#: and never imposed. See `rag storage strategy`.
LAYOUT_FOR_EXISTING_INSTALL = "shared"

#: A configuration with no projects has nothing indexed, so adopting the v3
#: layout costs nothing and re-indexes nothing.
LAYOUT_FOR_NEW_INSTALL = "per_project"


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

    # The layout depends on whether there is an index to disturb. An explicit
    # value either way, so the file states what it does rather than relying on
    # a code default that a later release could change underneath it.
    if "collection_strategy" not in doc:
        existing_install = bool(doc.get("projects"))
        doc["collection_strategy"] = (LAYOUT_FOR_EXISTING_INSTALL if existing_install
                                      else LAYOUT_FOR_NEW_INSTALL)
        result.added_keys.append("collection_strategy")
        if existing_install:
            result.notes.append(
                "collection layout left as 'shared' — switching to per-project "
                "collections requires a full re-index, so it is offered rather "
                "than imposed (see `rag storage strategy`)"
            )

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


def fold_for_match(text: str) -> str:
    """Case-fold and normalise separators, for NAME MATCHING, on every platform.

    ``os.path.normcase`` looks like the right tool and is not: it lowercases on
    Windows and is the **identity function** on POSIX. Every "is this one of
    ours?" check built on it therefore worked on Windows and silently matched
    nothing on Linux or macOS — where the product's own directories are
    ``RAGTools`` and ``RAGTools-dev``, capitalised.

    The consequences were not cosmetic. PATH cleanup recognised no product
    entries at all off Windows, and ``scan.is_development_path`` failed to
    recognise a developer's isolated store — so the guard whose entire purpose
    is "never delete a dev environment" was inert on two of three platforms.

    Kept separate from :func:`directory_identity` deliberately. Identity ("are
    these the same directory?") is a filesystem question. Matching ("does this
    name look like ours?") is not, and over-matching is the safe direction for a
    protective check.
    """
    return str(text).replace("\\", "/").casefold()


def directory_identity(entry: str) -> str:
    """Identity of a directory, for de-duplication.

    Asks the **filesystem**, not the platform. Whether two spellings name one
    directory is a property of the filesystem, and no ``sys.platform`` test gets
    it right: NTFS and the default APFS are case-insensitive, ext4 is
    case-sensitive, and a case-sensitive APFS volume on the same Mac is too.

    ``os.path.normcase`` was the previous answer and is wrong twice over — it is
    the identity on *both* Linux and macOS, so on macOS two casings of one
    directory each kept their own PATH entry and the duplicate-entry bug this
    function exists to fix went unfixed.

    ``(st_dev, st_ino)`` is the filesystem's own answer and is exact on all
    three platforms, including symlinks and junctions pointing at one target.
    Paths that do not exist have no inode, so those fall back to a textual key —
    which is right for them: nothing can prove two absent paths are one
    directory.
    """
    expanded = Path(entry).expanduser()
    try:
        stat = expanded.stat()
        if stat.st_ino:                       # 0 on filesystems with no inodes
            return f"inode:{stat.st_dev}:{stat.st_ino}"
    except (OSError, ValueError):
        pass

    try:
        resolved = str(expanded.resolve())
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
    keep_key = directory_identity(keep) if keep else None
    seen: set[str] = set()
    out: list[str] = []

    for entry in repair.original:
        stripped = entry.strip()
        if not stripped:
            # Preserve empty segments: on Windows a trailing separator is
            # common and removing it is a visible, pointless change.
            out.append(entry)
            continue
        key = directory_identity(stripped)
        is_product = "ragtools" in fold_for_match(stripped)
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
