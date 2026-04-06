"""HTTP API routes for the RAG service."""

import logging
import os
import signal
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ragtools.service.app import get_owner, get_settings, get_shutdown_event

logger = logging.getLogger("ragtools.service")

router = APIRouter()

# --- Watcher state ---
_watcher_thread = None
_watcher_lock = threading.Lock()


# --- Request/Response models ---

class IndexRequest(BaseModel):
    project: Optional[str] = None
    full: bool = False


class IgnoreTestRequest(BaseModel):
    path: str


# --- Health ---

@router.get("/health")
def health():
    """Readiness probe. Returns 200 when encoder loaded + Qdrant open."""
    try:
        owner = get_owner()
        return {"status": "ready", "collection": owner.settings.collection_name}
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Service not ready")


# --- Search ---

@router.get("/api/search")
def search(
    query: str = Query(..., description="Search query"),
    project: Optional[str] = Query(None, description="Filter by project"),
    top_k: int = Query(10, description="Max results"),
):
    """Search the knowledge base."""
    owner = get_owner()
    return owner.search_formatted(query=query, project_id=project, top_k=top_k)


# --- Indexing ---

@router.post("/api/index")
def index(req: IndexRequest):
    """Trigger indexing. Incremental by default."""
    owner = get_owner()
    if req.full:
        stats = owner.run_full_index(project_id=req.project)
    else:
        stats = owner.run_incremental_index(project_id=req.project)
    return {"stats": stats}


@router.post("/api/rebuild")
def rebuild():
    """Drop all data and rebuild index from scratch."""
    owner = get_owner()
    stats = owner.rebuild()
    return {"stats": stats}


# --- Status ---

@router.get("/api/status")
def status():
    """Get collection and index statistics."""
    owner = get_owner()
    return owner.get_status()


@router.get("/api/projects")
def projects():
    """List indexed projects with file/chunk counts."""
    owner = get_owner()
    return {"projects": owner.get_projects()}


# --- Config ---

@router.get("/api/config")
def config():
    """Return current settings."""
    settings = get_settings()
    return {
        "content_root": settings.content_root,
        "embedding_model": settings.embedding_model,
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "top_k": settings.top_k,
        "score_threshold": settings.score_threshold,
        "collection_name": settings.collection_name,
        "ignore_patterns": settings.ignore_patterns,
        "use_ragignore_files": settings.use_ragignore_files,
        "service_port": settings.service_port,
        "service_host": settings.service_host,
        "log_level": settings.log_level,
        "qdrant_path": settings.qdrant_path,
        "state_db": settings.state_db,
        "startup_enabled": settings.startup_enabled,
        "startup_delay": settings.startup_delay,
        "startup_watcher": settings.startup_watcher,
        "startup_open_browser": settings.startup_open_browser,
    }


class ConfigUpdateRequest(BaseModel):
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    content_root: Optional[str] = None
    top_k: Optional[int] = None
    score_threshold: Optional[float] = None
    service_port: Optional[int] = None
    log_level: Optional[str] = None


RESTART_FIELDS = {"service_port", "log_level"}
HOT_RELOAD_FIELDS = {"chunk_size", "chunk_overlap", "content_root", "top_k", "score_threshold"}
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


@router.put("/api/config")
def update_config(req: ConfigUpdateRequest):
    """Update configuration. Returns which fields changed and if restart is needed."""
    errors = []
    updates = {}

    if req.chunk_size is not None:
        if not (100 <= req.chunk_size <= 2000):
            errors.append("chunk_size must be 100-2000")
        else:
            updates["chunk_size"] = req.chunk_size

    if req.chunk_overlap is not None:
        max_overlap = (req.chunk_size or get_settings().chunk_size) - 1
        if not (0 <= req.chunk_overlap <= min(500, max_overlap)):
            errors.append(f"chunk_overlap must be 0-{min(500, max_overlap)}")
        else:
            updates["chunk_overlap"] = req.chunk_overlap

    if req.content_root is not None:
        if not req.content_root.strip():
            errors.append("content_root cannot be empty")
        else:
            updates["content_root"] = req.content_root.strip()

    if req.top_k is not None:
        if not (1 <= req.top_k <= 100):
            errors.append("top_k must be 1-100")
        else:
            updates["top_k"] = req.top_k

    if req.score_threshold is not None:
        if not (0.0 <= req.score_threshold <= 1.0):
            errors.append("score_threshold must be 0.0-1.0")
        else:
            updates["score_threshold"] = req.score_threshold

    if req.service_port is not None:
        if not (1024 <= req.service_port <= 65535):
            errors.append("service_port must be 1024-65535")
        else:
            updates["service_port"] = req.service_port

    if req.log_level is not None:
        if req.log_level.upper() not in VALID_LOG_LEVELS:
            errors.append(f"log_level must be one of {', '.join(VALID_LOG_LEVELS)}")
        else:
            updates["log_level"] = req.log_level.upper()

    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    if not updates:
        return {"updated": [], "restart_required": False}

    # Filter out values that haven't actually changed
    current = get_settings()
    actually_changed = {
        k: v for k, v in updates.items()
        if getattr(current, k, None) != v
    }
    if not actually_changed:
        return {"updated": [], "restart_required": False}
    updates = actually_changed

    # Save to TOML
    from ragtools.service.pages import _update_toml_config
    _update_toml_config(None, updates)

    # Hot-reload applicable fields
    hot = {k: v for k, v in updates.items() if k in HOT_RELOAD_FIELDS}
    if hot:
        owner = get_owner()
        owner.update_settings(**hot)

    restart_needed = bool(set(updates.keys()) & RESTART_FIELDS)
    return {
        "updated": list(updates.keys()),
        "restart_required": restart_needed,
    }


# --- Watcher ---

@router.post("/api/watcher/start")
def watcher_start():
    """Start the file watcher as a background thread."""
    global _watcher_thread

    with _watcher_lock:
        if _watcher_thread is not None and _watcher_thread.is_alive():
            return {"status": "already_running"}

        from ragtools.service.watcher_thread import WatcherThread
        owner = get_owner()
        settings = get_settings()
        _watcher_thread = WatcherThread(owner=owner, settings=settings)
        _watcher_thread.start()
        logger.info("Watcher started for %s", settings.content_root)
        return {"status": "started", "content_root": settings.content_root}


@router.post("/api/watcher/stop")
def watcher_stop():
    """Stop the file watcher."""
    global _watcher_thread

    with _watcher_lock:
        if _watcher_thread is None or not _watcher_thread.is_alive():
            return {"status": "not_running"}

        _watcher_thread.stop()
        _watcher_thread.join(timeout=5)
        _watcher_thread = None
        logger.info("Watcher stopped")
        return {"status": "stopped"}


@router.get("/api/watcher/status")
def watcher_status():
    """Check watcher state."""
    with _watcher_lock:
        if _watcher_thread is not None and _watcher_thread.is_alive():
            return {
                "running": True,
                "content_root": get_settings().content_root,
            }
        return {"running": False}


# --- Ignore Rules ---

@router.get("/api/ignore/rules")
def ignore_rules():
    """Get all active ignore patterns."""
    owner = get_owner()
    return owner.ignore_rules.get_all_patterns()


@router.post("/api/ignore/test")
def ignore_test(req: IgnoreTestRequest):
    """Test if a path would be ignored."""
    from pathlib import Path
    owner = get_owner()
    reason = owner.ignore_rules.get_reason(Path(req.path))
    return {
        "path": req.path,
        "ignored": reason is not None,
        "reason": reason,
    }


# --- Semantic Map ---

@router.get("/api/map/points")
def map_points(project: Optional[str] = Query(None, description="Filter by project")):
    """Get 2D coordinates for all indexed files."""
    owner = get_owner()
    points = owner.get_map_points()
    if project:
        points = [p for p in points if p["project_id"] == project]
    return {"points": points, "count": len(points)}


@router.post("/api/map/recompute")
def map_recompute():
    """Force recomputation of map coordinates."""
    owner = get_owner()
    points = owner.get_map_points(force_recompute=True)
    return {"status": "recomputed", "count": len(points)}


# --- Startup ---

@router.get("/api/startup/status")
def startup_status():
    """Get startup registration status."""
    from ragtools.service.startup import is_task_installed, get_task_info
    settings = get_settings()
    installed = is_task_installed()
    info = get_task_info() if installed else None
    return {
        "installed": installed,
        "delay": settings.startup_delay,
        "watcher": settings.startup_watcher,
        "open_browser": settings.startup_open_browser,
        "task_info": info,
    }


@router.post("/api/startup/install")
def startup_install():
    """Create the Windows scheduled task."""
    from ragtools.service.startup import install_task
    settings = get_settings()
    try:
        install_task(settings, delay_seconds=settings.startup_delay)
        return {"status": "installed", "delay": settings.startup_delay}
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/startup/uninstall")
def startup_uninstall():
    """Remove the Windows scheduled task."""
    from ragtools.service.startup import uninstall_task
    if uninstall_task():
        return {"status": "uninstalled"}
    raise HTTPException(status_code=500, detail="Failed to remove task")


# --- Activity Log ---

@router.get("/api/activity")
def get_activity(
    limit: int = Query(50, description="Max events"),
    after: int = Query(0, description="Return events after this ID"),
):
    """Get recent activity events for the UI log."""
    from ragtools.service.activity import activity_log
    events = activity_log.get_recent(limit=limit, after_id=after)
    return {"events": [e.to_dict() for e in events], "count": len(events)}


# --- Shutdown ---

@router.post("/api/shutdown")
def shutdown():
    """Graceful shutdown. Stops watcher, then signals uvicorn to exit."""
    logger.info("Shutdown requested via API")
    from ragtools.service.activity import log_activity
    log_activity("warning", "service", "Shutdown requested")

    # Stop watcher if running
    watcher_stop()

    # Signal shutdown
    event = get_shutdown_event()
    event.set()

    # Send SIGINT to self to trigger uvicorn shutdown
    # On Windows, os.kill with SIGINT works for the current process
    def _do_shutdown():
        import time
        time.sleep(0.5)  # Let the response return first
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=_do_shutdown, daemon=True).start()
    return {"status": "shutting_down"}
