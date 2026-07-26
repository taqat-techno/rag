"""S5 — operational state hardening + real migration ladder.

v2.7.0's state DB opened with no WAL and no busy timeout (concurrent readers
alongside the writer is exactly the access pattern), and its migration ladder
was an empty placeholder that only stamped the version forward. These pin WAL +
busy timeout and a REAL forward migration (v1 -> v2 adds a collection_id column,
the first step toward collection-aware keys) that actually runs on an old DB.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S5 -> G5)
"""

import sqlite3

import pytest

from ragtools.indexing.state import IndexState, StateSchemaError

_OLD_V1_DDL = (
    "CREATE TABLE file_state ("
    "file_path TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
    "file_hash TEXT NOT NULL, chunk_count INTEGER NOT NULL, "
    "last_indexed TEXT NOT NULL)"
)


def test_connection_uses_wal_and_busy_timeout(tmp_path):
    st = IndexState(str(tmp_path / "s.db"))
    assert st.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert st.conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000


def test_fresh_db_has_collection_id_column(tmp_path):
    st = IndexState(str(tmp_path / "s.db"))
    cols = [r[1] for r in st.conn.execute("PRAGMA table_info(file_state)")]
    assert "collection_id" in cols


def test_migrates_v1_db_forward_and_backfills(tmp_path):
    p = str(tmp_path / "old.db")
    conn = sqlite3.connect(p)
    conn.execute(_OLD_V1_DDL)
    conn.execute(
        "INSERT INTO file_state VALUES ('proj/a.md','proj','h',3,'2026-01-01')"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    st = IndexState(p)  # opens + migrates v1 -> current
    cols = [r[1] for r in st.conn.execute("PRAGMA table_info(file_state)")]
    assert "collection_id" in cols
    row = st.conn.execute(
        "SELECT collection_id FROM file_state WHERE file_path='proj/a.md'"
    ).fetchone()
    assert row[0] == "proj"  # backfilled from project_id
    assert st.conn.execute("PRAGMA user_version").fetchone()[0] >= 2


def test_refuses_a_newer_schema(tmp_path):
    p = str(tmp_path / "future.db")
    conn = sqlite3.connect(p)
    conn.execute(_OLD_V1_DDL)
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()
    with pytest.raises(StateSchemaError):
        IndexState(p)


def test_existing_operations_still_work_after_migration(tmp_path):
    # The migration must not break the existing hash-skip / update API.
    st = IndexState(str(tmp_path / "s.db"))
    st.update("proj/a.md", "proj", "hash1", 5)
    st.commit()
    assert st.file_changed("proj/a.md", "hash2") is True
    assert st.file_changed("proj/a.md", "hash1") is False
