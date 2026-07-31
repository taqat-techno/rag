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
        registry.db           (per-project layout only; absent under `shared`)
        manifest.json         ({timestamp, trigger, size, ...})
      20260418_015012_project_remove/
        ...

Why registry.db is here (R06)
-----------------------------
Under `collection_strategy = "per_project"` the registry says which collection
holds each project's vectors, so losing it loses the index just as thoroughly as
losing the state DB — the collections survive, but nothing knows whose they are.

This is SUPPORTING, not the fix. A restored registry is only *correct* because
the registry fingerprint first NOTICES that the live mapping is not the one the
state DB was written against; without that, a wrong registry is
indistinguishable from a right one and the restore would be a coin flip nobody
knew they were tossing.

That fingerprint is a GLOBAL integrity signal and nothing else — it answers "is
this the same registry?", never "may this project's file hashes be trusted?".
Restoring a backup therefore has an exact success criterion rather than a
statistical one: `registry_integrity.verify_restored_mapping` compares the
restored `project_id -> collection_name` mapping against the recorded one, per
project, and names every entry it disagrees about.
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
#: Same name it has in the data dir (see collection_router.build_router), so a
#: backup directory can be read without a decoder ring.
REGISTRY_DB_FILENAME = "registry.db"
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
    #: Bytes of the captured registry.db; 0 means "not captured" — either a
    #: `shared`-layout install (which has no registry) or a failed copy. The
    #: snapshot is still valid without it, so this is a fact, not a status.
    registry_db_size: int = 0

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


def registry_db_path(settings) -> Optional[Path]:
    """Where this installation's registry lives, or None if it cannot be known.

    Mirrors ``collection_router.build_router``: ``<data_dir>/registry.db``. A
    settings object without ``data_dir`` (the CLI's minimal shims, tests) gets
    None rather than a guess — backing up the wrong file would be worse than
    backing up none.
    """
    data_dir = getattr(settings, "data_dir", None)
    if not data_dir:
        return None
    return Path(data_dir) / REGISTRY_DB_FILENAME


def _copy_sqlite(source: Path, target: Path) -> int:
    """Snapshot one SQLite file with the online backup API; return its size."""
    src = sqlite3.connect(str(source), timeout=5.0)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target.stat().st_size


def _backup_registry(settings, target_dir: Path) -> int:
    """Best-effort snapshot of registry.db beside the state DB.

    Deliberately non-fatal and separate from the state-DB copy: the registry is
    absent under the `shared` layout (the default), and a state-DB backup that
    failed because an optional companion file was missing would be a worse
    outcome than a state-DB backup without it.
    """
    source = registry_db_path(settings)
    if source is None or not source.is_file():
        return 0
    try:
        return _copy_sqlite(source, target_dir / REGISTRY_DB_FILENAME)
    except Exception as e:  # noqa: BLE001
        logger.warning("Registry not included in backup (%s): %s", source, e)
        try:
            (target_dir / REGISTRY_DB_FILENAME).unlink()
        except Exception:
            pass
        return 0


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
        state_db_size = _copy_sqlite(state_db_path, target_db)

        # And the registry, which under `per_project` is the only record of
        # which collection holds each project's vectors.
        registry_db_size = _backup_registry(settings, target_dir)

        manifest = BackupManifest(
            backup_id=backup_id,
            timestamp=(now or datetime.now(timezone.utc)).isoformat(),
            trigger=trigger,
            state_db_size=state_db_size,
            source_path=str(state_db_path.resolve()),
            project_count=_count_projects(target_db),
            note=note,
            registry_db_size=registry_db_size,
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


def _atomic_replace(source: Path, target: Path, prefix: str) -> None:
    """Copy `source` over `target` via a sibling tempfile + os.replace."""
    import os as _os
    import tempfile as _tempfile

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = _tempfile.mkstemp(prefix=prefix, suffix=".db",
                                     dir=str(target.parent))
    _os.close(fd)
    try:
        shutil.copyfile(source, tmp_path)
        _os.replace(tmp_path, target)
    except Exception:
        # Clean up the temp file on error.
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass
        raise


def restore_backup(settings, backup_id: str) -> Path:
    """Restore the state DB — and the registry, if the backup has one.

    A safety snapshot of the current DBs is taken FIRST (trigger=pre_restore)
    so the restore itself is reversible.

    The registry goes back BEFORE the state DB, deliberately. If it cannot be
    replaced (on Windows a running service holds the handle) the failure lands
    with nothing yet changed. The reverse order would leave a state DB
    describing a mapping that was never restored — and while R06's fingerprint
    would catch that and force a re-index rather than let it pass silently,
    "nothing happened" is a better place to fail than "half happened".

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

    # Safety first — snapshot the current DBs (if any) before overwriting.
    safety_dir = backup_state_db(settings, trigger="pre_restore")

    source_registry = source_dir / REGISTRY_DB_FILENAME
    target_registry = registry_db_path(settings)
    if source_registry.is_file() and target_registry is not None:
        _atomic_replace(source_registry, target_registry, "registry_restore_")
        logger.info("Restored registry from backup: %s", backup_id)

    _atomic_replace(source_db, Path(settings.state_db), "state_restore_")
    logger.info("Restored state DB from backup: %s", backup_id)
    return safety_dir if safety_dir else source_dir
