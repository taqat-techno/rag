"""QdrantOwner — sole owner of Qdrant client and Encoder.

All search and indexing operations go through this singleton.
Protected by threading.RLock to serialize Qdrant + encoder access.
"""

import logging
import shutil
import threading
from pathlib import Path

from qdrant_client import QdrantClient

from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.ignore import IgnoreRules
from ragtools.chunking.markdown import chunk_markdown_file
from ragtools.indexing.indexer import (
    ensure_collection,
    recreate_collection,
    delete_file_points,
    chunks_to_points,
    upsert_points,
    index_file,
)
from ragtools.indexing.scanner import (
    get_project_relative_path,
    scan_configured_projects,
)
from ragtools.indexing.state import IndexState
from ragtools.models import SearchResult
from ragtools.retrieval.formatter import format_context
from ragtools.retrieval.searcher import Searcher

# Batch size for windowed lock release during indexing
_INDEX_BATCH_SIZE = 30

# Qdrant local-mode scale thresholds. Qdrant's own guidance recommends
# against local mode above 20,000 points. We warn earlier (at 15,000) so
# users see the signal before it bites.
_QDRANT_LOCAL_SOFT_WARN = 15_000
_QDRANT_LOCAL_HARD_WARN = 20_000

logger = logging.getLogger("ragtools.service")


def compute_scale_warning(points_count: int) -> dict:
    """Return a structured scale-warning record for a given collection size.

    Levels:
      - ok        (< 15,000 points): no action required
      - approaching (15,000 - 19,999): user should start pruning or plan migration
      - over      (>= 20,000): past Qdrant's own local-mode recommendation

    The record is attached to /api/status so the admin panel and `rag doctor`
    can surface the signal. Pure function — no side effects, easy to unit-test.
    """
    if points_count >= _QDRANT_LOCAL_HARD_WARN:
        level = "over"
        message = (
            f"Collection has {points_count:,} points, which is above Qdrant's "
            f"recommended local-mode limit of {_QDRANT_LOCAL_HARD_WARN:,}. "
            "Search latency and memory use may degrade. Consider pruning the "
            "index or migrating Qdrant to server mode."
        )
    elif points_count >= _QDRANT_LOCAL_SOFT_WARN:
        level = "approaching"
        message = (
            f"Collection has {points_count:,} points, approaching the local-mode "
            f"limit of {_QDRANT_LOCAL_HARD_WARN:,}. Review ignore_patterns "
            "for large generated files and consider archiving completed projects."
        )
    else:
        level = "ok"
        message = ""

    return {
        "level": level,
        "points_count": points_count,
        "soft_limit": _QDRANT_LOCAL_SOFT_WARN,
        "hard_limit": _QDRANT_LOCAL_HARD_WARN,
        "message": message,
    }


def _log_scale_warning_once(points_count: int) -> None:
    """Log the scale warning at service/index-complete time.

    Kept separate from compute_scale_warning (pure) so the pure function
    remains test-friendly and this logging side-effect stays isolated.
    """
    record = compute_scale_warning(points_count)
    if record["level"] == "over":
        logger.warning(
            "[scale=over] %s", record["message"],
        )
    elif record["level"] == "approaching":
        logger.info(
            "[scale=approaching] %s", record["message"],
        )


class QdrantOwner:
    """Holds Qdrant client + Encoder, protected by RLock.

    Args:
        settings: Application settings.
        client: Optional pre-created client (for testing with in-memory).
    """

    def __init__(self, settings: Settings, client: QdrantClient | None = None):
        self._lock = threading.RLock()
        self._settings = settings
        self._client = client or settings.get_qdrant_client()
        self._encoder = Encoder(settings.embedding_model)

        # Global ignore rules (per-project merging happens at scan time)
        self._ignore_rules = IgnoreRules(
            content_root=".",
            global_patterns=settings.ignore_patterns,
            use_ragignore=settings.use_ragignore_files,
        )

        ensure_collection(self._client, settings.collection_name, self._encoder.dimension)
        logger.info("QdrantOwner initialized (model=%s, collection=%s)",
                     settings.embedding_model, settings.collection_name)
        from ragtools.service.activity import log_activity
        log_activity("info", "service", f"Engine initialized (model={settings.embedding_model})")

        # Validate configured project paths
        for p in settings.enabled_projects:
            if not Path(p.path).exists():
                logger.warning("Project '%s' path does not exist: %s", p.id, p.path)
                log_activity("warning", "config", f"Project '{p.id}' path missing: {p.path}")

    @property
    def client(self) -> QdrantClient:
        return self._client

    @property
    def encoder(self) -> Encoder:
        return self._encoder

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def ignore_rules(self) -> IgnoreRules:
        return self._ignore_rules

    def search(
        self,
        query: str,
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        top_k: int | None = None,
        score_threshold: float | None = None,
    ) -> list[SearchResult]:
        """Search the knowledge base. Thread-safe."""
        with self._lock:
            searcher = Searcher(
                client=self._client,
                encoder=self._encoder,
                settings=self._settings,
            )
            return searcher.search(
                query=query,
                project_id=project_id,
                project_ids=project_ids,
                top_k=top_k,
                score_threshold=score_threshold,
            )

    def search_formatted(
        self,
        query: str,
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        top_k: int | None = None,
        compact: bool = False,
    ) -> dict:
        """Search and return both raw results and formatted context."""
        results = self.search(query, project_id, project_ids=project_ids, top_k=top_k)
        if compact:
            from ragtools.retrieval.formatter import format_context_compact
            formatted = format_context_compact(results, query)
        else:
            formatted = format_context(results, query)
        return {
            "query": query,
            "count": len(results),
            "results": [
                {
                    "score": r.score,
                    "confidence": r.confidence,
                    "text": r.raw_text,
                    "file_path": r.file_path,
                    "project_id": r.project_id,
                    "headings": r.headings,
                }
                for r in results
            ],
            "formatted": formatted,
        }

    def get_map_points(self, force_recompute: bool = False) -> list[dict]:
        """Get 2D map coordinates for all indexed files. Uses cache when valid. Thread-safe."""
        with self._lock:
            from ragtools.service.map_data import (
                compute_map_points, load_cached_map, save_map_cache, invalidate_map_cache,
            )

            if not force_recompute:
                cached = load_cached_map(self._settings.state_db)
                if cached is not None:
                    return cached

            points = compute_map_points(self._client, self._settings)
            save_map_cache(self._settings.state_db, points)
            return points

    def run_full_index(self, project_id: str | None = None) -> dict:
        """Full index — re-index everything.

        Uses split-lock strategy like run_incremental_index:
        scan/hash/chunk outside lock, encode/upsert inside lock in batches.
        """
        from ragtools.service.activity import log_activity
        log_activity("info", "indexer", "Full index started")

        # --- Phase 1: outside lock — scan and chunk all files ---
        files = self._scan_files(project_id)
        pending = []  # (pid, relative_path, file_hash, chunks)

        for pid, file_path in files:
            relative_path = self._resolve_relative_path(pid, file_path)
            file_hash = IndexState.hash_file(file_path)
            chunks = chunk_markdown_file(
                file_path=file_path,
                project_id=pid,
                relative_path=relative_path,
                chunk_size=self._settings.chunk_size,
                chunk_overlap=self._settings.chunk_overlap,
            )
            if chunks:
                pending.append((pid, relative_path, file_hash, chunks))

        # --- Phase 2: inside lock — encode, upsert, update state in batches ---
        stats = {"files_indexed": 0, "chunks_indexed": 0, "projects": set()}

        for i in range(0, max(len(pending), 1), _INDEX_BATCH_SIZE):
            batch = pending[i : i + _INDEX_BATCH_SIZE]
            if not batch:
                break
            with self._lock:
                state = IndexState(self._settings.state_db)

                all_chunks = []
                chunk_file_map = []
                for pid, relative_path, file_hash, chunks in batch:
                    start = len(all_chunks)
                    all_chunks.extend(chunks)
                    chunk_file_map.append((start, len(chunks), pid, relative_path, file_hash))

                all_texts = [c.text for c in all_chunks]
                all_embeddings = self._encoder.encode_batch(all_texts)

                for start, count, pid, relative_path, file_hash in chunk_file_map:
                    file_chunks = all_chunks[start : start + count]
                    file_embeddings = all_embeddings[start : start + count]
                    points = chunks_to_points(file_chunks, file_embeddings, file_hash)
                    upsert_points(self._client, self._settings.collection_name, points)
                    state.update(relative_path, pid, file_hash, count)
                    stats["files_indexed"] += 1
                    stats["chunks_indexed"] += count
                    stats["projects"].add(pid)

                state.commit()
                state.close()

        with self._lock:
            self._invalidate_map_cache()

        stats["projects"] = sorted(stats["projects"])
        logger.info("Full index: %d files, %d chunks", stats["files_indexed"], stats["chunks_indexed"])
        log_activity("success", "indexer",
                     f"Full index: {stats['files_indexed']} files, {stats['chunks_indexed']} chunks")
        # Surface a scale warning into logs (and via /api/status) if applicable.
        self._emit_scale_warning_after_index(log_activity)
        return stats

    def run_incremental_index(self, project_id: str | None = None) -> dict:
        """Incremental index — only new/changed/deleted.

        Uses split-lock strategy: scan/hash/chunk outside lock (I/O only),
        then encode/upsert/state-update inside lock in batches, releasing
        between batches so search requests aren't blocked for minutes.
        """
        # --- Phase 1: outside lock — scan, hash, chunk, detect changes ---
        files = self._scan_files(project_id)

        # Open a read-only state connection for change detection
        read_state = IndexState(self._settings.state_db)
        current_paths = set()
        pending = []  # (pid, relative_path, file_hash, chunks)

        tracked_paths = read_state.get_all_paths()
        if project_id:
            project_records = read_state.get_all_for_project(project_id)
            tracked_paths = {r["file_path"] for r in project_records}

        stats = {"indexed": 0, "skipped": 0, "deleted": 0, "chunks_indexed": 0, "projects": set()}

        for pid, file_path in files:
            relative_path = self._resolve_relative_path(pid, file_path)
            current_paths.add(relative_path)
            current_hash = IndexState.hash_file(file_path)

            if not read_state.file_changed(relative_path, current_hash):
                stats["skipped"] += 1
                continue

            # Chunk the file (pure I/O, no shared resources)
            chunks = chunk_markdown_file(
                file_path=file_path,
                project_id=pid,
                relative_path=relative_path,
                chunk_size=self._settings.chunk_size,
                chunk_overlap=self._settings.chunk_overlap,
            )
            if chunks:
                pending.append((pid, relative_path, current_hash, chunks))
            else:
                stats["skipped"] += 1

        deleted_paths = tracked_paths - current_paths
        read_state.close()

        # --- Phase 2: inside lock — delete, encode, upsert, update state ---
        # Process deletes in one locked batch
        if deleted_paths:
            with self._lock:
                state = IndexState(self._settings.state_db)
                for del_path in deleted_paths:
                    delete_file_points(self._client, self._settings.collection_name, del_path)
                    state.remove(del_path)
                    stats["deleted"] += 1
                state.commit()
                state.close()

        # Process inserts in windowed batches (release lock between batches)
        for i in range(0, len(pending), _INDEX_BATCH_SIZE):
            batch = pending[i : i + _INDEX_BATCH_SIZE]
            with self._lock:
                state = IndexState(self._settings.state_db)

                # Encode all chunks from this batch together
                all_chunks = []
                chunk_file_map = []  # (index_in_all, pid, relative_path, file_hash)
                for pid, relative_path, file_hash, chunks in batch:
                    start = len(all_chunks)
                    all_chunks.extend(chunks)
                    chunk_file_map.append((start, len(chunks), pid, relative_path, file_hash))

                all_texts = [c.text for c in all_chunks]
                all_embeddings = self._encoder.encode_batch(all_texts)

                # Distribute embeddings back to files, create points, upsert
                for start, count, pid, relative_path, file_hash in chunk_file_map:
                    file_chunks = all_chunks[start : start + count]
                    file_embeddings = all_embeddings[start : start + count]

                    delete_file_points(self._client, self._settings.collection_name, relative_path)
                    points = chunks_to_points(file_chunks, file_embeddings, file_hash)
                    upsert_points(self._client, self._settings.collection_name, points)
                    state.update(relative_path, pid, file_hash, count)

                    stats["indexed"] += 1
                    stats["chunks_indexed"] += count
                    stats["projects"].add(pid)

                state.commit()
                state.close()

        # Finalize
        with self._lock:
            self._invalidate_map_cache()

        stats["projects"] = sorted(stats["projects"])
        logger.info("Incremental index: %d indexed, %d skipped, %d deleted",
                    stats["indexed"], stats["skipped"], stats["deleted"])
        from ragtools.service.activity import log_activity
        log_activity("success", "indexer",
                     f"Incremental: {stats['indexed']} indexed, {stats['skipped']} skipped, {stats['deleted']} deleted")
        self._emit_scale_warning_after_index(log_activity)
        return stats

    def rebuild(self) -> dict:
        """Drop all data and rebuild from scratch. Thread-safe."""
        with self._lock:
            state_path = Path(self._settings.state_db)

            # Snapshot the state DB before we drop it. Best-effort — failures
            # here must not block the rebuild itself (disk full, etc.).
            try:
                from ragtools.backup import backup_state_db, prune_backups
                backup_state_db(self._settings, trigger="rebuild")
                prune_backups(self._settings)
            except Exception as e:
                logger.warning("Pre-rebuild backup failed (non-fatal): %s", e)

            # Force-drop and recreate collection (clean slate)
            recreate_collection(self._client, self._settings.collection_name, self._encoder.dimension)

            # Delete state DB
            if state_path.exists():
                state_path.unlink()

            # Full index
            from ragtools.service.activity import log_activity
            log_activity("info", "indexer", "Rebuild started — all data dropped")
            stats = self._run_full_index_inner()
            self._invalidate_map_cache()
            logger.info("Rebuild complete: %s", stats)
            return stats

    def _run_full_index_inner(self, project_id: str | None = None) -> dict:
        """Full index without acquiring lock (called from within locked context)."""
        state = IndexState(self._settings.state_db)

        files = self._scan_files(project_id)
        stats = {"files_indexed": 0, "chunks_indexed": 0, "projects": set()}

        for pid, file_path in files:
            relative_path = self._resolve_relative_path(pid, file_path)
            file_hash = IndexState.hash_file(file_path)
            count = index_file(
                client=self._client,
                encoder=self._encoder,
                collection_name=self._settings.collection_name,
                project_id=pid,
                file_path=file_path,
                relative_path=relative_path,
                chunk_size=self._settings.chunk_size,
                chunk_overlap=self._settings.chunk_overlap,
            )
            state.update(relative_path, pid, file_hash, count)
            stats["files_indexed"] += 1
            stats["chunks_indexed"] += count
            stats["projects"].add(pid)

        state.close()
        stats["projects"] = sorted(stats["projects"])
        logger.info("Full index: %d files, %d chunks", stats["files_indexed"], stats["chunks_indexed"])
        return stats

    def get_status(self) -> dict:
        """Get collection and index status. Thread-safe."""
        with self._lock:
            try:
                info = self._client.get_collection(self._settings.collection_name)
                points_count = info.points_count
            except Exception:
                points_count = 0

            state_path = Path(self._settings.state_db)
            if state_path.exists():
                state = IndexState(self._settings.state_db)
                summary = state.get_summary()
                state.close()
            else:
                summary = {"total_files": 0, "total_chunks": 0, "projects": [], "last_indexed": None}

            return {
                "points_count": points_count,
                "collection_name": self._settings.collection_name,
                "scale": compute_scale_warning(points_count),
                **summary,
            }

    def _emit_scale_warning_after_index(self, log_activity) -> None:
        """Check current collection size and surface a scale warning if needed.

        Called at the end of run_full_index / run_incremental_index so the
        signal appears in the activity log next to the index result, and
        again through the normal logger at WARNING level for service.log.
        Safe to call without the RLock held as callers already hold it.
        """
        try:
            info = self._client.get_collection(self._settings.collection_name)
            points_count = info.points_count
        except Exception:
            return

        record = compute_scale_warning(points_count)
        if record["level"] == "over":
            logger.warning("[scale=over] %s", record["message"])
            log_activity("warning", "indexer", record["message"])
            self._notify_scale_warning("over", record["message"])
        elif record["level"] == "approaching":
            logger.info("[scale=approaching] %s", record["message"])
            log_activity("info", "indexer", record["message"])
            self._notify_scale_warning("approaching", record["message"])

    def _notify_scale_warning(self, level: str, message: str) -> None:
        """Fire a desktop toast for the scale warning. Best-effort; never raises.

        Kept in a separate method so the shared notifier has a single
        import point and the 1-hour cooldown (defined in notify.py) has a
        deterministic call path to dedupe against.
        """
        try:
            from ragtools.service.notify import notify_scale_warning
            notify_scale_warning(self._settings, level=level, message=message)
        except Exception as e:
            logger.debug("scale-warning toast failed (non-fatal): %s", e)

    def get_projects(self) -> list[dict]:
        """Get indexed projects with counts. Thread-safe."""
        with self._lock:
            state_path = Path(self._settings.state_db)
            if not state_path.exists():
                return []

            state = IndexState(self._settings.state_db)
            summary = state.get_summary()
            projects = []
            for pid in summary["projects"]:
                records = state.get_all_for_project(pid)
                projects.append({
                    "project_id": pid,
                    "files": len(records),
                    "chunks": sum(r["chunk_count"] for r in records),
                })
            state.close()
            return projects

    def update_settings(self, **kwargs) -> None:
        """Hot-reload mutable settings in the running service. Thread-safe."""
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._settings, key):
                    object.__setattr__(self._settings, key, value)
            logger.info("Settings updated: %s", list(kwargs.keys()))
            from ragtools.service.activity import log_activity
            log_activity("info", "config", f"Settings updated: {', '.join(kwargs.keys())}")

    def update_projects(self, projects: list) -> None:
        """Hot-reload project configuration. Thread-safe."""
        with self._lock:
            object.__setattr__(self._settings, "projects", projects)
            object.__setattr__(self._settings, "config_version", 2)
            logger.info("Projects updated: %d configured", len(projects))
            from ragtools.service.activity import log_activity
            log_activity("info", "config", f"Projects reloaded: {len(projects)} configured")

    def reindex_project(self, project_id: str) -> dict:
        """Drop a project's chunks + state rows and re-index from scratch.

        Composes ``delete_project_data`` (which backs up the state DB first)
        with ``run_full_index(project_id=X)``. The delete step is atomic per
        project — other projects are untouched.
        """
        # The inner calls both grab ``self._lock``; we don't hold it here.
        deleted = self.delete_project_data(project_id)
        stats = self.run_full_index(project_id=project_id)
        return {
            "project_id": project_id,
            "deleted_files": deleted.get("files_deleted", 0),
            **stats,
        }

    def delete_project_data(self, project_id: str) -> dict:
        """Delete all indexed data for a project from Qdrant and state DB. Thread-safe."""
        with self._lock:
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            # Snapshot the state DB before wiping this project's rows.
            try:
                from ragtools.backup import backup_state_db, prune_backups
                backup_state_db(self._settings, trigger="project_remove",
                                note=f"project={project_id}")
                prune_backups(self._settings)
            except Exception as e:
                logger.warning("Pre-remove backup failed (non-fatal): %s", e)

            # Delete from Qdrant (all chunks with this project_id)
            try:
                self._client.delete(
                    collection_name=self._settings.collection_name,
                    points_selector=Filter(
                        must=[FieldCondition(key="project_id", match=MatchValue(value=project_id))]
                    ),
                )
            except Exception as e:
                logger.warning("Failed to delete Qdrant data for project %s: %s", project_id, e)

            # Delete from state DB
            deleted_files = 0
            state_path = Path(self._settings.state_db)
            if state_path.exists():
                state = IndexState(self._settings.state_db)
                records = state.get_all_for_project(project_id)
                for r in records:
                    state.remove(r["file_path"])
                    deleted_files += 1
                state.close()

            self._invalidate_map_cache()

            from ragtools.service.activity import log_activity
            log_activity("warning", "indexer", f"Project data deleted: {project_id} ({deleted_files} files)")
            logger.info("Deleted data for project %s: %d files", project_id, deleted_files)
            return {"project_id": project_id, "files_deleted": deleted_files}

    def _scan_files(self, project_id: str | None = None) -> list[tuple[str, Path]]:
        """Scan files from configured projects.

        Always passes ALL projects to the scanner so nested path scoping
        works correctly (parent excludes child project files). Filters
        results by project_id afterward if requested.
        """
        if project_id and not any(p.id == project_id for p in self._settings.enabled_projects):
            raise ValueError(f"Project '{project_id}' not found in configuration")

        # Pass all projects (including disabled) so scanner detects nested path overlaps
        all_files = scan_configured_projects(
            self._settings.projects,
            global_ignore_patterns=self._settings.ignore_patterns,
            use_ragignore=self._settings.use_ragignore_files,
        )

        if project_id:
            return [(pid, fp) for pid, fp in all_files if pid == project_id]
        return all_files

    def _resolve_relative_path(self, project_id: str, file_path: Path) -> str:
        """Compute the storage-relative path for a file."""
        project = next(
            (p for p in self._settings.projects if p.id == project_id), None
        )
        if project:
            return get_project_relative_path(file_path, project.path, project.id)
        return f"{project_id}/{file_path.name}"

    def _invalidate_map_cache(self) -> None:
        """Invalidate the Semantic Map cache. Called after index changes."""
        try:
            from ragtools.service.map_data import invalidate_map_cache
            invalidate_map_cache(self._settings.state_db)
        except Exception:
            pass  # Non-critical

    def close(self):
        """Close Qdrant client."""
        try:
            del self._client
        except Exception:
            pass
        logger.info("QdrantOwner closed")
