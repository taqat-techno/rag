"""Watcher adapted to run as a daemon thread inside the service.

Supports two modes:
  - v2 (explicit projects): watches multiple project directories
  - v1 (legacy): watches single content_root directory

Uses the QdrantOwner's shared client instead of creating its own.
"""

import logging
import threading
from pathlib import Path

from watchfiles import watch, Change

from ragtools.config import Settings
from ragtools.ignore import IgnoreRules, RAGIGNORE_FILENAME
from ragtools.service.owner import QdrantOwner

logger = logging.getLogger("ragtools.watcher")


class WatcherThread(threading.Thread):
    """File watcher that runs as a daemon thread.

    Monitors project directories for .md file changes and triggers incremental
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
        from ragtools.service.activity import log_activity

        if self._settings.has_explicit_projects:
            self._run_multi_root(log_activity)
        else:
            self._run_single_root(log_activity)

    def _run_multi_root(self, log_activity):
        """Watch multiple explicit project directories (v2 mode)."""
        enabled = self._settings.enabled_projects
        if not enabled:
            logger.warning("No enabled projects to watch")
            log_activity("warning", "watcher", "No enabled projects to watch")
            return

        # Build per-project ignore rules
        project_rules: dict[Path, IgnoreRules] = {}
        project_map: dict[Path, str] = {}  # resolved_path → project_id
        watch_paths = []

        for project in enabled:
            p = Path(project.path)
            if not p.exists() or not p.is_dir():
                logger.warning("Project '%s' path unavailable: %s", project.id, project.path)
                log_activity("warning", "watcher", f"Project '{project.id}' path unavailable: {project.path}")
                continue

            resolved = p.resolve()
            combined = list(self._settings.ignore_patterns) + list(project.ignore_patterns)
            project_rules[resolved] = IgnoreRules(
                content_root=project.path,
                global_patterns=combined,
                use_ragignore=self._settings.use_ragignore_files,
            )
            project_map[resolved] = project.id
            watch_paths.append(project.path)

        if not watch_paths:
            logger.warning("No valid project paths to watch")
            log_activity("warning", "watcher", "No valid project paths to watch")
            return

        def md_filter(change: Change, path: str) -> bool:
            fp = Path(path)
            if fp.name == RAGIGNORE_FILENAME:
                return True
            if not path.endswith(".md"):
                return False
            # Find which project this file belongs to and apply its rules
            resolved = fp.resolve()
            for root, rules in project_rules.items():
                try:
                    resolved.relative_to(root)
                    return not rules.is_ignored(fp, root)
                except ValueError:
                    continue
            return False

        logger.info("Watcher started: %d projects (debounce=%dms)", len(watch_paths), self._debounce_ms)
        log_activity("info", "watcher", f"Watcher started: {len(watch_paths)} projects")

        try:
            for changes in watch(
                *watch_paths,
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
                ragignore_changed = any(Path(p).name == RAGIGNORE_FILENAME for _, p in changes)
                if ragignore_changed:
                    for rules in project_rules.values():
                        rules.clear_cache()
                    logger.debug(".ragignore changed — ignore rules reloaded")

                md_changes = [(c, p) for c, p in changes if p.endswith(".md")]
                if not md_changes:
                    continue

                # Determine affected projects
                affected = set()
                for _, changed_path in md_changes:
                    resolved = Path(changed_path).resolve()
                    for root, pid in project_map.items():
                        try:
                            resolved.relative_to(root)
                            affected.add(pid)
                            break
                        except ValueError:
                            continue

                added = sum(1 for c, _ in md_changes if c == Change.added)
                modified = sum(1 for c, _ in md_changes if c == Change.modified)
                deleted = sum(1 for c, _ in md_changes if c == Change.deleted)
                logger.info("Changes in %s: +%d ~%d -%d", affected, added, modified, deleted)
                log_activity("info", "watcher", f"Changes in {', '.join(affected)}: +{added} ~{modified} -{deleted}")

                # Index only affected projects
                for pid in affected:
                    try:
                        stats = self._owner.run_incremental_index(project_id=pid)
                        if stats["indexed"] > 0 or stats["deleted"] > 0:
                            logger.info("Project %s: indexed=%d, skipped=%d, deleted=%d",
                                        pid, stats["indexed"], stats["skipped"], stats["deleted"])
                    except Exception as e:
                        logger.error("Indexing error for project %s: %s", pid, e)
                        log_activity("error", "watcher", f"Indexing error ({pid}): {e}")

        except Exception as e:
            if not self._stop_event.is_set():
                logger.error("Watcher error: %s", e)
                log_activity("error", "watcher", f"Watcher error: {e}")

        logger.info("Watcher stopped")
        log_activity("info", "watcher", "Watcher stopped")

    def _run_single_root(self, log_activity):
        """Watch single content_root directory (v1 legacy mode)."""
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
        log_activity("info", "watcher", f"Watcher started: {content_root}")

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

                ragignore_changed = any(Path(p).name == RAGIGNORE_FILENAME for _, p in changes)
                if ragignore_changed:
                    ignore_rules.clear_cache()

                md_changes = [(c, p) for c, p in changes if p.endswith(".md")]
                if not md_changes:
                    continue

                added = sum(1 for c, _ in md_changes if c == Change.added)
                modified = sum(1 for c, _ in md_changes if c == Change.modified)
                deleted = sum(1 for c, _ in md_changes if c == Change.deleted)
                logger.info("Changes detected: +%d ~%d -%d", added, modified, deleted)
                log_activity("info", "watcher", f"Changes: +{added} ~{modified} -{deleted}")

                try:
                    stats = self._owner.run_incremental_index()
                    if stats["indexed"] > 0 or stats["deleted"] > 0:
                        logger.info("Indexed: %d, skipped: %d, deleted: %d",
                                    stats["indexed"], stats["skipped"], stats["deleted"])
                except Exception as e:
                    logger.error("Indexing error: %s", e)
                    log_activity("error", "watcher", f"Indexing error: {e}")

        except Exception as e:
            if not self._stop_event.is_set():
                logger.error("Watcher error: %s", e)
                log_activity("error", "watcher", f"Watcher error: {e}")

        logger.info("Watcher stopped")
        log_activity("info", "watcher", "Watcher stopped")

    def stop(self):
        """Signal the watcher to stop."""
        self._stop_event.set()
