"""Watcher adapted to run as a daemon thread inside the service.

Supports two modes:
  - v2 (explicit projects): watches multiple project directories
  - v1 (legacy): watches single content_root directory

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

    _MAX_RETRIES = 5
    _BASE_BACKOFF = 5  # seconds

    def run(self):
        """Main watch loop with automatic restart on failure."""
        from ragtools.service.activity import log_activity
        retries = 0
        while not self._stop_event.is_set():
            try:
                self._run_multi_root(log_activity)
                break  # Clean exit (stop_event set or no projects)
            except Exception as e:
                retries += 1
                if self._stop_event.is_set():
                    break
                if retries > self._MAX_RETRIES:
                    logger.error("Watcher giving up after %d failures: %s", retries, e)
                    log_activity("error", "watcher", f"Watcher stopped after {retries} failures: {e}")
                    break
                backoff = self._BASE_BACKOFF * (2 ** (retries - 1))
                logger.warning("Watcher crashed (attempt %d/%d), restarting in %ds: %s",
                               retries, self._MAX_RETRIES, backoff, e)
                log_activity("warning", "watcher", f"Watcher crashed, restarting in {backoff}s (attempt {retries})")
                self._stop_event.wait(backoff)  # Sleep but respect stop signal

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

    def stop(self):
        """Signal the watcher to stop."""
        self._stop_event.set()
