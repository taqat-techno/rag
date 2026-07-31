"""Filesystem changes seen while an index was being replaced — `pending_changes.db`.

A rebuild holds the index mutex for its whole duration, so **every** watcher
tick that lands during one is told ``busy``. The watcher then discarded that
answer on the assumption that "the next tick will pick it up". It does not:

* nothing schedules a next tick — the watcher only runs when the filesystem
  moves again, so a single edit during a rebuild simply never gets indexed;
* the rebuild rewrites the project's state rows from its OWN scan, so the file
  the user edited is recorded against the hash the rebuild read, and a later
  incremental has no reason to look at it again.

The result was a store holding pre-edit content with no pending work anywhere
to correct it, and no signal that anything had been lost. The only recovery was
a full re-index the user had no reason to know they needed.

This module is the durable ledger that makes those events survivable.

Design
------

**A row records that a file changed — never what it changed to.** Replay
re-reads the disk through the ordinary incremental indexer, so the queue never
has to reconstruct content, reconcile a create against a later delete, or get
an ordering right. Final-state correctness comes from reading final state.
That is why ``kind`` is diagnostic rather than load-bearing, and why a
spurious ``deleted`` from an editor's write-to-temp-then-rename cannot corrupt
anything.

**Dedup is the primary key.** ``(project_id, rel_path)`` — N edits to one file
are one row. Re-observing a path refreshes its ``seq``, ``kind`` and
``last_seen`` and bumps ``hits``.

**Ordering is a global monotonic ``seq``**, allocated as ``MAX(seq) + 1``. It
exists for one purpose: a claim records the highest ``seq`` it covers, and
consumption deletes only up to that watermark. An event observed *while the
replay runs* is allocated a higher ``seq`` and therefore survives — including
when it lands on a path the claim already covered, because the upsert moves
that row forward past the watermark. Without it, a replay would swallow the
change that arrived during it.

**The bound is per project, and overflowing is recorded, not dropped.** Past
``limit`` distinct files, the project's rows are replaced by a single re-scan
marker. Both a marker and a set of rows lead to the same replay action — a
full project scan — so degrading costs precision in the diagnostics and
nothing in correctness.

**A failed replay keeps its rows.** ``record_failure`` is written *instead of*
consuming, so the work is retried at the next boot and the reason is visible in
the meantime. Nothing here is ever swallowed silently.

Separate from ``runtime.db`` (jobs/events) and ``index_state.db`` on purpose:
this store must be openable by a bare :class:`~ragtools.service.owner.QdrantOwner`
with no service around it, and it must not share a schema ladder with a store
that refuses to open when it is newer than the build.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("ragtools.pending_changes")

SCHEMA_VERSION = 1

#: A file that exists (created, modified, or moved into place).
KIND_UPSERT = "upsert"
#: A file that is gone from where it was (deleted, or moved away).
KIND_DELETE = "delete"

#: Default ceiling on distinct pending files per project before the queue
#: degrades to a re-scan marker. Deliberately generous: the queue holds one
#: short row per file, and the whole point is that overflowing is rare enough
#: to be an event worth recording.
DEFAULT_LIMIT = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Claim:
    """One project's outstanding work, and the watermark that covers it.

    Held across the replay so :meth:`PendingChanges.consume` can delete exactly
    what was replayed and nothing that arrived afterwards.
    """

    project_id: str
    seq_high: int
    files: list = field(default_factory=list)
    rescan_reason: str | None = None

    @property
    def is_rescan(self) -> bool:
        return self.rescan_reason is not None

    def describe(self) -> str:
        if self.is_rescan:
            return f"{self.project_id}: full re-scan ({self.rescan_reason})"
        return f"{self.project_id}: {len(self.files)} file(s) changed"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_changes (
    project_id TEXT NOT NULL,
    rel_path   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    abs_path   TEXT,
    seq        INTEGER NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    PRIMARY KEY (project_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_pending_project_seq
    ON pending_changes(project_id, seq);

CREATE TABLE IF NOT EXISTS rescan_markers (
    project_id  TEXT PRIMARY KEY,
    reason      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_failures (
    project_id TEXT PRIMARY KEY,
    error      TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 1,
    first_at   TEXT NOT NULL,
    failed_at  TEXT NOT NULL
);
"""


class PendingChanges:
    """Thread-safe SQLite ledger of changes awaiting replay."""

    def __init__(self, db_path: str, *, limit: int = DEFAULT_LIMIT):
        self.db_path = str(db_path)
        self.limit = max(1, int(limit))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        #: Set under the lock by :meth:`close`, so a watcher write that arrives
        #: during shutdown is a no-op rather than a use of a freed connection.
        #: Same hazard, and the same answer, as ``RuntimeStore.close``.
        self._closed = False
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    # -- schema ---------------------------------------------------------

    def _migrate(self) -> None:
        cur = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if cur == SCHEMA_VERSION:
            return
        if cur > SCHEMA_VERSION:
            raise RuntimeError(
                f"pending_changes.db schema v{cur} is newer than this build "
                f"supports (v{SCHEMA_VERSION}); upgrade ragtools or remove the file."
            )
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> "PendingChanges":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- sequence -------------------------------------------------------

    def _next_seq(self) -> int:
        """One past the highest ``seq`` anywhere in the store.

        Safe against consumption because a claim's rows are still present until
        :meth:`consume` runs, so ``MAX(seq)`` can never fall below an
        outstanding watermark for that project while the claim is open.
        Consumption is scoped by ``project_id``, so another project reusing a
        number is inert.
        """
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM ("
            "  SELECT seq FROM pending_changes UNION ALL SELECT seq FROM rescan_markers"
            ")"
        ).fetchone()
        return int(row["m"]) + 1

    # -- capture --------------------------------------------------------

    def record(self, project_id: str, rel_path: str, kind: str = KIND_UPSERT,
               abs_path: str | None = None) -> str:
        """Record one changed file. Returns ``queued``, ``marked`` or ``overflowed``.

        ``marked``  — the project is already flagged for a full re-scan, so the
                      row would add nothing.
        ``overflowed`` — this call crossed the bound; the project's rows were
                      replaced by a re-scan marker.
        """
        return self.record_many(project_id, [(kind, rel_path, abs_path)])["status"]

    def record_many(self, project_id: str, entries) -> dict:
        """Record several changed files for one project in a single transaction.

        ``entries`` is an iterable of ``(kind, rel_path, abs_path)``.
        """
        entries = list(entries)
        with self._lock:
            if self._closed:
                return {"status": "closed", "queued": 0, "project": project_id}
            if self._marker_for(project_id) is not None:
                return {"status": "marked", "queued": 0, "project": project_id}

            queued = 0
            ts = _now()
            for kind, rel_path, abs_path in entries:
                if not rel_path:
                    continue
                kind = KIND_DELETE if kind == KIND_DELETE else KIND_UPSERT
                known = self.conn.execute(
                    "SELECT 1 FROM pending_changes WHERE project_id = ? AND rel_path = ?",
                    (project_id, rel_path),
                ).fetchone()
                if known is None and self._count_for(project_id) >= self.limit:
                    # The bound is reached and this is a NEW path. Degrade the
                    # whole project to one recorded marker rather than dropping
                    # this event on the floor.
                    reason = (
                        f"more than {self.limit} distinct files changed while the "
                        f"index was being replaced; the queue was replaced by a "
                        f"full re-scan of this project"
                    )
                    self._mark_locked(project_id, reason, ts)
                    self.conn.commit()
                    logger.warning("Pending changes for %s overflowed: %s",
                                   project_id, reason)
                    return {"status": "overflowed", "queued": queued,
                            "project": project_id, "reason": reason}
                self.conn.execute(
                    "INSERT INTO pending_changes "
                    "  (project_id, rel_path, kind, abs_path, seq, hits, first_seen, last_seen) "
                    "VALUES (?,?,?,?,?,1,?,?) "
                    "ON CONFLICT(project_id, rel_path) DO UPDATE SET "
                    "  kind = excluded.kind, abs_path = excluded.abs_path, "
                    "  seq = excluded.seq, hits = pending_changes.hits + 1, "
                    "  last_seen = excluded.last_seen",
                    (project_id, rel_path, kind, abs_path, self._next_seq(), ts, ts),
                )
                queued += 1
            self.conn.commit()
        return {"status": "queued", "queued": queued, "project": project_id}

    def mark_rescan(self, project_id: str, reason: str) -> None:
        """Flag a project as needing a full re-scan, discarding its file rows.

        Used when per-file identity cannot be established — the event is still
        recorded, at coarser resolution, which is the whole contract of the
        bound: degrade, never drop.
        """
        with self._lock:
            if self._closed:
                return
            self._mark_locked(project_id, reason, _now())
            self.conn.commit()

    def _mark_locked(self, project_id: str, reason: str, ts: str) -> None:
        seq = self._next_seq()
        self.conn.execute("DELETE FROM pending_changes WHERE project_id = ?", (project_id,))
        self.conn.execute(
            "INSERT INTO rescan_markers (project_id, reason, seq, recorded_at) "
            "VALUES (?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
            "  reason = excluded.reason, seq = excluded.seq, "
            "  recorded_at = excluded.recorded_at",
            (project_id, reason, seq, ts),
        )

    def _marker_for(self, project_id: str):
        return self.conn.execute(
            "SELECT * FROM rescan_markers WHERE project_id = ?", (project_id,)
        ).fetchone()

    def _count_for(self, project_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM pending_changes WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return int(row["n"])

    # -- replay ---------------------------------------------------------

    def projects_with_work(self) -> list[str]:
        """Every project holding rows or a marker, oldest work first."""
        with self._lock:
            if self._closed:
                return []
            rows = self.conn.execute(
                "SELECT project_id, MIN(seq) AS s FROM ("
                "  SELECT project_id, seq FROM pending_changes"
                "  UNION ALL SELECT project_id, seq FROM rescan_markers"
                ") GROUP BY project_id ORDER BY s"
            ).fetchall()
        return [r["project_id"] for r in rows]

    def claim(self, project_id: str) -> Claim | None:
        """Take a snapshot of one project's work, or ``None`` if it has none.

        Deletes nothing. The rows stay until :meth:`consume`, so a replay that
        crashes leaves the work behind to be retried rather than losing it.
        """
        with self._lock:
            if self._closed:
                return None
            marker = self._marker_for(project_id)
            rows = self.conn.execute(
                "SELECT * FROM pending_changes WHERE project_id = ? ORDER BY seq",
                (project_id,),
            ).fetchall()
        if marker is None and not rows:
            return None
        seqs = [int(r["seq"]) for r in rows]
        if marker is not None:
            seqs.append(int(marker["seq"]))
        return Claim(
            project_id=project_id,
            seq_high=max(seqs),
            files=[{"path": r["rel_path"], "kind": r["kind"], "hits": r["hits"],
                    "first_seen": r["first_seen"], "last_seen": r["last_seen"]}
                   for r in rows],
            rescan_reason=(marker["reason"] if marker is not None else None),
        )

    def consume(self, claim: Claim) -> int:
        """Delete exactly the work ``claim`` covered. Returns rows removed.

        Bounded by ``seq_high`` so a change observed during the replay — which
        the replay's own scan may well have missed — is still outstanding
        afterwards.
        """
        with self._lock:
            if self._closed:
                return 0
            cur = self.conn.execute(
                "DELETE FROM pending_changes WHERE project_id = ? AND seq <= ?",
                (claim.project_id, claim.seq_high),
            )
            self.conn.execute(
                "DELETE FROM rescan_markers WHERE project_id = ? AND seq <= ?",
                (claim.project_id, claim.seq_high),
            )
            self.conn.execute(
                "DELETE FROM replay_failures WHERE project_id = ?", (claim.project_id,)
            )
            self.conn.commit()
            return cur.rowcount

    # -- diagnostics ----------------------------------------------------

    def record_failure(self, project_id: str, error: str) -> None:
        """Record that a replay failed. The work is NOT consumed.

        Called instead of :meth:`consume`, so the rows survive to be retried and
        the reason is visible until they are.
        """
        with self._lock:
            if self._closed:
                return
            ts = _now()
            self.conn.execute(
                "INSERT INTO replay_failures (project_id, error, attempts, first_at, failed_at) "
                "VALUES (?,?,1,?,?) ON CONFLICT(project_id) DO UPDATE SET "
                "  error = excluded.error, attempts = replay_failures.attempts + 1, "
                "  failed_at = excluded.failed_at",
                (project_id, str(error)[:2000], ts, ts),
            )
            self.conn.commit()
        logger.error("Replay failed for project %s: %s", project_id, error)

    def failures(self) -> dict:
        with self._lock:
            if self._closed:
                return {}
            rows = self.conn.execute("SELECT * FROM replay_failures").fetchall()
        return {r["project_id"]: {"error": r["error"], "attempts": r["attempts"],
                                  "first_at": r["first_at"], "failed_at": r["failed_at"]}
                for r in rows}

    def report(self) -> dict:
        """What is outstanding and why — the diagnostic surface.

        ``pending_files`` counts rows only; a project degraded to a marker has
        no rows, which is exactly why ``rescan_required`` is reported
        separately rather than as a count of zero.
        """
        with self._lock:
            if self._closed:
                return {"pending_files": 0, "projects": {}, "failures": {},
                        "rescan_required": {}, "limit": self.limit}
            rows = self.conn.execute(
                "SELECT project_id, COUNT(*) AS n, MIN(first_seen) AS oldest "
                "FROM pending_changes GROUP BY project_id"
            ).fetchall()
            markers = self.conn.execute("SELECT * FROM rescan_markers").fetchall()
            fails = self.conn.execute("SELECT * FROM replay_failures").fetchall()
        projects = {r["project_id"]: {"files": r["n"], "oldest": r["oldest"]}
                    for r in rows}
        return {
            "pending_files": sum(p["files"] for p in projects.values()),
            "projects": projects,
            "rescan_required": {m["project_id"]: m["reason"] for m in markers},
            "failures": {f["project_id"]: {"error": f["error"],
                                           "attempts": f["attempts"],
                                           "failed_at": f["failed_at"]}
                         for f in fails},
            "limit": self.limit,
        }
