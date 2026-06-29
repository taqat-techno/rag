"""Watcher adapted to run as a daemon thread inside the service.

Supports two modes:
  - v2 (explicit projects): watches multiple project directories
  - v1 (legacy): watches single content_root directory

Uses the QdrantOwner's shared client instead of creating its own.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from watchfiles import watch, Change

from ragtools.config import Settings
from ragtools.ignore import IgnoreRules, RAGIGNORE_FILENAME
from ragtools.service.owner import QdrantOwner

logger = logging.getLogger("ragtools.watcher")


def _deepest_matching_root(resolved: Path, roots) -> Path | None:
    """Return the DEEPEST (longest-path) root that is an ancestor of ``resolved``,
    or None if none match.

    Deepest-match so a code-mode child project nested inside a docs-only parent
    is attributed to the CHILD — mirroring the scanner's child-path ownership.
    A first-match would filter the child's code edits with the parent's
    docs-only decision and silently drop them (scan-vs-watch divergence)."""
    best = None
    for root in roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if best is None or len(str(root)) > len(str(best)):
            best = root
    return best


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

        # Observability state read by /api/watcher/status. Guarded by a
        # dedicated lock so the request thread can read snapshots without
        # waiting on the watcher's main loop. State is intentionally
        # tiny (4 immutable string/int values) so lock hold time is nil.
        self._state_lock = threading.Lock()
        self._last_started_at: str | None = None
        self._last_error: str | None = None
        self._last_error_at: str | None = None
        self._consecutive_failures: int = 0

    _MAX_RETRIES = 5
    _BASE_BACKOFF = 5  # seconds

    # ----------------------------------------------------------------------
    # Observability — small, side-effect-free helpers that the route reads.
    # ----------------------------------------------------------------------

    @staticmethod
    def _now_iso() -> str:
        """UTC ISO-8601 timestamp for the observability fields."""
        return datetime.now(timezone.utc).isoformat()

    def _record_started(self) -> None:
        """Mark a successful watch-loop entry. Resets the failure run.

        Called from inside ``_run_multi_root`` once the underlying
        ``watch()`` iterator is about to begin yielding changes. At that
        point the daemon has resolved every project path, built ignore
        rules, and is awaiting events — i.e. it is meaningfully 'up'.
        """
        with self._state_lock:
            self._last_started_at = self._now_iso()
            self._last_error = None
            self._last_error_at = None
            self._consecutive_failures = 0

    def _record_error(self, exc: BaseException) -> None:
        """Capture an exception from the retry/give-up path.

        ``BaseException`` (not ``Exception``) so that propagated control
        signals would also be traced if they ever reach this codepath —
        defensive only; the watcher's main loop catches plain Exceptions.
        """
        with self._state_lock:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._last_error_at = self._now_iso()
            self._consecutive_failures += 1

    def get_state_snapshot(self) -> dict:
        """Return a JSON-safe copy of the four observability fields.

        Cheap (one lock acquisition; copies four primitive values) so
        the HTTP route can call it on every request without contention.
        """
        with self._state_lock:
            return {
                "last_started_at": self._last_started_at,
                "last_error": self._last_error,
                "last_error_at": self._last_error_at,
                "consecutive_failures": self._consecutive_failures,
            }

    def run(self):
        """Main watch loop with automatic restart on failure."""
        from ragtools.service.activity import log_activity
        retries = 0
        while not self._stop_event.is_set():
            try:
                self._run_multi_root(log_activity)
                break  # Clean exit (stop_event set or no projects)
            except Exception as e:
                # Capture for /api/watcher/status before any other work so a
                # crash inside log_activity / _record_give_up does not
                # swallow the error trail.
                self._record_error(e)
                retries += 1
                if self._stop_event.is_set():
                    break
                if retries > self._MAX_RETRIES:
                    logger.error("Watcher giving up after %d failures: %s", retries, e)
                    log_activity("error", "watcher", f"Watcher stopped after {retries} failures: {e}")
                    # Persist a marker + fire a toast so the user finds out
                    # immediately instead of discovering stale search results
                    # hours later. Both ops are best-effort.
                    self._record_give_up(retries, e)
                    break
                backoff = self._BASE_BACKOFF * (2 ** (retries - 1))
                logger.warning("Watcher crashed (attempt %d/%d), restarting in %ds: %s",
                               retries, self._MAX_RETRIES, backoff, e)
                log_activity("warning", "watcher", f"Watcher crashed, restarting in {backoff}s (attempt {retries})")
                self._stop_event.wait(backoff)  # Sleep but respect stop signal

    def _record_give_up(self, retries: int, exc: Exception) -> None:
        """Persist a crash-banner marker and dispatch a desktop toast.

        Symmetric with the supervisor's ``_write_gave_up_marker``. Both
        operations are wrapped in try/except — this method runs from the
        watcher's error path and must never raise.
        """
        try:
            logs_dir = Path(self._settings.state_db).parent / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            marker = logs_dir / "watcher_gave_up.json"
            marker.write_text(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "retries": retries,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }, indent=2))
        except Exception as mark_err:
            logger.warning("Could not write watcher_gave_up marker: %s", mark_err)

        try:
            from ragtools.service.notify import notify_watcher_gave_up
            notify_watcher_gave_up(self._settings, error=str(exc), retries=retries)
        except Exception as notify_err:
            logger.warning("Could not send watcher toast: %s", notify_err)

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
        project_include: dict[Path, bool] = {}  # resolved_path → effective index_source_code
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
                secret_allowlist=self._settings.secret_allowlist,
            )
            project_map[resolved] = project.id
            project_include[resolved] = project.resolve_index_code(self._settings.index_source_code)
            watch_paths.append(project.path)

        if not watch_paths:
            logger.warning("No valid project paths to watch")
            log_activity("warning", "watcher", "No valid project paths to watch")
            return

        from ragtools.watcher.observer import is_indexable_change

        def _accept(path: str) -> bool:
            # Honor per-project index_source_code + secret exclusion + ignore rules.
            # Deepest-match so a nested child project's mode wins over its parent's
            # (consistent with the scanner's child-path ownership).
            resolved = Path(path).resolve()
            root = _deepest_matching_root(resolved, project_rules.keys())
            if root is None:
                return False
            return is_indexable_change(path, project_rules[root], root, project_include[root])

        def md_filter(change: Change, path: str) -> bool:
            if Path(path).name == RAGIGNORE_FILENAME:
                return True
            return _accept(path)

        logger.info("Watcher started: %d projects (debounce=%dms)", len(watch_paths), self._debounce_ms)
        log_activity("info", "watcher", f"Watcher started: {len(watch_paths)} projects")
        # /api/watcher/status snapshot: a successful start clears any
        # prior error and resets consecutive_failures to zero.
        self._record_started()

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

                md_changes = [(c, p) for c, p in changes if _accept(p)]
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
