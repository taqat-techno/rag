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
from ragtools.indexing.indexer import (
    ensure_collection,
    recreate_collection,
    delete_file_points,
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

logger = logging.getLogger("ragtools.service")


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
                top_k=top_k,
                score_threshold=score_threshold,
            )

    def search_formatted(
        self,
        query: str,
        project_id: str | None = None,
        top_k: int | None = None,
        compact: bool = False,
    ) -> dict:
        """Search and return both raw results and formatted context."""
        results = self.search(query, project_id, top_k)
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
        """Full index — re-index everything. Thread-safe."""
        from ragtools.service.activity import log_activity
        log_activity("info", "indexer", "Full index started")
        with self._lock:
            result = self._run_full_index_inner(project_id)
            self._invalidate_map_cache()
        log_activity("success", "indexer",
                     f"Full index: {result['files_indexed']} files, {result['chunks_indexed']} chunks")
        return result

    def run_incremental_index(self, project_id: str | None = None) -> dict:
        """Incremental index — only new/changed/deleted. Thread-safe."""
        with self._lock:
            state = IndexState(self._settings.state_db)

            files = self._scan_files(project_id)
            current_paths = {
                self._resolve_relative_path(pid, fp) for pid, fp in files
            }

            tracked_paths = state.get_all_paths()
            if project_id:
                project_records = state.get_all_for_project(project_id)
                tracked_paths = {r["file_path"] for r in project_records}

            deleted_paths = tracked_paths - current_paths
            stats = {"indexed": 0, "skipped": 0, "deleted": 0, "chunks_indexed": 0, "projects": set()}

            for del_path in deleted_paths:
                delete_file_points(self._client, self._settings.collection_name, del_path)
                state.remove(del_path)
                stats["deleted"] += 1

            for pid, file_path in files:
                relative_path = self._resolve_relative_path(pid, file_path)
                current_hash = IndexState.hash_file(file_path)

                if not state.file_changed(relative_path, current_hash):
                    stats["skipped"] += 1
                    continue

                delete_file_points(self._client, self._settings.collection_name, relative_path)
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
                state.update(relative_path, pid, current_hash, count)
                stats["indexed"] += 1
                stats["chunks_indexed"] += count
                stats["projects"].add(pid)

            stats["projects"] = sorted(stats["projects"])
            state.close()
            self._invalidate_map_cache()
            logger.info("Incremental index: %d indexed, %d skipped, %d deleted",
                        stats["indexed"], stats["skipped"], stats["deleted"])
            from ragtools.service.activity import log_activity
            log_activity("success", "indexer",
                         f"Incremental: {stats['indexed']} indexed, {stats['skipped']} skipped, {stats['deleted']} deleted")
            return stats

    def rebuild(self) -> dict:
        """Drop all data and rebuild from scratch. Thread-safe."""
        with self._lock:
            state_path = Path(self._settings.state_db)

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
                **summary,
            }

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

    def delete_project_data(self, project_id: str) -> dict:
        """Delete all indexed data for a project from Qdrant and state DB. Thread-safe."""
        with self._lock:
            from qdrant_client.models import Filter, FieldCondition, MatchValue

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
        """Scan files from configured projects."""
        projects = self._settings.enabled_projects
        if project_id:
            projects = [p for p in projects if p.id == project_id]
            if not projects:
                raise ValueError(f"Project '{project_id}' not found in configuration")
        return scan_configured_projects(
            projects,
            global_ignore_patterns=self._settings.ignore_patterns,
            use_ragignore=self._settings.use_ragignore_files,
        )

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
