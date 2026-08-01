"""Durable job + event store — `runtime.db` (Phase 2 / W2).

The service's runtime lifecycle finally gets an owner. Two tables:

* **jobs** — every long operation (index, reindex, rebuild, purge) as a durable
  record with identity, state, progress, result, verification and cancellation.
* **events** — an append-only log whose autoincrement ``id`` is a monotonic
  cursor. It backs both the activity drawer and the SSE stream, where it maps
  directly onto ``Last-Event-ID``.

**Why a separate database.** `index_state.db` owns per-file index state and is
*deleted* by `owner.rebuild()` — job history stored there would be destroyed by
the very rebuild that was running. `profiles.db` owns authz identity. Qdrant owns
vectors. Service runtime lifecycle had no owner; this is it.

Design notes:

* WAL + `busy_timeout` so a status read never blocks behind a heavy index write.
* `PRAGMA user_version` migration ladder, adopted from `indexing/state.py` —
  the only store in the codebase that had one.
* Progress throttling is the *worker's* job, not the store's: a 37k-file index
  must not write a row per file.

Plan: docs/planning/RAG_MODERNIZATION_MASTER_PLAN.md  (Phase 2 -> G2)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("ragtools.runtime_store")

SCHEMA_VERSION = 1

#: Job kinds the service knows how to run.
KIND_INDEX = "index"
KIND_REINDEX = "reindex"
KIND_REBUILD = "rebuild"
KIND_PURGE = "purge"

#: Kinds whose success MUST be verified against the store before being reported
#: (they destroy data). See `finish(..., verified=...)`.
DESTRUCTIVE_KINDS = frozenset({KIND_REBUILD, KIND_PURGE, KIND_REINDEX})


class StoreClosed(RuntimeError):
    """Raised when NEW work is handed to a store that has been closed.

    Only :meth:`RuntimeStore.submit` raises it. Everything a running job can
    call answers instead — see :meth:`RuntimeStore.close`. Queuing is the one
    operation where an answer would be a lie: telling a caller its job was
    accepted, and handing back an id nothing will ever run, is worse than the
    error.

    ``RuntimeError`` is the base ON PURPOSE. Three of the four ``submit`` call
    sites already catch ``RuntimeError`` — the one ``get_runtime()`` raises when
    there is no store at all — and fall back to running the work on a thread.
    "The store is closed" wants that same fallback, so it inherits the handling
    rather than needing new handling written at every site.
    """


class JobState:
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED, INTERRUPTED})
    ACTIVE = frozenset({QUEUED, RUNNING})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    kind: str
    scope: dict
    state: str
    progress_done: int = 0
    progress_total: Optional[int] = None
    phase: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    verified: Optional[bool] = None
    idempotency_key: Optional[str] = None
    cancel_requested: bool = False
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    instance_id: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        return self.state in JobState.TERMINAL

    def to_dict(self) -> dict:
        d = {
            "job_id": self.id, "kind": self.kind, "scope": self.scope, "state": self.state,
            "progress": {"done": self.progress_done, "total": self.progress_total,
                         "phase": self.phase},
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "cancel_requested": self.cancel_requested,
        }
        if self.result is not None:
            d["result"] = self.result
        if self.error is not None:
            d["error"] = self.error
        if self.verified is not None:
            d["verified"] = self.verified
        return d


@dataclass
class Event:
    id: int
    ts: str
    type: str
    source: str
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "ts": self.ts, "type": self.type,
                "source": self.source, "payload": self.payload}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    scope_json       TEXT NOT NULL DEFAULT '{}',
    state            TEXT NOT NULL,
    progress_done    INTEGER NOT NULL DEFAULT 0,
    progress_total   INTEGER,
    phase            TEXT,
    result_json      TEXT,
    error            TEXT,
    verified         INTEGER,
    idempotency_key  TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    instance_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
-- Idempotency applies only to jobs that have not finished; a completed job must
-- not block an identical later request. Enforced by a partial unique index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idem_active
    ON jobs(idempotency_key)
    WHERE idempotency_key IS NOT NULL AND state IN ('queued','running');

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    type         TEXT NOT NULL,
    source       TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
"""


class RuntimeStore:
    """Thread-safe SQLite store for jobs and events."""

    def __init__(self, db_path: str, instance_id: str = ""):
        self.db_path = db_path
        self.instance_id = instance_id or uuid.uuid4().hex[:12]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        #: Set under the lock by `close`, so a write that arrives afterwards is
        #: a no-op rather than a use of a freed connection. See `close`.
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
                f"runtime.db schema v{cur} is newer than this build supports "
                f"(v{SCHEMA_VERSION}); upgrade ragtools or remove the file."
            )
        # Fresh or older -> stamp. Future versions add migration steps here.
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        """Close under the SAME lock every other method takes.

        It did not, and that is a use-after-free rather than a tidiness point.
        Twelve methods hold ``self._lock`` across their ``self.conn`` calls;
        this one held nothing, so closing could free the sqlite3 connection
        underneath a C-level ``execute`` running in another thread.

        Observed on a Linux CI runner as ``Fatal Python error: Segmentation
        fault``, exit 139: the watcher thread was inside ``_emit`` (it logs
        activity continuously, and it is a daemon thread that legitimately
        outlives the request that started it) while the lifespan was tearing
        the service down.

        ``_closed`` is the other half. The watcher does not stop just because
        we decided to shut down, so a write arriving after the close must be a
        no-op — losing a shutdown activity line is the right trade against
        raising in a thread nobody is waiting on, and a far better one than
        crashing the interpreter.

        THAT REASONING WAS APPLIED TO ``_emit`` AND NOWHERE ELSE, and the job
        worker is the other thread that legitimately outlives a shutdown. It
        calls ``is_cancel_requested`` on every progress tick and ``finish``
        exactly once, and both went straight at ``self.conn``::

            sqlite3.ProgrammingError: Cannot operate on a closed database.
              at is_cancel_requested   <- the running job
              at finish                <- trying to record that it had failed

        The second one escapes ``JobWorker._run``, kills the thread, and leaves
        its traceback holding every frame beneath it — including the
        ``IndexState`` the indexer had open. On Windows that pins ``state.db``
        until the cyclic collector runs. It failed a release build; see
        `tests/test_runtime_store_shutdown.py`.

        So EVERY method now answers rather than raising, and each answer is
        chosen for what its caller does with it:

        * ``is_cancel_requested`` -> **True**. A closed store means the service
          is going away, so "should this job stop?" is honestly yes, and a
          cooperative handler unwinds through the ``Cancelled`` path it already
          has instead of crashing on the next call.
        * ``claim_next`` -> ``None``. Nothing new starts.
        * ``finish`` -> ``None``, **logged at ERROR** naming the job, the
          terminal state and the error it was carrying. A terminal state that
          could not be recorded is not something to swallow; it is something to
          say out loud. (The next boot's ``recover_interrupted`` is what makes
          it good again.)
        * writes -> no-ops; reads -> empty.
        * ``submit`` -> raises :class:`StoreClosed`; see there.
        """
        with self._lock:
            self._closed = True
            try:
                self.conn.close()
            except Exception:
                pass

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def __enter__(self) -> "RuntimeStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- jobs -----------------------------------------------------------

    def submit(self, kind: str, scope: Optional[dict] = None, *,
               idempotency_key: Optional[str] = None) -> Job:
        """Queue a job. With an ``idempotency_key`` matching an ACTIVE job, that
        job is returned instead of starting a second one."""
        with self._lock:
            if self._closed:
                raise StoreClosed(
                    f"the runtime store is closed; job {kind!r} was not queued")
            if idempotency_key:
                row = self.conn.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ? AND state IN (?,?)",
                    (idempotency_key, JobState.QUEUED, JobState.RUNNING),
                ).fetchone()
                if row:
                    return self._job(row)

            job = Job(id=str(uuid.uuid4()), kind=kind, scope=dict(scope or {}),
                      state=JobState.QUEUED, idempotency_key=idempotency_key,
                      created_at=_now(), instance_id=self.instance_id)
            self.conn.execute(
                "INSERT INTO jobs (id, kind, scope_json, state, idempotency_key, "
                "created_at, instance_id) VALUES (?,?,?,?,?,?,?)",
                (job.id, job.kind, json.dumps(job.scope), job.state,
                 job.idempotency_key, job.created_at, job.instance_id),
            )
            self.conn.commit()
            self._emit("job.submitted", "service",
                       {"job_id": job.id, "kind": kind, "scope": job.scope})
            return job

    def claim_next(self) -> Optional[Job]:
        """Atomically take the oldest runnable queued job and mark it running.

        A job cancelled while still QUEUED is finalised here rather than being
        claimed: the worker only checks cancellation *inside* a handler, so
        without this a job cancelled before it started would later be picked up
        and run to completion anyway. (Found in live testing.)
        """
        while True:
            with self._lock:
                if self._closed:
                    return None
                row = self.conn.execute(
                    "SELECT * FROM jobs WHERE state = ? ORDER BY created_at, rowid LIMIT 1",
                    (JobState.QUEUED,),
                ).fetchone()
                if not row:
                    return None
                job_id = row["id"]
                if row["cancel_requested"]:
                    cancelled = True
                else:
                    cancelled = False
                    self.conn.execute(
                        "UPDATE jobs SET state = ?, started_at = ?, instance_id = ? WHERE id = ?",
                        (JobState.RUNNING, _now(), self.instance_id, job_id),
                    )
                    self.conn.commit()

            if cancelled:
                self.finish(job_id, JobState.CANCELLED,
                            result={"note": "cancelled before it started"})
                continue          # try the next queued job

            job = self.get_job(job_id)
            if job is None:       # deleted between the update and the read
                continue
            self._emit("job.started", "service", {"job_id": job.id, "kind": job.kind,
                                                  "scope": job.scope})
            return job

    def update_progress(self, job_id: str, *, done: int, total: Optional[int] = None,
                        phase: Optional[str] = None, emit: bool = True) -> None:
        """Record progress. Callers must THROTTLE — never one write per item."""
        with self._lock:
            if self._closed:
                return
            self.conn.execute(
                "UPDATE jobs SET progress_done = ?, progress_total = COALESCE(?, progress_total), "
                "phase = COALESCE(?, phase) WHERE id = ?",
                (done, total, phase, job_id),
            )
            self.conn.commit()
        if emit:
            self._emit("job.progress", "indexer",
                       {"job_id": job_id, "done": done, "total": total, "phase": phase})

    def finish(self, job_id: str, state: str, *, result: Optional[dict] = None,
               error: Optional[str] = None, verified: Optional[bool] = None) -> Optional[Job]:
        """Move a job to a terminal state and emit `job.completed`.

        ``None`` when the store is closed — the job DID end, but nothing could
        be written down. That is reported here, at ERROR, because this is the
        only place that knows both the outcome and that it was lost. See
        :meth:`close`.
        """
        if state not in JobState.TERMINAL:
            raise ValueError(f"{state!r} is not a terminal state")
        with self._lock:
            if self._closed:
                logger.error(
                    "job %s ended as %s but the runtime store is closed, so the "
                    "outcome was not recorded%s. It will be reported as "
                    "'interrupted' on the next start.",
                    job_id, state, f" (error: {error})" if error else "")
                return None
            self.conn.execute(
                "UPDATE jobs SET state = ?, finished_at = ?, result_json = ?, error = ?, "
                "verified = ? WHERE id = ?",
                (state, _now(), json.dumps(result) if result is not None else None,
                 error, None if verified is None else int(verified), job_id),
            )
            self.conn.commit()
            job = self.get_job(job_id)
        self._emit("job.completed", "service",
                   {"job_id": job_id, "kind": job.kind if job else None, "state": state,
                    "scope": job.scope if job else {}, "error": error, "verified": verified})
        return job

    def request_cancel(self, job_id: str) -> None:
        with self._lock:
            if self._closed:
                return
            self.conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
            self.conn.commit()
        self._emit("job.cancel_requested", "service", {"job_id": job_id})

    def is_cancel_requested(self, job_id: str) -> bool:
        """Should the job stop? A CLOSED store answers **yes**.

        This is the call a running handler makes most often, and the one that
        crashed the worker when the store closed underneath it. Answering "yes"
        turns a shutdown into the cancellation path the handler already
        implements, so it unwinds instead of dying — see :meth:`close`.
        """
        # NOTE: every connection access — reads included — is serialised. The
        # worker thread and request threads share one connection; unguarded
        # concurrent reads surface as `sqlite3.InterfaceError: bad parameter or
        # other API misuse`, which is how this was found.
        with self._lock:
            if self._closed:
                return True
            row = self.conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            if self._closed:
                return None
            row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, *, limit: int = 100, kind: Optional[str] = None) -> list:
        sql = "SELECT * FROM jobs"
        params: tuple = ()
        if kind:
            sql += " WHERE kind = ?"
            params = (kind,)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        with self._lock:
            if self._closed:
                return []
            rows = self.conn.execute(sql, params + (limit,)).fetchall()
        return [self._job(r) for r in rows]

    def active_jobs(self) -> list:
        """Queued or running — what the UI reconciles against on load."""
        with self._lock:
            if self._closed:
                return []
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE state IN (?,?) ORDER BY created_at",
                (JobState.QUEUED, JobState.RUNNING),
            ).fetchall()
        return [self._job(r) for r in rows]

    def recover_interrupted(self) -> list:
        """Mark jobs left active by a PREVIOUS instance as ``interrupted``.

        Called once at service boot. This is what makes "the window was closed
        during a background operation" an answerable question.
        """
        with self._lock:
            if self._closed:
                return []
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE state IN (?,?) AND (instance_id IS NULL OR instance_id != ?)",
                (JobState.QUEUED, JobState.RUNNING, self.instance_id),
            ).fetchall()
            jobs = [self._job(r) for r in rows]
            for j in jobs:
                self.conn.execute(
                    "UPDATE jobs SET state = ?, finished_at = ?, error = ? WHERE id = ?",
                    (JobState.INTERRUPTED, _now(),
                     "service restarted while this job was active", j.id),
                )
            self.conn.commit()
        for j in jobs:
            self._emit("job.completed", "service",
                       {"job_id": j.id, "kind": j.kind, "state": JobState.INTERRUPTED,
                        "scope": j.scope})
        return jobs

    # -- events ---------------------------------------------------------

    def append_event(self, type_: str, source: str, payload: Optional[dict] = None) -> Event:
        return self._emit(type_, source, payload or {})

    def _emit(self, type_: str, source: str, payload: dict) -> Event:
        with self._lock:
            ts = _now()
            if self._closed:
                # The store is gone; hand back a detached event so callers that
                # read the result still work. This is the path the watcher takes
                # while the service shuts down underneath it.
                return Event(id=0, ts=ts, type=type_, source=source,
                             payload=payload)
            cur = self.conn.execute(
                "INSERT INTO events (ts, type, source, payload_json) VALUES (?,?,?,?)",
                (ts, type_, source, json.dumps(payload)),
            )
            self.conn.commit()
            return Event(id=cur.lastrowid, ts=ts, type=type_, source=source, payload=payload)

    def events_after(self, cursor: int = 0, *, limit: int = 200,
                     types: Optional[list] = None) -> list:
        """Events with ``id > cursor`` — the SSE ``Last-Event-ID`` contract."""
        sql = "SELECT * FROM events WHERE id > ?"
        params: tuple = (cursor,)
        if types:
            sql += " AND type IN (%s)" % ",".join("?" * len(types))
            params += tuple(types)
        sql += " ORDER BY id LIMIT ?"
        with self._lock:
            if self._closed:
                return []
            rows = self.conn.execute(sql, params + (limit,)).fetchall()
        return [self._event(r) for r in rows]

    def latest_event_id(self) -> int:
        with self._lock:
            if self._closed:
                return 0
            row = self.conn.execute("SELECT MAX(id) AS m FROM events").fetchone()
        return int(row["m"] or 0)

    def prune_events(self, *, keep: int = 5000) -> int:
        """Bound the log. Returns how many rows were removed."""
        with self._lock:
            if self._closed:
                return 0
            row = self.conn.execute(
                "SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?", (keep,)
            ).fetchone()
            if not row:
                return 0
            cur = self.conn.execute("DELETE FROM events WHERE id <= ?", (row["id"],))
            self.conn.commit()
            return cur.rowcount

    # -- mapping --------------------------------------------------------

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"], kind=row["kind"], scope=json.loads(row["scope_json"] or "{}"),
            state=row["state"], progress_done=row["progress_done"],
            progress_total=row["progress_total"], phase=row["phase"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
            verified=None if row["verified"] is None else bool(row["verified"]),
            idempotency_key=row["idempotency_key"],
            cancel_requested=bool(row["cancel_requested"]),
            created_at=row["created_at"], started_at=row["started_at"],
            finished_at=row["finished_at"], instance_id=row["instance_id"],
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> Event:
        return Event(id=row["id"], ts=row["ts"], type=row["type"], source=row["source"],
                     payload=json.loads(row["payload_json"] or "{}"))
