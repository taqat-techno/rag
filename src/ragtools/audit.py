"""Durable per-client audit log (RAG v3, Stage S16 §28).

The highest-value new observability signal is *behavioral*: which client was
denied a capability, and which tried to expand its scope. Today that is
unobservable by construction. This is the durable home for it — SQLite, not the
in-memory ring buffer a restart erases (§28.2) — recording enough to see a
client drifting from expectation, and nothing sensitive (no secret values, no
project content; details are tool names, profile ids, and requested scopes).

Ordering is by an autoincrement id, not the timestamp, so events recorded in the
same millisecond still read back in insertion order.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S16 §28 -> G16)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Event-type vocabulary — the two behavioral signals §28.1 calls out, plus a
# generic access record.
DENIED_CAPABILITY = "denied_capability"
FAILED_SCOPE = "failed_scope"


@dataclass
class AuditEvent:
    """One audit record (privacy-safe: names and scopes, never values)."""

    id: int
    ts: str
    event_type: str
    profile_id: str
    tool: Optional[str]
    detail: Optional[str]


class AuditLog:
    """SQLite-backed durable audit trail (survives restart)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT NOT NULL,
                event_type   TEXT NOT NULL,
                profile_id   TEXT NOT NULL,
                tool         TEXT,
                detail       TEXT
            )
            """
        )
        self._conn.commit()

    def record(
        self,
        event_type: str,
        *,
        profile_id: str,
        tool: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        """Append one event."""
        self._conn.execute(
            "INSERT INTO audit_events (ts, event_type, profile_id, tool, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), event_type, profile_id, tool, detail),
        )
        self._conn.commit()

    def record_denied_capability(self, *, profile_id: str, tool: str) -> None:
        """A client was refused a tool it may not use."""
        self.record(DENIED_CAPABILITY, profile_id=profile_id, tool=tool)

    def record_failed_scope(self, *, profile_id: str, requested) -> None:
        """A client's request resolved to no authorized scope (or tried to widen)."""
        self.record(FAILED_SCOPE, profile_id=profile_id, detail=repr(list(requested or [])))

    def recent(self, limit: int = 50, *, event_type: Optional[str] = None) -> list[AuditEvent]:
        """The most recent events, newest first, optionally filtered by type."""
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if event_type is not None:
            sql += " WHERE event_type = ?"
            params = (event_type,)
        sql += " ORDER BY id DESC LIMIT ?"
        params += (limit,)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            AuditEvent(
                id=r["id"], ts=r["ts"], event_type=r["event_type"],
                profile_id=r["profile_id"], tool=r["tool"], detail=r["detail"],
            )
            for r in rows
        ]
