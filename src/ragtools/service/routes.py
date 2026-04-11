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
    compact: bool = Query(False, description="Token-efficient output for MCP"),
):
    """Search the knowledge base."""
    owner = get_owner()
    return owner.search_formatted(query=query, project_id=project, top_k=top_k, compact=compact)


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


# --- Project Management ---

class ProjectCreateRequest(BaseModel):
    id: str
    name: str = ""
    path: str
    enabled: bool = True
    ignore_patterns: list[str] = []


class ProjectUpdateRequest(BaseModel):
    name: Optional[str] = None
    path: Optional[str] = None
    enabled: Optional[bool] = None
    ignore_patterns: Optional[list[str]] = None


import re
def _validate_project_id(pid: str) -> str | None:
    if not pid or not pid.strip():
        return "Project ID is required"
    if not re.match(r'^[a-z0-9][a-z0-9_-]*$', pid):
        return "ID must be lowercase alphanumeric with hyphens/underscores"
    if len(pid) > 64:
        return "ID must be 64 characters or fewer"
    return None


@router.get("/api/projects/configured")
def projects_configured():
    """List configured projects with index stats."""
    from pathlib import Path as P
    settings = get_settings()
    state_path = P(settings.state_db)
    index_data = {}
    if state_path.exists():
        from ragtools.indexing.state import IndexState
        state = IndexState(settings.state_db)
        for project in settings.projects:
            records = state.get_all_for_project(project.id)
            index_data[project.id] = {"files": len(records), "chunks": sum(r["chunk_count"] for r in records)}
        state.close()

    return {"projects": [
        {
            "id": p.id, "name": p.name, "path": p.path,
            "enabled": p.enabled, "ignore_patterns": p.ignore_patterns,
            "files": index_data.get(p.id, {}).get("files", 0),
            "chunks": index_data.get(p.id, {}).get("chunks", 0),
        }
        for p in settings.projects
    ]}


def _schedule_auto_index(project_id: str):
    """Start auto-indexing a project in a background thread.

    Uses a timer thread (3s delay) so the HTTP response completes first
    and the watcher restart releases the RLock before indexing begins.
    """
    import threading
    from ragtools.service.activity import log_activity

    def _run():
        try:
            log_activity("info", "indexer", f"Auto-indexing {project_id}...")
            owner = get_owner()
            stats = owner.run_full_index(project_id=project_id)
            log_activity("success", "indexer",
                f"Auto-indexed {project_id}: {stats.get('files_indexed', 0)} files, {stats.get('chunks_indexed', 0)} chunks")
        except Exception as e:
            log_activity("error", "indexer", f"Auto-index failed for {project_id}: {e}")

    timer = threading.Timer(3.0, _run)
    timer.daemon = False
    timer.start()


@router.post("/api/projects")
def project_create(req: ProjectCreateRequest):
    """Add a new project."""
    from pathlib import Path as P
    from ragtools.config import ProjectConfig

    err = _validate_project_id(req.id)
    if err:
        raise HTTPException(status_code=422, detail=err)

    settings = get_settings()
    if any(p.id == req.id for p in settings.projects):
        raise HTTPException(status_code=422, detail=f"Project ID '{req.id}' already exists")

    path = str(P(req.path).resolve())
    if not P(path).is_dir():
        raise HTTPException(status_code=422, detail=f"Path does not exist or is not a directory: {req.path}")

    # Block exact duplicate paths
    for p in settings.projects:
        if str(P(p.path).resolve()) == path:
            raise HTTPException(
                status_code=422,
                detail=f"This folder is already configured as project '{p.id}'"
            )

    new_project = ProjectConfig(
        id=req.id, name=req.name or req.id, path=path,
        enabled=req.enabled, ignore_patterns=req.ignore_patterns,
    )
    updated = list(settings.projects) + [new_project]

    from ragtools.service.pages import _save_projects_to_toml
    _save_projects_to_toml(updated)
    get_owner().update_projects(updated)

    from ragtools.service.activity import log_activity
    log_activity("info", "config", f"Project added: {req.id}")
    _restart_watcher_if_running()

    # Schedule auto-index (runs after response completes)
    _schedule_auto_index(req.id)

    return {"status": "created", "project": {"id": new_project.id, "name": new_project.name, "path": new_project.path}}


@router.put("/api/projects/{project_id}")
def project_update(project_id: str, req: ProjectUpdateRequest):
    """Update a project."""
    from pathlib import Path as P
    settings = get_settings()
    project = next((p for p in settings.projects if p.id == project_id), None)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    if req.name is not None:
        project.name = req.name
    if req.path is not None:
        resolved = str(P(req.path).resolve())
        if not P(resolved).is_dir():
            raise HTTPException(status_code=422, detail=f"Path does not exist: {req.path}")
        project.path = resolved
    if req.enabled is not None:
        project.enabled = req.enabled
    if req.ignore_patterns is not None:
        project.ignore_patterns = req.ignore_patterns

    from ragtools.service.pages import _save_projects_to_toml
    _save_projects_to_toml(list(settings.projects))
    get_owner().update_projects(list(settings.projects))

    from ragtools.service.activity import log_activity
    log_activity("info", "config", f"Project updated: {project_id}")
    _restart_watcher_if_running()
    return {"status": "updated", "project": {"id": project.id, "name": project.name, "path": project.path}}


@router.delete("/api/projects/{project_id}")
def project_delete(project_id: str):
    """Remove a project and delete its indexed data."""
    settings = get_settings()
    updated = [p for p in settings.projects if p.id != project_id]
    if len(updated) == len(settings.projects):
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    # Delete indexed data (Qdrant chunks + state DB entries)
    owner = get_owner()
    cleanup = owner.delete_project_data(project_id)

    from ragtools.service.pages import _save_projects_to_toml
    _save_projects_to_toml(updated)
    owner.update_projects(updated)

    from ragtools.service.activity import log_activity
    log_activity("warning", "config", f"Project removed: {project_id} ({cleanup['files_deleted']} files deleted)")
    _restart_watcher_if_running()
    return {"status": "removed", "project_id": project_id, "files_deleted": cleanup["files_deleted"]}


@router.post("/api/projects/{project_id}/toggle")
def project_toggle(project_id: str):
    """Toggle project enabled/disabled."""
    settings = get_settings()
    project = next((p for p in settings.projects if p.id == project_id), None)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    project.enabled = not project.enabled

    from ragtools.service.pages import _save_projects_to_toml
    _save_projects_to_toml(list(settings.projects))
    get_owner().update_projects(list(settings.projects))

    from ragtools.service.activity import log_activity
    log_activity("info", "config", f"Project {project_id} {'enabled' if project.enabled else 'disabled'}")
    _restart_watcher_if_running()
    return {"status": "toggled", "project_id": project_id, "enabled": project.enabled}


# --- Config ---

@router.get("/api/config")
def config():
    """Return current settings."""
    settings = get_settings()
    return {
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
        "startup_open_browser": settings.startup_open_browser,
    }


class ConfigUpdateRequest(BaseModel):
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    top_k: Optional[int] = None
    score_threshold: Optional[float] = None
    service_port: Optional[int] = None
    log_level: Optional[str] = None


RESTART_FIELDS = {"service_port", "log_level"}
HOT_RELOAD_FIELDS = {"chunk_size", "chunk_overlap", "top_k", "score_threshold"}
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
        project_count = len(settings.enabled_projects) if settings.has_explicit_projects else 0
        logger.info("Watcher started: %d projects", project_count)
        return {"status": "started", "project_count": project_count}


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
    settings = get_settings()
    with _watcher_lock:
        if _watcher_thread is not None and _watcher_thread.is_alive():
            if settings.has_explicit_projects:
                paths = [p.path for p in settings.enabled_projects]
                return {"running": True, "paths": paths, "project_count": len(paths)}
            else:
                return {"running": True, "project_count": 0}
        return {"running": False}


def _restart_watcher_if_running():
    """Restart the watcher if it's currently running. Called after project config changes.

    Runs in a background thread so it doesn't block the HTTP response.
    """
    import threading as _th
    def _do_restart():
        with _watcher_lock:
            if _watcher_thread is not None and _watcher_thread.is_alive():
                watcher_stop()
                watcher_start()
                logger.info("Watcher restarted after project config change")
    _th.Thread(target=_do_restart, daemon=True).start()


# --- Semantic Map ---

# --- Folder Resolver (browser-native folder picker support) ---


class ResolveFolderRequest(BaseModel):
    name: str
    files: list[str] = []


@router.post("/api/resolve-folder")
def resolve_folder(req: ResolveFolderRequest):
    """Resolve a folder name + sample files to an absolute path.

    The browser's <input webkitdirectory> gives us the folder name and
    relative file paths but NOT the absolute path (security restriction).
    This endpoint finds the actual folder on disk by searching common locations.
    """
    from pathlib import Path as P
    import sys

    name = req.name.strip()
    if not name:
        return {"path": None}

    sample = req.files[:5]

    def _check(candidate: P) -> bool:
        if not candidate.is_dir():
            return False
        if not sample:
            return True
        return any((candidate / f).exists() for f in sample)

    # Build search roots
    home = P.home()
    roots = [home]
    try:
        roots.extend(d for d in home.iterdir() if d.is_dir() and not d.name.startswith('.'))
    except OSError:
        pass

    if sys.platform == "win32":
        import string
        for letter in string.ascii_uppercase:
            drive = P(f"{letter}:\\")
            if drive.exists():
                roots.append(drive)
    else:
        for mp in [P("/mnt"), P("/media"), P("/opt"), P("/tmp")]:
            if mp.is_dir():
                roots.append(mp)

    # Search: <root>/name
    for root in roots:
        candidate = root / name
        if _check(candidate):
            return {"path": str(candidate)}

    # Depth-2: <root>/*/name
    for root in roots[:5]:
        try:
            for sub in root.iterdir():
                if sub.is_dir() and not sub.name.startswith('.'):
                    candidate = sub / name
                    if _check(candidate):
                        return {"path": str(candidate)}
        except OSError:
            continue

    return {"path": None}


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


# --- MCP Connection ---

@router.get("/api/mcp-config")
def mcp_config():
    """Return the MCP server configuration JSON for Claude Code.

    Detects the runtime environment:
    - Frozen exe (installed): returns full path to rag.exe + 'serve' subcommand
    - Dev/pip install: returns generic 'rag-mcp' entry point
    """
    import sys
    import shutil

    if getattr(sys, "frozen", False):
        # Installed via exe: use the actual executable path
        config = {
            "mcpServers": {
                "ragtools": {
                    "command": sys.executable,
                    "args": ["serve"]
                }
            }
        }
    elif shutil.which("rag-mcp"):
        # Dev/pip install: use the entry point
        config = {
            "mcpServers": {
                "ragtools": {
                    "command": "rag-mcp",
                    "args": []
                }
            }
        }
    else:
        # Fallback: python module
        config = {
            "mcpServers": {
                "ragtools": {
                    "command": "python",
                    "args": ["-m", "ragtools.integration.mcp_server"]
                }
            }
        }
    return {"config": config}


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
