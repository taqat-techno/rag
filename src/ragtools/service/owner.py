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
    delete_file_points,
    index_file,
)
from ragtools.indexing.scanner import get_relative_path, scan_project
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
        self._ignore_rules = IgnoreRules(
            content_root=settings.content_root,
            global_patterns=settings.ignore_patterns,
            use_ragignore=settings.use_ragignore_files,
        )
        ensure_collection(self._client, settings.collection_name, self._encoder.dimension)
        logger.info("QdrantOwner initialized (model=%s, collection=%s)",
                     settings.embedding_model, settings.collection_name)

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
    ) -> dict:
        """Search and return both raw results and formatted context."""
        results = self.search(query, project_id, top_k)
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
        with self._lock:
            result = self._run_full_index_inner(project_id)
            self._invalidate_map_cache()
            return result

    def run_incremental_index(self, project_id: str | None = None) -> dict:
        """Incremental index — only new/changed/deleted. Thread-safe."""
        with self._lock:
            state = IndexState(self._settings.state_db)

            files = scan_project(
                self._settings.content_root,
                project_id=project_id,
                ignore_rules=self._ignore_rules,
            )
            current_paths = {
                get_relative_path(fp, self._settings.content_root) for _, fp in files
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
                relative_path = get_relative_path(file_path, self._settings.content_root)
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
            return stats

    def rebuild(self) -> dict:
        """Drop all data and rebuild from scratch. Thread-safe."""
        with self._lock:
            qdrant_path = Path(self._settings.qdrant_path)
            state_path = Path(self._settings.state_db)

            # Delete collection
            try:
                self._client.delete_collection(self._settings.collection_name)
            except Exception:
                pass

            # Recreate collection
            ensure_collection(self._client, self._settings.collection_name, self._encoder.dimension)

            # Delete state DB
            if state_path.exists():
                state_path.unlink()

            # Full index
            stats = self._run_full_index_inner()
            self._invalidate_map_cache()
            logger.info("Rebuild complete: %s", stats)
            return stats

    def _run_full_index_inner(self, project_id: str | None = None) -> dict:
        """Full index without acquiring lock (called from within locked context)."""
        state = IndexState(self._settings.state_db)

        files = scan_project(
            self._settings.content_root,
            project_id=project_id,
            ignore_rules=self._ignore_rules,
        )
        stats = {"files_indexed": 0, "chunks_indexed": 0, "projects": set()}

        for pid, file_path in files:
            relative_path = get_relative_path(file_path, self._settings.content_root)
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
