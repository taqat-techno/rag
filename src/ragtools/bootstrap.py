"""Bring the on-disk configuration up to the current schema, once, before use.

`migrate_config` has existed, been correct, and been fully tested since v3.0.0.
It has also never run: every reference to it outside this module is a test or a
script. So an installation upgraded to v3 kept a v2 configuration, the runtime
fell back to the v2 code defaults, and the v3 architecture — per-project
collections, managed storage, framework corpora — stayed unreachable behind a
file nobody rewrote. A clean v3 install landed in the same place, which is why
this is not only an upgrade defect.

**Where this runs matters more than what it does.** `QdrantOwner.__init__` opens
the store and *creates collections* while constructing itself, so a migration
that ran afterwards would already be looking at a store built to the previous
layout. The seam is therefore before `Settings()` is read by an owner, not
after.

**Only writers migrate.** The twelve production `Settings()` sites include read
paths — the searcher, `selfcheck`, MCP — and a read path that rewrites the
user's configuration as a side effect is a worse defect than the one being
fixed. It is safe to leave them out because migration is deliberately
behaviour-preserving (see `LAYOUT_FOR_EXISTING_INSTALL`): a reader looking at an
unmigrated file resolves exactly the same backend and layout it would have
resolved after migration.

**Failure is loud, not fatal.** A configuration that cannot be rewritten — a
read-only directory, a permission problem, another process holding the lock —
must not stop the service from starting. The service degrades to the values it
already resolved, says so on `/health` and in `rag doctor`, and tries again next
boot.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ragtools.bootstrap")

#: How long to wait for another process to finish migrating before giving up
#: and carrying on with the file as it stands.
LOCK_WAIT_SECONDS = 10.0

#: A lock older than this is treated as abandoned. Migration is a sub-second
#: operation; a lock surviving minutes means the holder died, and refusing to
#: start forever because of a stale file would be its own outage.
LOCK_STALE_SECONDS = 120.0


@dataclass
class BootstrapResult:
    """What bootstrap did, and what to tell the user if it could not."""

    #: True when the file on disk was rewritten.
    migrated: bool = False
    #: True when the file was already current.
    already_current: bool = False
    from_version: Optional[int] = None
    to_version: Optional[int] = None
    added_keys: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Set when migration was needed but could not be completed. The service
    #: still starts; this is what it reports.
    error: Optional[str] = None
    config_path: Optional[Path] = None

    @property
    def degraded(self) -> bool:
        return self.error is not None

    def describe(self) -> str:
        if self.error:
            return f"configuration could not be migrated: {self.error}"
        if self.migrated:
            detail = f"v{self.from_version} -> v{self.to_version}"
            if self.added_keys:
                detail += f" (added {', '.join(sorted(self.added_keys))})"
            return f"configuration migrated {detail}"
        return "configuration already current"


class _ConfigLock:
    """An exclusive-create lock file beside the configuration.

    Not merely defensive. The service, the tray, the MCP server and a CLI
    invocation can all start within the same second of a login, and each one is
    a candidate writer. Two processes rewriting one TOML file concurrently is
    how a configuration gets truncated, and the atomic write protects a single
    writer from being interrupted — not two writers from each other.
    """

    def __init__(self, target: Path):
        self.path = target.with_suffix(target.suffix + ".migrating")
        self._fd: Optional[int] = None

    def _stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False
        return age > LOCK_STALE_SECONDS

    def acquire(self, timeout: float = LOCK_WAIT_SECONDS) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                return True
            except FileExistsError:
                if self._stale():
                    logger.warning("removing a stale migration lock at %s", self.path)
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.1)
            except OSError as exc:
                logger.warning("could not create the migration lock: %s", exc)
                return False

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "_ConfigLock":
        self.acquired = self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        if getattr(self, "acquired", False):
            self.release()


def _load(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover — Python 3.10 fallback
        import tomli as tomllib  # type: ignore[no-redef]
    with open(path, "rb") as handle:
        return tomllib.load(handle)


#: Result of the first call in this process, so entry points can compose
#: without re-reading the file for every command.
_MEMO: Optional[BootstrapResult] = None


def ensure_config_current_once(*, allow_write: bool = True) -> BootstrapResult:
    """`ensure_config_current`, evaluated at most once per process.

    Entry points nest: `rag service start` runs the CLI seam and then the
    service seam in the same interpreter. Doing the work twice is harmless but
    pointless, and the memo keeps the reported result stable for whatever asks
    later — `/health` and `rag doctor` both surface it.
    """
    global _MEMO
    if _MEMO is None:
        _MEMO = ensure_config_current(allow_write=allow_write)
    return _MEMO


def last_result() -> Optional[BootstrapResult]:
    """What bootstrap did in this process, if it has run. For diagnostics."""
    return _MEMO


def ensure_config_current(*, allow_write: bool = True) -> BootstrapResult:
    """Migrate the configuration file to the current schema if it needs it.

    Safe to call repeatedly and from any entry point: `migrate_config` is pure
    and idempotent, so a config already at the current version produces no write
    at all. Never raises — every failure is reported on the result.
    """
    from ragtools.config import _find_config_path

    result = BootstrapResult()
    try:
        path = _find_config_path()
    except Exception as exc:  # noqa: BLE001
        result.error = f"could not locate the configuration: {exc}"
        return result

    if not path or not Path(path).is_file():
        # Nothing to migrate. A fresh install has no file until something
        # writes one, and every writer stamps the current version.
        result.already_current = True
        return result

    path = Path(path)
    result.config_path = path

    try:
        from ragtools.upgrade.migrate import migrate_config

        migration = migrate_config(_load(path))
    except Exception as exc:  # noqa: BLE001
        result.error = f"could not read or migrate {path}: {exc}"
        logger.warning("%s", result.error)
        return result

    result.from_version = migration.from_version
    result.to_version = migration.to_version
    result.added_keys = list(migration.added_keys)
    result.notes = list(migration.notes)

    if not migration.changed:
        result.already_current = True
        return result

    if not allow_write:
        result.notes.append("read-only caller; the file was left unchanged")
        return result

    with _ConfigLock(path) as lock:
        if not lock.acquired:
            # Another process is migrating the same file. Its result is as good
            # as ours would have been, so this is not an error worth degrading
            # over — the next read simply picks up whatever it wrote.
            result.notes.append("another process is migrating this configuration")
            return result
        try:
            # Re-read under the lock. Between the check above and here, the
            # holder we just waited on may have completed the same migration.
            recheck = migrate_config(_load(path))
            if not recheck.changed:
                result.already_current = True
                return result

            import io

            import tomli_w

            from ragtools.atomicio import atomic_write_bytes

            buffer = io.BytesIO()
            tomli_w.dump(recheck.document, buffer)
            atomic_write_bytes(path, buffer.getvalue(), backup=True)
        except Exception as exc:  # noqa: BLE001
            result.error = f"could not write {path}: {exc}"
            logger.warning("%s", result.error)
            return result

    result.migrated = True
    logger.info("%s", result.describe())
    for note in result.notes:
        logger.info("  %s", note)
    return result
