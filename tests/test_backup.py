"""Tests for the state-DB backup subsystem.

Covers:
  - backup_state_db: creates dir + manifest; no-op when DB missing
  - Invalid trigger rejected
  - list_backups: newest-first ordering; ignores half-written dirs
  - prune_backups: keeps exactly N; deletes older; handles empty
  - restore_backup: copies DB over; writes pre-restore safety snapshot;
    raises on missing ID
  - Backup failure is swallowed (returns None) — callers depend on this
  - Manifest round-trips unknown fields safely (forward-compat)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

from ragtools.backup import (
    MANIFEST_FILENAME,
    STATE_DB_FILENAME,
    BackupManifest,
    backup_state_db,
    backups_root,
    list_backups,
    prune_backups,
    restore_backup,
)


@dataclass
class FakeSettings:
    """Minimal settings object for tests — only the attributes backup.py uses."""
    state_db: str
    backup_keep: int = 10


def _make_state_db(path: Path, rows: int = 3) -> None:
    """Create a plausible state DB with a `file_state` table + some rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("""
            CREATE TABLE file_state (
                file_path TEXT PRIMARY KEY,
                project_id TEXT,
                file_hash TEXT,
                mtime REAL
            )
        """)
        for i in range(rows):
            conn.execute(
                "INSERT INTO file_state VALUES (?, ?, ?, ?)",
                (f"proj{i}/file{i}.md", f"proj{i}", f"h{i}", float(i)),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# backup_state_db
# ---------------------------------------------------------------------------


def test_backup_creates_directory_with_db_and_manifest(tmp_path):
    db = tmp_path / "data" / "index_state.db"
    _make_state_db(db)
    settings = FakeSettings(state_db=str(db))

    target = backup_state_db(settings, trigger="rebuild")
    assert target is not None
    assert target.is_dir()
    assert (target / STATE_DB_FILENAME).is_file()
    assert (target / MANIFEST_FILENAME).is_file()

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert manifest["trigger"] == "rebuild"
    assert manifest["state_db_size"] > 0
    assert manifest["project_count"] == 3
    assert manifest["backup_id"] == target.name


def test_backup_no_op_when_db_missing(tmp_path):
    """Fresh install has no state DB yet — backup must silently skip."""
    db = tmp_path / "does_not_exist.db"
    settings = FakeSettings(state_db=str(db))

    target = backup_state_db(settings, trigger="rebuild")
    assert target is None
    # No backups directory should be created for a no-op.
    assert not (tmp_path / "backups").exists()


def test_backup_rejects_unknown_trigger(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db)
    settings = FakeSettings(state_db=str(db))

    with pytest.raises(ValueError, match="Unknown backup trigger"):
        backup_state_db(settings, trigger="nonsense")


def test_backup_with_note_persists_to_manifest(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db)
    settings = FakeSettings(state_db=str(db))

    target = backup_state_db(settings, trigger="manual", note="before big refactor")
    assert target is not None
    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert manifest["note"] == "before big refactor"


def test_backup_failure_returns_none_and_cleans_up(tmp_path, monkeypatch):
    """If writing the manifest fails, the backup dir must not leak into
    list_backups as a ghost entry."""
    db = tmp_path / "state.db"
    _make_state_db(db)
    settings = FakeSettings(state_db=str(db))

    # Force sqlite backup to fail by patching sqlite3.connect to raise.
    import ragtools.backup as backup_module
    orig_connect = sqlite3.connect

    def broken_connect(target, *args, **kwargs):
        # Let the read from state.db work, but fail on writing the dest DB.
        if str(db) in str(target):
            return orig_connect(target, *args, **kwargs)
        raise sqlite3.DatabaseError("simulated disk full")

    monkeypatch.setattr(backup_module.sqlite3, "connect", broken_connect)

    result = backup_state_db(settings, trigger="rebuild")
    assert result is None

    # No ghost directory lingering.
    assert list_backups(settings) == []


# ---------------------------------------------------------------------------
# list_backups
# ---------------------------------------------------------------------------


def test_list_backups_returns_newest_first(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db)
    settings = FakeSettings(state_db=str(db))

    # Create 3 backups with distinct timestamps.
    t0 = datetime(2026, 4, 18, 10, 0, 0, tzinfo=timezone.utc)
    backup_state_db(settings, trigger="rebuild", now=t0)
    backup_state_db(settings, trigger="manual", now=t0 + timedelta(seconds=1))
    backup_state_db(settings, trigger="project_remove", now=t0 + timedelta(seconds=2))

    results = list_backups(settings)
    triggers = [b.trigger for b in results]
    assert triggers == ["project_remove", "manual", "rebuild"]  # newest first


def test_list_backups_ignores_half_written_directories(tmp_path):
    """A directory with only an index_state.db but no manifest is treated
    as in-progress / corrupt and excluded."""
    db = tmp_path / "state.db"
    _make_state_db(db)
    settings = FakeSettings(state_db=str(db))

    # Create one good backup
    backup_state_db(settings, trigger="rebuild")

    # Manually create a broken directory
    ghost = backups_root(settings) / "20260101_000000_rebuild"
    ghost.mkdir(parents=True)
    (ghost / STATE_DB_FILENAME).write_bytes(b"truncated")
    # No manifest.json — this entry must NOT appear in list_backups.

    results = list_backups(settings)
    assert len(results) == 1


def test_list_backups_empty_when_root_missing(tmp_path):
    settings = FakeSettings(state_db=str(tmp_path / "nothing.db"))
    assert list_backups(settings) == []


# ---------------------------------------------------------------------------
# prune_backups
# ---------------------------------------------------------------------------


def test_prune_keeps_exactly_n_most_recent(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db)
    settings = FakeSettings(state_db=str(db), backup_keep=2)

    t0 = datetime(2026, 4, 18, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        backup_state_db(settings, trigger="manual", now=t0 + timedelta(seconds=i))

    assert len(list_backups(settings)) == 5
    deleted = prune_backups(settings)
    assert deleted == 3
    remaining = list_backups(settings)
    assert len(remaining) == 2
    # The two newest must be kept.
    assert remaining[0].timestamp > remaining[1].timestamp


def test_prune_no_op_when_under_limit(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db)
    settings = FakeSettings(state_db=str(db), backup_keep=10)

    backup_state_db(settings, trigger="manual")
    deleted = prune_backups(settings)
    assert deleted == 0
    assert len(list_backups(settings)) == 1


def test_prune_with_explicit_keep_override_trumps_settings(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db)
    settings = FakeSettings(state_db=str(db), backup_keep=100)

    t0 = datetime(2026, 4, 18, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(4):
        backup_state_db(settings, trigger="manual", now=t0 + timedelta(seconds=i))

    deleted = prune_backups(settings, keep=1)
    assert deleted == 3
    assert len(list_backups(settings)) == 1


def test_prune_with_keep_zero_deletes_all(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db)
    settings = FakeSettings(state_db=str(db))

    t0 = datetime(2026, 4, 18, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        backup_state_db(settings, trigger="manual", now=t0 + timedelta(seconds=i))

    deleted = prune_backups(settings, keep=0)
    assert deleted == 3
    assert list_backups(settings) == []


# ---------------------------------------------------------------------------
# restore_backup
# ---------------------------------------------------------------------------


def test_restore_replaces_current_db_and_takes_safety_snapshot(tmp_path):
    """Restoring must:
      1. take a pre_restore backup of the live DB
      2. overwrite the live DB with the backup contents
    """
    db = tmp_path / "state.db"
    _make_state_db(db, rows=3)
    settings = FakeSettings(state_db=str(db))

    # Backup #1 captures the 3-row state.
    orig_backup = backup_state_db(settings, trigger="manual")
    assert orig_backup is not None

    # Now mutate the live DB so it's different from the backup.
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM file_state")
    conn.commit()
    conn.close()

    # Restore — live DB should regain the original 3 rows; safety snapshot
    # of the emptied DB should be created.
    safety = restore_backup(settings, orig_backup.name)
    assert safety is not None
    assert safety.exists()
    # The safety snapshot must have the trigger pre_restore
    safety_manifest = json.loads((safety / MANIFEST_FILENAME).read_text())
    assert safety_manifest["trigger"] == "pre_restore"

    # And the live DB now matches the original 3-row state.
    conn = sqlite3.connect(db)
    cur = conn.execute("SELECT COUNT(*) FROM file_state")
    count = cur.fetchone()[0]
    conn.close()
    assert count == 3


def test_restore_raises_for_unknown_backup_id(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db)
    settings = FakeSettings(state_db=str(db))

    with pytest.raises(FileNotFoundError):
        restore_backup(settings, "20260101_000000_nonexistent")


# ---------------------------------------------------------------------------
# BackupManifest
# ---------------------------------------------------------------------------


def test_manifest_round_trips_through_json(tmp_path):
    m = BackupManifest(
        backup_id="20260418_013045_rebuild",
        timestamp="2026-04-18T01:30:45Z",
        trigger="rebuild",
        state_db_size=12345,
        source_path="/x/y.db",
        project_count=2,
        note="hi",
    )
    data = m.to_dict()
    round_tripped = BackupManifest.from_dict(data)
    assert round_tripped == m


def test_manifest_from_dict_ignores_unknown_keys():
    """Forward compatibility: a future version adding new fields must not
    break older CLIs reading its manifests."""
    data = {
        "backup_id": "x",
        "timestamp": "t",
        "trigger": "manual",
        "state_db_size": 0,
        "source_path": "p",
        "project_count": 0,
        "note": "",
        "future_field": "ignored",
    }
    m = BackupManifest.from_dict(data)
    assert m.backup_id == "x"
    assert not hasattr(m, "future_field")


# ---------------------------------------------------------------------------
# Concurrency — backup during writes
# ---------------------------------------------------------------------------


def test_backup_snapshot_is_consistent_under_concurrent_writes(tmp_path):
    """SQLite's online backup API must produce a readable, well-formed DB
    even if the source is being written to during the copy."""
    db = tmp_path / "state.db"
    _make_state_db(db, rows=5)
    settings = FakeSettings(state_db=str(db))

    # Open a writer connection, then take a backup, then write more.
    writer = sqlite3.connect(db, timeout=2.0)
    try:
        writer.execute("INSERT INTO file_state VALUES ('mid/x.md', 'mid', 'h', 99.0)")
        writer.commit()

        target = backup_state_db(settings, trigger="manual")
        assert target is not None

        # Verify the backup is readable and has the committed row.
        verify = sqlite3.connect(target / STATE_DB_FILENAME)
        try:
            cur = verify.execute("SELECT COUNT(*) FROM file_state")
            n = cur.fetchone()[0]
        finally:
            verify.close()
        # Original 5 + 1 we inserted before backup = 6
        assert n == 6
    finally:
        writer.close()
