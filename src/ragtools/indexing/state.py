"""Persistent index state tracking using SQLite.

Tracks which files have been indexed, their content hashes, and chunk counts.
Used by the indexer to detect new, changed, unchanged, and deleted files.
"""

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path


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
        self.conn.commit()

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
