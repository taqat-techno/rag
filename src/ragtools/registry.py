"""Persistent project registry — UUID identity + collection lifecycle (S6, §11).

A project is an immutable UUID with a mutable display identity, owning exactly
one collection whose name derives from the UUID (:func:`ragtools.identity.project_collection_name`).
Because identity is the UUID — never the editable id or the movable path — a
rename or a path move leaves both the UUID and the collection untouched, and two
folders with the same basename under different parents are distinct projects.

Three lifecycle verbs are three distinct outcomes (§11.3):

* **archive** — keep the row and the collection; just stop treating it as active.
* **remove** — drop the row; the collection is orphaned (returned so the caller
  can decide) but not destroyed.
* **delete collection** — a separate destructive act against Qdrant, not here.

SQLite with the S5 hardening (WAL + busy timeout) so concurrent readers (the
service) never block the writer.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S6 §11 -> G6)
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ragtools.registry")

class RegistryIntegrityError(RuntimeError):
    """A registry whose contents cannot be vouched for refused an operation.

    Raised by the operations that would make an unproven mapping permanent —
    minting a new project identity, and swapping a project's active collection
    — while a hold is in force (:meth:`ProjectRegistry.hold`). The hold is armed
    by :func:`ragtools.registry_integrity.reconcile_startup` when the live
    registry is not the one the index was built against.

    Re-adoption is deliberately NOT refused: it is the recovery verb, and it
    only ever restores an identity the collection name already encodes.
    """


#: ``PRAGMA synchronous`` value that fsyncs at every commit. Under WAL the
#: usual advice is NORMAL (1), which does NOT fsync the WAL on commit: a
#: committed transaction can be lost to a power failure. That trade is fine for
#: a cache and wrong for this file — see :meth:`ProjectRegistry.set_active_collection`.
SYNCHRONOUS_FULL = 2


def _connect(db_path: str) -> sqlite3.Connection:
    """Open a registry connection usable from every service thread.

    A registry is consulted by the request thread (status, search), the job
    worker (indexing) and the watcher — all of which run in one process. The
    default ``check_same_thread=True`` makes any cross-thread use raise
    ``ProgrammingError``, which surfaced only once the router was wired into the
    live indexing path. Access is serialised by the owning class's lock, so
    relaxing the check is safe rather than merely convenient.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL + a real busy timeout, so a concurrent reader never gets an instant
    # "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # FULL, not the WAL-default NORMAL. Registry writes are rare (add, rename,
    # move, mode, swap) so the fsync costs nothing measurable, and this file is
    # the only record of which collection holds a project's vectors: a commit
    # lost to a power cut points the project back at the collection the rebuild
    # was replacing, with no way to tell from the data which one is current.
    conn.execute(f"PRAGMA synchronous={SYNCHRONOUS_FULL}")
    return conn


def _synchronous_level(conn: sqlite3.Connection) -> int:
    """What ``PRAGMA synchronous`` is *actually* set to on this connection."""
    row = conn.execute("PRAGMA synchronous").fetchone()
    if row is None:
        return -1
    return int(row[0])

from ragtools.identity import (
    framework_collection_name,
    project_collection_name,
    project_uuid_from_collection_name,
    validate_project_id,
)


@dataclass
class ProjectRecord:
    """One registered project."""

    uuid: str
    project_id: str
    display_name: str
    path: str
    mode: str
    collection_name: str
    created_at: str
    archived: bool = False
    #: Which rebuild generation ``collection_name`` refers to. Generation 0 is
    #: the name a v3.4 install already has, so upgrading moves no data.
    generation: int = 0


class ProjectRegistry:
    """SQLite-backed store for project identity and lifecycle."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        # Serialises every use of the connection. The relaxed
        # check_same_thread in _connect is only safe because of this lock.
        self._lock = threading.RLock()
        #: Why identity-creating writes are refused, or "" when they are not.
        #: In memory on purpose: it is a statement about THIS process's belief,
        #: re-derived from the state DB at every start by
        #: ``registry_integrity.reconcile_startup``. Persisting it would
        #: outlive the evidence and leave an install wedged with no file to
        #: point at.
        self._hold_reason = ""
        self._conn = _connect(db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                uuid            TEXT PRIMARY KEY,
                project_id      TEXT UNIQUE NOT NULL,
                display_name    TEXT,
                path            TEXT,
                mode            TEXT NOT NULL DEFAULT 'docs',
                collection_name TEXT NOT NULL,
                created_at      TEXT,
                archived        INTEGER NOT NULL DEFAULT 0,
                generation      INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._add_missing_columns()
        self._conn.commit()

    def _add_missing_columns(self) -> None:
        """Bring a registry written by an older version up to this schema.

        ``CREATE TABLE IF NOT EXISTS`` does NOT add a column to a table that
        already exists, so a v3.4 registry would open without ``generation``
        and every read would raise. Same trap, same fix as
        ``relayout._add_retry_columns``.
        """
        have = {r[1] for r in self._conn.execute("PRAGMA table_info(projects)")}
        if "generation" not in have:
            # DEFAULT 0: an upgraded project keeps reading the collection it
            # already has. The upgrade must never repoint anything by itself.
            self._conn.execute(
                "ALTER TABLE projects ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")

    # -- integrity hold -------------------------------------------------

    def hold(self, reason: str) -> None:
        """Refuse identity-creating writes until :meth:`release_hold`.

        Armed when this installation cannot prove the live registry is the one
        its index was built against. Two operations are then refused:

        * ``add`` — minting a fresh UUID for a project that already has a
          collection full of its vectors is how a recoverable loss becomes a
          permanent one (N empty ``proj_<hex>`` beside the N that hold the
          data);
        * ``set_active_collection`` — repointing a project while it is unclear
          which collection currently holds it writes the confusion down.

        Reads, renames, moves, mode changes and re-adoption stay available: a
        held registry must still be *diagnosable* and *recoverable*, or the
        guard becomes the outage.
        """
        self._hold_reason = str(reason or "registry integrity is unresolved")

    def release_hold(self) -> None:
        """Clear the hold. Idempotent."""
        self._hold_reason = ""

    @property
    def hold_reason(self) -> str:
        """Why writes are held, or "" when they are not."""
        return getattr(self, "_hold_reason", "")

    def _assert_writable(self, operation: str) -> None:
        reason = self.hold_reason
        if reason:
            raise RegistryIntegrityError(
                f"refusing to {operation}: {reason}. Nothing has been deleted; "
                f"re-adopt the existing collections or restore registry.db, then "
                f"reconcile."
            )

    # -- reads ----------------------------------------------------------

    def get(self, project_id: str) -> Optional[ProjectRecord]:
        # The lock must span execute AND fetch: a cursor consumed outside it is
        # exactly the cross-thread race this class exists inside a service to
        # avoid.
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        return self._record(row) if row else None

    def get_by_uuid(self, project_uuid: str) -> Optional[ProjectRecord]:
        """Look a project up by its immutable identity.

        Needed by re-adoption: a collection name carries a UUID, and before
        reattaching it to a project we must know whether some *other* project
        already holds that identity.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE uuid = ?", (str(project_uuid),)
            ).fetchone()
        return self._record(row) if row else None

    def list(self, *, include_archived: bool = False) -> list[ProjectRecord]:
        sql = "SELECT * FROM projects"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY created_at, project_id"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [self._record(r) for r in rows]

    # -- writes ---------------------------------------------------------

    def add(
        self,
        project_id: str,
        *,
        path: str,
        display_name: Optional[str] = None,
        mode: str = "docs",
    ) -> ProjectRecord:
        """Register a project: validate the id, mint a UUID, derive the collection."""
        pid = validate_project_id(project_id)
        # A fresh uuid4 here is a fresh, EMPTY collection. That is right for a
        # genuinely new project and catastrophic for one whose registry row was
        # merely lost, so it is refused while integrity is unresolved.
        self._assert_writable(f"mint a new identity for project {pid!r}")
        if self.get(pid) is not None:
            raise ValueError(f"project id {pid!r} already exists")
        u = str(_uuid.uuid4())
        rec = ProjectRecord(
            uuid=u,
            project_id=pid,
            display_name=display_name or pid,
            path=path,
            mode=mode,
            collection_name=project_collection_name(u),
            created_at=datetime.now(timezone.utc).isoformat(),
            archived=False,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO projects (uuid, project_id, display_name, path, mode, "
                "collection_name, created_at, archived) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (rec.uuid, rec.project_id, rec.display_name, rec.path, rec.mode,
                 rec.collection_name, rec.created_at),
            )
            self._conn.commit()
        return rec

    def rename(self, project_id: str, new_id: str) -> ProjectRecord:
        """Change the display id; UUID and collection are unchanged (§11.2)."""
        new_pid = validate_project_id(new_id)
        rec = self._require(project_id)
        if new_pid != project_id and self.get(new_pid) is not None:
            raise ValueError(f"project id {new_pid!r} already exists")
        with self._lock:
            self._conn.execute(
                "UPDATE projects SET project_id = ? WHERE uuid = ?", (new_pid, rec.uuid)
            )
            self._conn.commit()
        return self._require(new_pid)

    def move(self, project_id: str, new_path: str) -> ProjectRecord:
        """Update the path; identity and collection are unchanged (§11.2)."""
        rec = self._require(project_id)
        with self._lock:
            self._conn.execute(
                "UPDATE projects SET path = ? WHERE uuid = ?", (new_path, rec.uuid)
            )
            self._conn.commit()
        return self._require(project_id)

    def set_mode(self, project_id: str, mode: str) -> ProjectRecord:
        """Change the indexing mode (docs|code|general); identity unchanged."""
        rec = self._require(project_id)
        with self._lock:
            self._conn.execute(
                "UPDATE projects SET mode = ? WHERE uuid = ?", (mode, rec.uuid)
            )
            self._conn.commit()
        return self._require(project_id)

    def archive(self, project_id: str) -> None:
        """Verb 1: stop treating the project as active; keep row and collection."""
        rec = self._require(project_id)
        with self._lock:
            self._conn.execute(
                "UPDATE projects SET archived = 1 WHERE uuid = ?", (rec.uuid,)
            )
            self._conn.commit()

    def remove(self, project_id: str) -> str:
        """Verb 2: drop from the registry; return the now-orphaned collection name.

        The collection itself is NOT destroyed — that is verb 3, a separate
        explicit act. Returning the name lets the caller decide.
        """
        rec = self._require(project_id)
        with self._lock:
            self._conn.execute("DELETE FROM projects WHERE uuid = ?", (rec.uuid,))
            self._conn.commit()
        return rec.collection_name

    # -- lifecycle ------------------------------------------------------

    def set_active_collection(self, project_uuid: str, collection_name: str,
                              *, generation: int) -> ProjectRecord:
        """Point a project at a different collection. THE SWAP PRIMITIVE.

        One UPDATE, so it either happened or it did not — there is no state in
        which the project half-points at a replacement. That is the property a
        safe rebuild needs and the reason this is done here rather than with a
        Qdrant alias: the embedded backend (the product default) implements
        aliases as an in-memory dict with a deferred save, and Qdrant has no
        rename at all.

        The caller must have VERIFIED the new collection first. Nothing here
        can tell whether the replacement is any good; this only makes the
        handover atomic once it is.

        Atomic is not the same as durable, and v3.5.0 bought only the first.
        Two things are therefore proven rather than assumed:

        * the connection really is fsyncing at commit (``PRAGMA synchronous``
          is re-checked here, not merely set at open — one ``PRAGMA`` on a
          reused connection is all it takes to silently downgrade this file to
          "probably written");
        * the row really changed, read back after the commit. The read-back is
          on the same connection, so it proves the write landed in the database
          rather than in a lost cursor; ``synchronous=FULL`` is what carries it
          to the platter.

        A swap that cannot be proven raises. The caller has just built and
        verified a replacement collection; failing loudly leaves it standing and
        the project on its old one, which is recoverable. Returning as if the
        flip happened is not.
        """
        with self._lock:
            # Ownership before durability: fsyncing a pointer nobody can vouch
            # for only makes the wrong answer permanent. This is also the guard
            # WP-R03's reaper consults before it may consider a collection
            # orphaned — see `registry_integrity.assert_reaping_allowed`.
            self._assert_writable(
                f"swap the active collection of project {project_uuid!r}")
            self._assert_durable()
            cur = self._conn.execute(
                "UPDATE projects SET collection_name = ?, generation = ? WHERE uuid = ?",
                (collection_name, int(generation), project_uuid),
            )
            if cur.rowcount == 0:
                raise ValueError(f"no project with uuid {project_uuid!r}")
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM projects WHERE uuid = ?", (project_uuid,)).fetchone()
        if row is None:
            raise RuntimeError(
                f"the collection swap for {project_uuid!r} committed but the row "
                f"is gone; the registry cannot be trusted"
            )
        rec = self._record(row)
        if rec.collection_name != collection_name or rec.generation != int(generation):
            raise RuntimeError(
                f"the collection swap for {project_uuid!r} did not land: expected "
                f"{collection_name!r} generation {int(generation)}, read back "
                f"{rec.collection_name!r} generation {rec.generation}"
            )
        return rec

    def _assert_durable(self) -> None:
        """Re-establish and verify ``PRAGMA synchronous`` before a swap.

        Called with ``self._lock`` held. Re-applying is cheap and idempotent;
        verifying is the point, because "we set it at open" is exactly the kind
        of claim that stays true until something else on the connection changes
        it.
        """
        level = _synchronous_level(self._conn)
        if level >= SYNCHRONOUS_FULL:
            return
        self._conn.execute(f"PRAGMA synchronous={SYNCHRONOUS_FULL}")
        level = _synchronous_level(self._conn)
        if level < SYNCHRONOUS_FULL:
            raise RuntimeError(
                f"refusing to swap a collection on a registry that does not "
                f"fsync: PRAGMA synchronous is {level}, expected "
                f">= {SYNCHRONOUS_FULL} (FULL)"
            )

    def _readopt(
        self,
        project_id: str,
        project_uuid: str,
        collection_name: str,
        *,
        path: str,
        display_name: Optional[str],
        mode: str,
        generation: int,
    ) -> ProjectRecord:
        """Insert-or-update the row that reattaches a project to a collection.

        Private: the public entry point is :func:`readopt_collection`, which
        owns the checks that make the reattachment legitimate.
        """
        existing = self.get(project_id)
        with self._lock:
            self._assert_durable()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO projects (uuid, project_id, display_name, path, "
                    "mode, collection_name, created_at, archived, generation) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                    (project_uuid, project_id, display_name or project_id, path,
                     mode, collection_name,
                     datetime.now(timezone.utc).isoformat(), int(generation)),
                )
            else:
                # In place, keyed by the id we were asked to reattach. The uuid
                # moves to the one the collection encodes — that is the whole
                # operation: identity follows the data, not the other way round.
                self._conn.execute(
                    "UPDATE projects SET uuid = ?, display_name = ?, path = ?, "
                    "mode = ?, collection_name = ?, generation = ? "
                    "WHERE project_id = ?",
                    (project_uuid, display_name or existing.display_name, path,
                     mode, collection_name, int(generation), project_id),
                )
            self._conn.commit()
        rec = self.get_by_uuid(project_uuid)
        if rec is None or rec.collection_name != collection_name:
            raise RuntimeError(
                f"re-adoption of {collection_name!r} by {project_id!r} did not land"
            )
        return rec

    def close(self) -> None:
        """Release the SQLite connection. Idempotent.

        Not optional on Windows: an open handle keeps the .db file locked, so a
        caller that opens a registry and never closes it leaves an undeletable
        file behind (and a service restart cannot replace it).
        """
        conn, self._conn = getattr(self, "_conn", None), None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- helpers --------------------------------------------------------

    def _require(self, project_id: str) -> ProjectRecord:
        rec = self.get(project_id)
        if rec is None:
            raise KeyError(f"no such project: {project_id!r}")
        return rec

    @staticmethod
    def _record(row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            uuid=row["uuid"],
            project_id=row["project_id"],
            display_name=row["display_name"],
            path=row["path"],
            mode=row["mode"],
            collection_name=row["collection_name"],
            created_at=row["created_at"],
            archived=bool(row["archived"]),
            generation=int(row["generation"] if "generation" in row.keys() else 0),
        )


def project_mapping(registry: "ProjectRegistry") -> dict[str, str]:
    """``project_id -> collection_name`` for every project, archived included.

    The thing the fingerprint digests, kept available undigested because the
    interesting questions ("which project moved?", "did the restore bring back
    the same mapping?") cannot be answered from a hash.
    """
    return {rec.project_id: rec.collection_name
            for rec in registry.list(include_archived=True)}


def registry_fingerprint(registry: "ProjectRegistry") -> str:
    """A stable digest of the whole ``project_id -> collection_name`` mapping.

    **This is a GLOBAL INTEGRITY signal — "is this the same registry?" — and
    nothing else.** Its consumers are registry corruption / replacement /
    rollback detection, backup and restore validation, degraded health
    reporting, the block on unsafe pointer swaps and orphan reaping, and
    startup reconciliation.

    It is deliberately NOT an input to per-project invalidation, and the first
    R06 shipping it as one is the defect this refinement repairs: because the
    digest covers every project, adding the sixteenth changed it, the state DB
    was declared untrustworthy wholesale, and the fifteen already-indexed
    projects were re-embedded for a change that moved none of them. "Is THIS
    project's mapping still valid?" is answered per project by
    :class:`ragtools.index_identity.ProjectIdentity`.

    Properties it needs, and why:

    * **Archived projects count.** ``archive`` keeps both the row and the
      vectors, so an archived project's collection is still part of the
      mapping. Excluding them would make archiving invalidate the whole state
      DB and re-embed the corpus for a change that moved nothing.
    * **Sorted.** Row order is an implementation detail of SQLite; the mapping
      is a set.
    * **Empty is "".** No projects means there is nothing to be wrong about,
      and "" is already the "unknown" value — an install with an empty registry
      and one with no registry at all should behave identically.

    A change here means *the mapping is not the one recorded* — no more than
    that. It says nothing about WHICH project moved, or whether anything moved
    at all: a project merely appearing changes it too. Turning that unqualified
    signal into a skip decision is the fused reading; ask
    :func:`ragtools.registry_integrity.evaluate`, which localises the change
    against the per-project rows and can tell an addition from a rollback.
    """
    pairs = sorted(
        (rec.project_id, rec.collection_name)
        for rec in registry.list(include_archived=True)
    )
    if not pairs:
        return ""
    payload = "\x1e".join(f"{pid}\x1f{coll}" for pid, coll in pairs)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def owns_collection(registry: "ProjectRegistry", collection_name: str) -> bool:
    """Did THIS installation create ``collection_name``? Proven, not guessed.

    True only when some row's ``collection_name`` is exactly this name.
    ``proj_<32hex>`` is a *shape*: another installation sharing a managed engine
    produces collections that match it perfectly, and this installation's
    registry is the only thing that knows which of them are its own.

    The project has made the opposite mistake once already —
    ``obsolete_collections`` computed ``existing - current`` and handed the
    result to a caller that deletes, which on a shared engine is another
    install's entire index. Deciding by prefix or regex is the same error with
    a friendlier face.
    """
    if not collection_name:
        return False
    name = str(collection_name).strip()
    return any(rec.collection_name == name
               for rec in registry.list(include_archived=True))


def readopt_collection(
    registry: "ProjectRegistry",
    project_id: str,
    collection_name: str,
    *,
    path: str,
    display_name: Optional[str] = None,
    mode: str = "docs",
    generation: int = 0,
) -> ProjectRecord:
    """Reattach an existing collection to a project, PRESERVING its identity.

    The recovery half of R06. When ``registry.db`` is lost, the collections are
    not: their names still encode the UUIDs they were derived from. Re-adoption
    reads the UUID back out of the name (:func:`ragtools.identity.project_uuid_from_collection_name`)
    and rebuilds the row around it, so the project points at the vectors it
    already has. ``registry.add`` would mint a fresh uuid4 instead, creating an
    empty collection beside the full one — which is precisely how a recoverable
    loss becomes a permanent one.

    Nothing is deleted here, and nothing is stolen: re-adopting a collection
    that another project already holds is refused, because two rows pointing at
    one collection is a merge nobody asked for.
    """
    pid = validate_project_id(project_id)
    # Raises InvalidProjectId for anything that is not proj_<32 hex>. A name we
    # cannot invert is a name whose identity we would be inventing.
    project_uuid = project_uuid_from_collection_name(collection_name)
    canonical = project_collection_name(project_uuid)

    holder = registry.get_by_uuid(project_uuid)
    if holder is not None and holder.project_id != pid:
        raise ValueError(
            f"collection {canonical!r} already belongs to project "
            f"{holder.project_id!r}; refusing to reattach it to {pid!r}"
        )
    existing = registry.get(pid)
    if existing is not None and existing.collection_name != canonical:
        logger.warning(
            "re-adopting %s for project %s; its previous collection %s is left "
            "in place (nothing here deletes)",
            canonical, pid, existing.collection_name,
        )
    return registry._readopt(
        pid, project_uuid, canonical,
        path=path, display_name=display_name, mode=mode, generation=generation,
    )


def sync_projects_from_config(configs, registry: "ProjectRegistry") -> dict:
    """Populate ``registry`` from the live TOML project configs, idempotently.

    Each config (anything with ``id`` / ``path`` and optional ``mode`` / ``name``)
    that is new is added; an existing one whose path or mode changed is updated
    in place — UUID and collection are preserved (§11.2), because identity is the
    UUID, not the path. Returns ``{added, updated, unchanged, blocked}``.

    This is the safe first step of the collection-per-project migration: it makes
    the registry mirror the live projects WITHOUT changing the search path or
    creating per-project collections. Nothing here reverses the single-collection
    model — that switch is a separate, deliberate act.

    **A refused mint is reported, never raised and never silent.** This function
    runs at boot for every configured project, so a lost registry.db would send
    it down the "new project" branch for all of them at once — which is exactly
    the case where minting a fresh uuid4 produces N empty collections beside the
    N that hold the data. While the registry is held (see
    :meth:`ProjectRegistry.hold`) those projects are listed in ``blocked``
    instead: the boot completes, the projects are visibly absent rather than
    invisibly empty, and ``readopt_collection`` can still reclaim them.
    """
    added = updated = unchanged = 0
    blocked: list[str] = []
    for cfg in configs:
        pid = cfg.id
        mode = getattr(cfg, "mode", "docs") or "docs"
        path = cfg.path
        name = getattr(cfg, "name", None)
        existing = registry.get(pid)
        if existing is None:
            try:
                registry.add(pid, path=path, display_name=name, mode=mode)
            except RegistryIntegrityError as exc:
                blocked.append(pid)
                logger.error(
                    "Not registering project %r: %s", pid, exc)
                continue
            added += 1
            continue
        changed = False
        if existing.path != path:
            registry.move(pid, path)
            changed = True
        if existing.mode != mode:
            registry.set_mode(pid, mode)
            changed = True
        updated += 1 if changed else 0
        unchanged += 0 if changed else 1
    return {"added": added, "updated": updated, "unchanged": unchanged,
            "blocked": blocked}


class FrameworkLinkError(RuntimeError):
    """A framework edition still linked by ≥1 project cannot be removed (§12.5)."""


@dataclass
class FrameworkRecord:
    """A framework corpus, identified and deduplicated by build identity."""

    collection_name: str
    name: str
    version: str
    edition: str
    build_id: Optional[str]
    canonical_root: str
    created_at: str


class FrameworkRegistry:
    """Frameworks (one collection per build identity) and their project links.

    Registering the same build twice reuses the one collection — the S7 dedup
    that keeps a 92–99%-of-volume framework corpus from being indexed per
    project. Links are first-class so freshness can fan out to every linked
    project, and a still-linked edition cannot be removed.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = _connect(db_path)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS frameworks (
                collection_name TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                version         TEXT,
                edition         TEXT,
                build_id        TEXT,
                canonical_root  TEXT,
                created_at      TEXT
            );
            CREATE TABLE IF NOT EXISTS project_framework_links (
                project_uuid        TEXT NOT NULL,
                framework_collection TEXT NOT NULL,
                link_kind           TEXT NOT NULL DEFAULT 'detected',
                confidence          REAL NOT NULL DEFAULT 1.0,
                detector            TEXT,
                PRIMARY KEY (project_uuid, framework_collection)
            );
            """
        )
        self._conn.commit()

    def register(
        self,
        *,
        name: str,
        version: str,
        edition: str,
        build_id: Optional[str],
        canonical_root: str,
    ) -> tuple:
        """Register a framework build; reuse the collection if it already exists.

        Returns ``(record, created)`` where ``created`` is False when a prior
        build with the same identity was reused — the whole dedup point.
        """
        collection = framework_collection_name(
            name, version=version, edition=edition, build_id=build_id
        )
        existing = self.get(collection)
        if existing is not None:
            return existing, False
        rec = FrameworkRecord(
            collection_name=collection,
            name=name,
            version=version,
            edition=edition,
            build_id=build_id,
            canonical_root=canonical_root,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO frameworks (collection_name, name, version, edition, "
                "build_id, canonical_root, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rec.collection_name, rec.name, rec.version, rec.edition,
                 rec.build_id, rec.canonical_root, rec.created_at),
            )
            self._conn.commit()
        return rec, True

    def get(self, collection_name: str) -> Optional[FrameworkRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM frameworks WHERE collection_name = ?", (collection_name,)
            ).fetchone()
        if not row:
            return None
        return FrameworkRecord(
            collection_name=row["collection_name"], name=row["name"],
            version=row["version"], edition=row["edition"], build_id=row["build_id"],
            canonical_root=row["canonical_root"], created_at=row["created_at"],
        )

    def list(self) -> list["FrameworkRecord"]:
        """Every registered corpus, linked or not.

        Registration happens BEFORE the corpus is indexed and linking after, so
        anything that enumerates only linked corpora reports a long-running
        import as nothing at all.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM frameworks ORDER BY name, version, collection_name"
            ).fetchall()
        return [
            FrameworkRecord(
                collection_name=r["collection_name"], name=r["name"],
                version=r["version"], edition=r["edition"], build_id=r["build_id"],
                canonical_root=r["canonical_root"], created_at=r["created_at"],
            )
            for r in rows
        ]

    def link(
        self,
        project_uuid: str,
        framework_collection: str,
        *,
        link_kind: str = "detected",
        confidence: float = 1.0,
        detector: str = "",
    ) -> None:
        """Link a project to a framework corpus (idempotent)."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO project_framework_links "
                "(project_uuid, framework_collection, link_kind, confidence, detector) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_uuid, framework_collection, link_kind, confidence, detector),
            )
            self._conn.commit()

    def unlink(self, project_uuid: str, framework_collection: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM project_framework_links WHERE project_uuid = ? "
                "AND framework_collection = ?",
                (project_uuid, framework_collection),
            )
            self._conn.commit()

    def framework_collections_for(self, project_uuid: str) -> list[str]:
        """The framework collections a project is linked to (feeds the router).

        Called on the SEARCH path, from whichever thread is serving the request
        — hence the lock around both execute and fetch.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT framework_collection FROM project_framework_links "
                "WHERE project_uuid = ? ORDER BY framework_collection",
                (project_uuid,),
            ).fetchall()
        return [r["framework_collection"] for r in rows]

    def projects_for(self, framework_collection: str) -> list[str]:
        """The project UUIDs linked to a framework — freshness fans out to all."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT project_uuid FROM project_framework_links "
                "WHERE framework_collection = ? ORDER BY project_uuid",
                (framework_collection,),
            ).fetchall()
        return [r["project_uuid"] for r in rows]

    def remove(self, framework_collection: str) -> None:
        """Remove a framework — REFUSED while any project still links it (§12.5)."""
        blockers = self.projects_for(framework_collection)
        if blockers:
            raise FrameworkLinkError(
                f"cannot remove framework {framework_collection!r}: still linked by "
                f"{blockers}. Unlink these projects first."
            )
        with self._lock:
            self._conn.execute(
                "DELETE FROM frameworks WHERE collection_name = ?", (framework_collection,)
            )
            self._conn.commit()

    def close(self) -> None:
        """Release the SQLite connection. Idempotent (see ProjectRegistry.close)."""
        conn, self._conn = getattr(self, "_conn", None), None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
