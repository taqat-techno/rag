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

import sqlite3
import threading
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


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
    return conn

from ragtools.identity import (
    framework_collection_name,
    project_collection_name,
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


class ProjectRegistry:
    """SQLite-backed store for project identity and lifecycle."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        # Serialises every use of the connection. The relaxed
        # check_same_thread in _connect is only safe because of this lock.
        self._lock = threading.RLock()
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
                archived        INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.commit()

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
        )


def sync_projects_from_config(configs, registry: "ProjectRegistry") -> dict:
    """Populate ``registry`` from the live TOML project configs, idempotently.

    Each config (anything with ``id`` / ``path`` and optional ``mode`` / ``name``)
    that is new is added; an existing one whose path or mode changed is updated
    in place — UUID and collection are preserved (§11.2), because identity is the
    UUID, not the path. Returns ``{added, updated, unchanged}`` counts.

    This is the safe first step of the collection-per-project migration: it makes
    the registry mirror the live projects WITHOUT changing the search path or
    creating per-project collections. Nothing here reverses the single-collection
    model — that switch is a separate, deliberate act.
    """
    added = updated = unchanged = 0
    for cfg in configs:
        pid = cfg.id
        mode = getattr(cfg, "mode", "docs") or "docs"
        path = cfg.path
        name = getattr(cfg, "name", None)
        existing = registry.get(pid)
        if existing is None:
            registry.add(pid, path=path, display_name=name, mode=mode)
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
    return {"added": added, "updated": updated, "unchanged": unchanged}


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
