"""Watcher adapted to run as a daemon thread inside the service.

Uses the QdrantOwner's shared client instead of creating its own.
"""

import logging
import threading
import time
from pathlib import Path

from watchfiles import watch, Change

from ragtools.config import Settings
from ragtools.ignore import IgnoreRules, RAGIGNORE_FILENAME
from ragtools.service.owner import QdrantOwner

logger = logging.getLogger("ragtools.watcher")


class WatcherThread(threading.Thread):
    """File watcher that runs as a daemon thread.

    Monitors content_root for .md file changes and triggers incremental
    indexing through the QdrantOwner (shared client, no lock contention).
    """

    def __init__(
        self,
        owner: QdrantOwner,
        settings: Settings,
        debounce_ms: int = 3000,
    ):
        super().__init__(daemon=True, name="rag-watcher")
        self._owner = owner
        self._settings = settings
        self._debounce_ms = debounce_ms
        self._stop_event = threading.Event()

    def run(self):
        """Main watch loop. Blocks until stop() is called or thread is killed."""
        content_root = self._settings.content_root
        root_path = Path(content_root).resolve()

        ignore_rules = IgnoreRules(
            content_root=root_path,
            global_patterns=self._settings.ignore_patterns,
            use_ragignore=self._settings.use_ragignore_files,
        )

        def md_filter(change: Change, path: str) -> bool:
            if Path(path).name == RAGIGNORE_FILENAME:
                return True
            if not path.endswith(".md"):
                return False
            return not ignore_rules.is_ignored(Path(path), root_path)

        logger.info("Watcher started: %s (debounce=%dms)", content_root, self._debounce_ms)

        try:
            for changes in watch(
                content_root,
                watch_filter=md_filter,
                debounce=self._debounce_ms,
                recursive=True,
                raise_interrupt=False,
                stop_event=self._stop_event,
            ):
                if self._stop_event.is_set():
                    break

                if not changes:
                    continue

                # Reload ignore rules if .ragignore changed
                ragignore_changed = any(
                    Path(p).name == RAGIGNORE_FILENAME for _, p in changes
                )
                if ragignore_changed:
                    ignore_rules.clear_cache()
                    logger.debug(".ragignore changed — ignore rules reloaded")

                # Filter to .md changes only
                md_changes = [(c, p) for c, p in changes if p.endswith(".md")]
                if not md_changes:
                    continue

                added = sum(1 for c, _ in md_changes if c == Change.added)
                modified = sum(1 for c, _ in md_changes if c == Change.modified)
                deleted = sum(1 for c, _ in md_changes if c == Change.deleted)
                logger.info("Changes detected: +%d ~%d -%d", added, modified, deleted)

                # Trigger incremental index through the owner (uses shared client)
                try:
                    stats = self._owner.run_incremental_index()
                    if stats["indexed"] > 0 or stats["deleted"] > 0:
                        logger.info("Indexed: %d, skipped: %d, deleted: %d",
                                    stats["indexed"], stats["skipped"], stats["deleted"])
                except Exception as e:
                    logger.error("Indexing error: %s", e)

        except Exception as e:
            if not self._stop_event.is_set():
                logger.error("Watcher error: %s", e)

        logger.info("Watcher stopped")

    def stop(self):
        """Signal the watcher to stop."""
        self._stop_event.set()
