"""Snapshot the SQLite state DB before destructive operations.

Motivation
----------
`rag rebuild` and project-remove both drop rows (and sometimes the whole
file) from `data/index_state.db`. A bug in those code paths — ours or a
future one — could lose the incremental-index state without warning.
Re-indexing from scratch is expensive on large knowledge bases, so a cheap
automatic snapshot is worth the disk cost.

Design
------
- Uses the SQLite Online Backup API (`sqlite3.Connection.backup()`), not a
  file copy, so WAL and journal files are handled correctly even if the
  indexer is actively writing.
- Each backup lives in its own timestamped directory under
  `{data_dir}/backups/` with a small `manifest.json` describing what and
  why. That makes the backups self-describing — `rag backup list` doesn't
  have to re-read every DB.
- Failure is non-fatal. Destructive operations MUST proceed even if the
  backup fails (e.g. disk full). We log + continue.
- Pure, testable. No imports of service modules — just sqlite, json, pathlib.

Layout
------
    data/backups/
      20260418_013045_rebuild/
        index_state.db        (full SQLite backup)
        manifest.json         ({timestamp, trigger, size, ...})
      20260418_015012_project_remove/
        ...
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("ragtools.backup")


MANIFEST_FILENAME = "manifest.json"
STATE_DB_FILENAME = "index_state.db"
VALID_TRIGGERS = {"rebuild", "project_remove", "manual", "pre_restore"}


@dataclass
class BackupManifest:
    """Metadata stored alongside each state-DB snapshot."""

    backup_id: str          # directory name, e.g. "20260418_013045_rebuild"
    timestamp: str          # ISO-8601 UTC
    trigger: str            # one of VALID_TRIGGERS
    state_db_size: int      # bytes
    source_path: str        # absolute path of the DB that was backed up
    project_count: int = 0  # optional, 0 if DB didn't expose it
    note: str = ""          # free-form, set by `rag backup create --note`

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BackupManifest":
        # Filter unknown keys so forward/backward-compatible reads don't blow up.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# Path helpers (pure)
# ---------------------------------------------------------------------------


def backups_root(settings) -> Path:
    """Root directory for all backups, derived from the state DB's parent."""
    state_db = Path(settings.state_db)
    return state_db.parent / "backups"


def _make_backup_id(trigger: str, now: Optional[datetime] = None) -> str:
    """Generate the directory name for a new backup."""
    if trigger not in VALID_TRIGGERS:
        raise ValueError(f"Unknown backup trigger: {trigger!r}")
    when = now or datetime.now(timezone.utc)
    stamp = when.strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{trigger}"


def _count_projects(db_path: Path) -> int:
    """Best-effort count of projects represented in the state DB. Returns 0
    on any error — this is metadata, not control flow."""
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            cur = conn.execute("SELECT COUNT(DISTINCT project_id) FROM file_state")
            row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


def backup_state_db(
    settings,
    trigger: str,
    note: str = "",
    now: Optional[datetime] = None,
) -> Optional[Path]:
    """Take a snapshot of `settings.state_db` into `backups/{id}/`.

    Args:
        settings: object with a `.state_db` attribute (path to the SQLite file).
        trigger: why the backup was taken; must be in VALID_TRIGGERS.
        note: optional free-form string stored in the manifest.
        now:  for tests; overrides the timestamp.

    Returns:
        Path to the backup directory on success, or None if the state DB
        did not exist or the backup failed. Never raises — destructive ops
        must not be gated on a successful snapshot.
    """
    state_db_path = Path(settings.state_db)
    if not state_db_path.exists():
        logger.info("Backup skipped: state DB does not exist yet (%s)", state_db_path)
        return None

    backup_id = _make_backup_id(trigger, now)
    target_dir = backups_root(settings) / backup_id
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target_db = target_dir / STATE_DB_FILENAME

        # Use SQLite's online backup API so WAL/journal are handled cleanly
        # and a concurrently-writing indexer can't corrupt the snapshot.
        src = sqlite3.connect(str(state_db_path), timeout=5.0)
        try:
            dst = sqlite3.connect(str(target_db))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        manifest = BackupManifest(
            backup_id=backup_id,
            timestamp=(now or datetime.now(timezone.utc)).isoformat(),
            trigger=trigger,
            state_db_size=target_db.stat().st_size,
            source_path=str(state_db_path.resolve()),
            project_count=_count_projects(target_db),
            note=note,
        )
        (target_dir / MANIFEST_FILENAME).write_text(
            json.dumps(manifest.to_dict(), indent=2)
        )
        logger.info("Backup created: %s (trigger=%s, size=%d)",
                    backup_id, trigger, manifest.state_db_size)
        return target_dir
    except Exception as e:
        # Never propagate — destructive callers depend on this being safe.
        logger.warning("Backup failed (trigger=%s): %s", trigger, e)
        # Clean up any half-written dir so list_backups doesn't see ghosts.
        try:
            if target_dir.exists() and not (target_dir / MANIFEST_FILENAME).exists():
                shutil.rmtree(target_dir, ignore_errors=True)
        except Exception:
            pass
        return None


def list_backups(settings) -> List[BackupManifest]:
    """Return all valid backups, newest first.

    A "valid" backup has both a `manifest.json` and an `index_state.db`
    inside its directory. Half-written directories are ignored silently.
    """
    root = backups_root(settings)
    if not root.exists():
        return []

    manifests: List[BackupManifest] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        manifest_path = entry / MANIFEST_FILENAME
        db_path = entry / STATE_DB_FILENAME
        if not manifest_path.is_file() or not db_path.is_file():
            continue
        try:
            data = json.loads(manifest_path.read_text())
            manifests.append(BackupManifest.from_dict(data))
        except Exception as e:
            logger.debug("Skipping unreadable backup %s: %s", entry.name, e)
            continue

    # The directory names start with YYYYMMDD_HHMMSS, so lexicographic sort
    # matches chronological order.
    manifests.sort(key=lambda m: m.backup_id, reverse=True)
    return manifests


def prune_backups(settings, keep: Optional[int] = None) -> int:
    """Delete oldest backups so only `keep` most recent remain.

    Args:
        keep: maximum number to retain. Defaults to `settings.backup_keep`
              (falling back to 10 if the attribute is missing).

    Returns:
        Number of backups deleted.
    """
    if keep is None:
        keep = getattr(settings, "backup_keep", 10)
    keep = max(0, int(keep))

    manifests = list_backups(settings)
    if len(manifests) <= keep:
        return 0

    to_delete = manifests[keep:]
    root = backups_root(settings)
    deleted = 0
    for m in to_delete:
        target = root / m.backup_id
        try:
            shutil.rmtree(target)
            deleted += 1
        except Exception as e:
            logger.warning("Could not delete old backup %s: %s", m.backup_id, e)
    if deleted:
        logger.info("Pruned %d old backup(s), kept %d", deleted, keep)
    return deleted


def restore_backup(settings, backup_id: str) -> Path:
    """Restore the state DB from a previous backup.

    A safety snapshot of the current DB is taken FIRST (trigger=pre_restore)
    so the restore itself is reversible.

    Args:
        backup_id: the directory name of the backup to restore from.

    Returns:
        Path to the pre-restore safety snapshot directory.

    Raises:
        FileNotFoundError: if `backup_id` does not point to a valid backup.
    """
    root = backups_root(settings)
    source_dir = root / backup_id
    source_db = source_dir / STATE_DB_FILENAME
    if not source_db.is_file():
        raise FileNotFoundError(f"Backup not found or incomplete: {backup_id}")

    # Safety first — snapshot the current state DB (if any) before overwriting.
    safety_dir = backup_state_db(settings, trigger="pre_restore")

    target_db = Path(settings.state_db)
    target_db.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace: copy to a sibling tempfile then os.replace.
    import os as _os
    import tempfile as _tempfile
    fd, tmp_path = _tempfile.mkstemp(
        prefix="state_restore_",
        suffix=".db",
        dir=str(target_db.parent),
    )
    _os.close(fd)
    try:
        shutil.copyfile(source_db, tmp_path)
        _os.replace(tmp_path, target_db)
    except Exception:
        # Clean up the temp file on error.
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass
        raise

    logger.info("Restored state DB from backup: %s", backup_id)
    return safety_dir if safety_dir else source_dir
