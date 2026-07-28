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

#: Keys a v3 config carries that a v2 config does not.
#:
#: Empty, and deliberately so: both storage keys are now decided by the
#: implicit-vs-explicit rule below rather than by a fixed default table, because
#: the right value depends on what the existing file already says and on what
#: this installation can actually run. Kept as a named, extensible seam for the
#: next key that genuinely does have one right answer.
V3_DEFAULTS: dict = {}

#: The v3 architecture a legacy configuration is migrated TO.
#:
#: **A missing key is not a decision.** A v2 config has no `storage_backend` and
#: no `collection_strategy` because those keys did not exist when it was written,
#: not because anyone chose the values they now imply. Treating that absence as
#: "the user wants shared+embedded" preserves a legacy architecture nobody asked
#: for — and leaves the scale problem that prompted the upgrade exactly where it
#: was, discoverable only by finding and running `rag storage strategy` by hand.
#:
#: So an implicit legacy default is a migration INPUT: it is migrated to the
#: recommended architecture. An explicit value — including an explicit
#: `embedded` or `shared` — is a decision, and decisions are preserved.
RECOMMENDED_BACKEND = "managed"
RECOMMENDED_LAYOUT = "per_project"

#: What a legacy default becomes when the recommended engine cannot run here.
#:
#: `managed` supervises a native binary. Every shipped artifact now packages one
#: (`scripts/fetch_qdrant.py`), but a source install, an unsupported
#: architecture or a stripped build may not have it — and a "recommended"
#: architecture that silently degrades is not a recommendation. Where the engine
#: is absent the layout still moves, because per-project collections need no
#: binary at all, and the reason is recorded rather than inferred.
FALLBACK_BACKEND = "embedded"

#: A configuration with no projects has nothing indexed, so adopting the v3
#: layout costs nothing and re-indexes nothing.
LAYOUT_FOR_NEW_INSTALL = RECOMMENDED_LAYOUT


@dataclass
class MigrationResult:
    """What a migration did, or would do. Same object for dry and real runs."""

    changed: bool = False
    from_version: int = 0
    to_version: int = CONFIG_VERSION
    added_keys: list[str] = field(default_factory=list)
    adopted_dependencies: list[str] = field(default_factory=list)
    #: Storage keys that were ABSENT and have been set to the recommended v3
    #: value. Non-empty means the machine changed architecture and owes a
    #: re-index; empty means every storage value was already explicit.
    adopted_defaults: list[str] = field(default_factory=list)
    project_count: int = 0
    notes: list[str] = field(default_factory=list)
    #: The exact document that would be written.
    document: dict = field(default_factory=dict)


def _recommended_backend() -> tuple[str, str]:
    """The engine a legacy default adopts here, and why if it is not the ideal.

    `managed` is the recommendation, and every shipped artifact now packages the
    pinned engine. But a source checkout, an unsupported architecture or a build
    made with `--no-engine` has no binary, and a recommended architecture that
    silently degrades is not a recommendation — so the reason is recorded rather
    than left to be discovered from behaviour.
    """
    try:
        from ragtools.config import Settings
        from ragtools.service.managed_qdrant import find_qdrant_binary

        if find_qdrant_binary(Settings()):
            return RECOMMENDED_BACKEND, ""
    except Exception:  # noqa: BLE001 — absence and error are the same answer here
        pass
    return (FALLBACK_BACKEND,
            f"the managed engine is not available on this installation, so the "
            f"storage backend was set to {FALLBACK_BACKEND!r}; the collection "
            f"layout still moves to {RECOMMENDED_LAYOUT!r}, which needs no binary")


def canonical_document() -> dict:
    """The configuration a CLEAN installation should have.

    Defined here, next to the migration, so "what a v3 config looks like" has
    one answer rather than two that drift. A fresh install used to receive
    ``version = 3`` and nothing else — the storage keys were absent, so the
    runtime fell back to code defaults and the file did not state what the
    product was actually doing. Two installs of the same release could then
    behave differently for reasons nothing on disk explained.

    A new installation has no index, so it takes the v3 layout: adopting it
    costs nothing when there is nothing to re-index.
    """
    engine, _why = _recommended_backend()
    return {
        "version": CONFIG_VERSION,
        **V3_DEFAULTS,
        "storage_backend": engine,
        "collection_strategy": LAYOUT_FOR_NEW_INSTALL,
        "projects": [],
    }


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

    # --- implicit legacy defaults become the recommended architecture ------
    #
    # Absence is the signal, and it is only readable ONCE: after this runs the
    # keys exist, and a later migration cannot tell a value it wrote from one
    # the user chose. That is what `[migration].adopted` records — provenance,
    # so the distinction survives the write that erases it.
    already = set((doc.get("migration") or {}).get("adopted") or [])
    adopted: list[str] = []

    if "storage_backend" not in doc:
        engine, why = _recommended_backend()
        doc["storage_backend"] = engine
        result.added_keys.append("storage_backend")
        adopted.append("storage_backend")
        if why:
            result.notes.append(why)
    else:
        # An explicit choice — including an explicit `embedded` — is a decision.
        # So are the values that describe it: url, credentials, port, ownership.
        result.notes.append(
            f"storage_backend={doc['storage_backend']!r} was set explicitly and "
            "is preserved")

    if "collection_strategy" not in doc:
        doc["collection_strategy"] = RECOMMENDED_LAYOUT
        result.added_keys.append("collection_strategy")
        adopted.append("collection_strategy")
    else:
        result.notes.append(
            f"collection_strategy={doc['collection_strategy']!r} was set "
            "explicitly and is preserved")

    if adopted:
        record = dict(doc.get("migration") or {})
        record["adopted"] = sorted(already | set(adopted))
        record["from_version"] = result.from_version
        doc["migration"] = record
        result.adopted_defaults = list(adopted)
        result.notes.append(
            "legacy configuration had no explicit storage settings; adopted the "
            f"v3 architecture ({doc['storage_backend']} + "
            f"{doc['collection_strategy']}). A full re-index is required and "
            "will run once — the previous index stays queryable until it "
            "completes."
        )

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
    # Whether anything changed is a question about the DOCUMENT, so ask the
    # document. It used to be inferred from three proxies, and one of them —
    # `adopted_dependencies` — is reported on every run, because adoption is
    # idempotent and re-adopting an already-adopted catalog still lists it. So
    # any configuration carrying a dependency catalog reported `changed` for
    # ever, and was rewritten (with a backup) on every single service start.
    #
    # Caught by the real-boot test rather than by the unit tests: the
    # idempotence unit test used a document with no dependencies, so the one
    # input that triggers it was the one input never tried.
    result.changed = doc != document
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
