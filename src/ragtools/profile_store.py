"""Persistent client-profile store (RAG v3, Stage S12).

The authorization decision is re-checked server-side on every call, so the
caller's :class:`~ragtools.profiles.ClientProfile` must live somewhere durable
and reload exactly — most importantly preserving ``allowed_projects = None``
(owner: ALL) as distinct from ``frozenset()`` (nothing), a distinction a naive
CSV/JSON round-trip destroys and which is the difference between "see
everything" and "see nothing".

SQLite (JSON in columns, not a JSON *file* — the state rule forbids JSON files,
not JSON values), with the S5 WAL hardening.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S12 -> G12)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from ragtools.profiles import ClientProfile


class ProfileStore:
    """SQLite-backed store for client profiles (server-side authz source)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS client_profiles (
                profile_id          TEXT PRIMARY KEY,
                allowed_projects    TEXT,           -- JSON array, or NULL = ALL (owner)
                capability_groups   TEXT NOT NULL,  -- JSON array
                tool_overrides      TEXT NOT NULL,  -- JSON object
                cross_project_policy TEXT NOT NULL,
                destructive_policy  TEXT NOT NULL,
                display_name        TEXT,
                client_type         TEXT
            )
            """
        )
        self._conn.commit()

    def add(self, profile: ClientProfile) -> None:
        """Insert or replace a profile."""
        allowed = (
            None if profile.allowed_projects is None
            else json.dumps(sorted(profile.allowed_projects))
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO client_profiles (profile_id, allowed_projects, "
            "capability_groups, tool_overrides, cross_project_policy, "
            "destructive_policy, display_name, client_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                profile.profile_id,
                allowed,
                json.dumps(sorted(profile.capability_groups)),
                json.dumps(profile.tool_overrides),
                profile.cross_project_policy,
                profile.destructive_policy,
                profile.display_name,
                profile.client_type,
            ),
        )
        self._conn.commit()

    def get(self, profile_id: str) -> Optional[ClientProfile]:
        row = self._conn.execute(
            "SELECT * FROM client_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        return self._profile(row) if row else None

    def list(self) -> list[ClientProfile]:
        rows = self._conn.execute(
            "SELECT * FROM client_profiles ORDER BY profile_id"
        ).fetchall()
        return [self._profile(r) for r in rows]

    def remove(self, profile_id: str) -> None:
        self._conn.execute(
            "DELETE FROM client_profiles WHERE profile_id = ?", (profile_id,)
        )
        self._conn.commit()

    def close(self) -> None:
        """Release the SQLite connection. Idempotent."""
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "ProfileStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @staticmethod
    def _profile(row: sqlite3.Row) -> ClientProfile:
        allowed_raw = row["allowed_projects"]
        allowed = None if allowed_raw is None else frozenset(json.loads(allowed_raw))
        return ClientProfile(
            profile_id=row["profile_id"],
            allowed_projects=allowed,
            capability_groups=frozenset(json.loads(row["capability_groups"])),
            tool_overrides=json.loads(row["tool_overrides"]),
            cross_project_policy=row["cross_project_policy"],
            destructive_policy=row["destructive_policy"],
            display_name=row["display_name"] or "",
            client_type=row["client_type"] or "",
        )
