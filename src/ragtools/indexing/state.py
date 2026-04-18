"""Persistent index state tracking using SQLite.

Tracks which files have been indexed, their content hashes, and chunk counts.
Used by the indexer to detect new, changed, unchanged, and deleted files.

Schema is versioned via SQLite's built-in PRAGMA user_version so that future
releases can detect incompatible state DBs and migrate or refuse them safely
rather than silently corrupting user data.
"""

import hashlib
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("ragtools.indexing.state")

# Current expected schema version for this codebase.
# Bump this whenever the table structure changes and add a migration step
# in _migrate_schema() below.
SCHEMA_VERSION = 1


class StateSchemaError(RuntimeError):
    """Raised when the on-disk state DB has an incompatible schema version."""


class IndexState:
    """Tracks file indexing state in a local SQLite database."""

    def __init__(self, db_path: str = "data/index_state.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS file_state (
                file_path TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                chunk_count INTEGER NOT NULL,
                last_indexed TEXT NOT NULL
            )
        """)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_state_project_id ON file_state(project_id)"
        )
        self._migrate_schema()
        self.conn.commit()

    def _migrate_schema(self) -> None:
        """Check and migrate the SQLite schema version.

        Behavior:
          - Fresh DB (user_version=0): stamp it with SCHEMA_VERSION.
          - DB at current SCHEMA_VERSION: no-op.
          - DB at older SCHEMA_VERSION: run migrations up to SCHEMA_VERSION.
          - DB at newer SCHEMA_VERSION (downgrade): raise StateSchemaError
            so the installer/service can tell the user to either upgrade
            the app back or rebuild via `rag rebuild`.
        """
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]

        if current == 0:
            # Fresh DB or pre-versioning install: stamp current version.
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return

        if current == SCHEMA_VERSION:
            return

        if current > SCHEMA_VERSION:
            raise StateSchemaError(
                f"Index state DB at {self.db_path} is schema version {current}, "
                f"but this app only supports up to version {SCHEMA_VERSION}. "
                f"You appear to have downgraded RAG Tools. "
                f"Either upgrade the app back to the newer version, or run "
                f"`rag rebuild` to recreate the state DB from scratch."
            )

        # Future: per-version upward migration steps go here.
        # while current < SCHEMA_VERSION:
        #     current += 1
        #     _migrate_to_vN(self.conn, current)
        logger.warning(
            "Index state DB at %s is at schema version %d; migrating forward "
            "to %d.",
            self.db_path, current, SCHEMA_VERSION,
        )
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def get(self, file_path: str) -> dict | None:
        """Get the state record for a file, or None if not tracked."""
        row = self.conn.execute(
            "SELECT * FROM file_state WHERE file_path = ?", (file_path,)
        ).fetchone()
        return dict(row) if row else None

    def file_changed(self, file_path: str, current_hash: str) -> bool:
        """Check if a file has changed since last indexing.

        Returns True if the file is new or its hash differs from stored state.
        """
        record = self.get(file_path)
        return record is None or record["file_hash"] != current_hash

    def update(
        self,
        file_path: str,
        project_id: str,
        file_hash: str,
        chunk_count: int,
    ) -> None:
        """Insert or update a file's state record."""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO file_state
            (file_path, project_id, file_hash, chunk_count, last_indexed)
            VALUES (?, ?, ?, ?, ?)
            """,
            (file_path, project_id, file_hash, chunk_count, datetime.now().isoformat()),
        )
        self.conn.commit()

    def remove(self, file_path: str) -> None:
        """Remove a file's state record."""
        self.conn.execute("DELETE FROM file_state WHERE file_path = ?", (file_path,))
        self.conn.commit()

    def get_all_paths(self) -> set[str]:
        """Get all tracked file paths."""
        rows = self.conn.execute("SELECT file_path FROM file_state").fetchall()
        return {row["file_path"] for row in rows}

    def get_all_for_project(self, project_id: str) -> list[dict]:
        """Get all state records for a specific project."""
        rows = self.conn.execute(
            "SELECT * FROM file_state WHERE project_id = ?", (project_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_project_summary(self, project_id: str) -> dict:
        """Return file-count, chunk-count, last-indexed for one project."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS files, COALESCE(SUM(chunk_count), 0) AS chunks, "
            "MAX(last_indexed) AS last FROM file_state WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return {
            "files":        row["files"] if row else 0,
            "chunks":       row["chunks"] if row else 0,
            "last_indexed": row["last"] if row else None,
        }

    def get_top_files_by_chunks(self, project_id: str, limit: int = 10) -> list[dict]:
        """Return the ``limit`` files with the highest chunk counts in a project."""
        rows = self.conn.execute(
            "SELECT file_path, chunk_count FROM file_state "
            "WHERE project_id = ? ORDER BY chunk_count DESC LIMIT ?",
            (project_id, int(limit)),
        ).fetchall()
        return [{"file_path": r["file_path"], "chunks": r["chunk_count"]} for r in rows]

    def get_summary(self) -> dict:
        """Get a summary of the index state.

        Returns dict with total_files, total_chunks, projects, last_indexed.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) as files, COALESCE(SUM(chunk_count), 0) as chunks, "
            "MAX(last_indexed) as last FROM file_state"
        ).fetchone()
        projects = self.conn.execute(
            "SELECT DISTINCT project_id FROM file_state ORDER BY project_id"
        ).fetchall()
        return {
            "total_files": row["files"],
            "total_chunks": row["chunks"],
            "projects": [r["project_id"] for r in projects],
            "last_indexed": row["last"],
        }

    def commit(self) -> None:
        """Explicitly commit pending changes. Used by batch operations."""
        self.conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()

    @staticmethod
    def hash_file(file_path: Path) -> str:
        """Compute SHA256 hash of a file's contents."""
        return hashlib.sha256(file_path.read_bytes()).hexdigest()
