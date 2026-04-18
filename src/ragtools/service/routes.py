"""HTTP API routes for the RAG service."""

import logging
import os
import re
import signal
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ragtools.service.app import get_owner, get_settings, get_shutdown_event

logger = logging.getLogger("ragtools.service")

router = APIRouter()


def _mcp_source(request: Request) -> str:
    """Return the activity-log source tag for a potentially-MCP-attributed write.

    Reads the ``X-MCP-Session`` header set by the MCP server's httpx client.
    If present, returns ``"mcp:<id>"`` so the admin-panel activity drawer
    distinguishes between concurrent Claude Code sessions. Otherwise returns
    plain ``"mcp"`` (old clients or direct-HTTP callers).
    """
    sid = request.headers.get("x-mcp-session") or request.headers.get("X-MCP-Session")
    return f"mcp:{sid}" if sid else "mcp"

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
    project: Optional[str] = Query(None, description="Filter by a single project"),
    projects: Optional[str] = Query(
        None,
        description=(
            "Comma-separated list of project IDs — search the union of these "
            "projects. Takes precedence over ``project`` when both are given."
        ),
    ),
    top_k: int = Query(10, description="Max results"),
    compact: bool = Query(False, description="Token-efficient output for MCP"),
    structured: bool = Query(
        False,
        description=(
            "When true, return a structured payload with context + results + meta "
            "so MCP agents can reason programmatically. Default false preserves "
            "the current shape for backward compatibility."
        ),
    ),
):
    """Search the knowledge base — all projects, one project, or a set of projects."""
    owner = get_owner()
    project_ids: Optional[list[str]] = None
    if projects:
        project_ids = [p.strip() for p in projects.split(",") if p.strip()]
    result = owner.search_formatted(
        query=query,
        project_id=project,
        project_ids=project_ids,
        top_k=top_k,
        compact=compact,
    )
    # The owner.search_formatted already returns {query, count, results, formatted}.
    # For structured mode, re-shape into the documented {context, results, meta}.
    if structured:
        return {
            "context": result.get("formatted", ""),
            "results": result.get("results", []),
            "meta": {
                "query": result.get("query", query),
                "count": result.get("count", 0),
                "project": project,
                "projects": project_ids,
                "top_k": top_k,
                "compact": compact,
            },
        }
    return result


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
    try:
        from ragtools.service.notify import notify_rebuild_complete
        notify_rebuild_complete(
            get_settings(),
            files=stats.get("files_indexed", 0),
            chunks=stats.get("chunks_indexed", 0),
        )
    except Exception as e:
        logger.debug("rebuild-complete toast failed (non-fatal): %s", e)
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
            files = stats.get("files_indexed", 0)
            chunks = stats.get("chunks_indexed", 0)
            log_activity("success", "indexer",
                f"Auto-indexed {project_id}: {files} files, {chunks} chunks")
            try:
                from ragtools.service.notify import notify_project_indexed
                notify_project_indexed(get_settings(), project_id, files, chunks)
            except Exception as e:
                logger.debug("project-indexed toast failed (non-fatal): %s", e)
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
        "desktop_notifications": settings.desktop_notifications,
        "notification_cooldown_seconds": settings.notification_cooldown_seconds,
        "mcp_tools": settings.mcp_tools,
    }


class ConfigUpdateRequest(BaseModel):
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    top_k: Optional[int] = None
    score_threshold: Optional[float] = None
    service_port: Optional[int] = None
    log_level: Optional[str] = None
    desktop_notifications: Optional[bool] = None
    mcp_tools: Optional[dict] = None


# Changing any of these requires restarting the MCP server process
# (stdio clients re-read config only at launch). We still accept the update
# here so the TOML file is current next time the MCP starts.
MCP_RESTART_FIELDS = {"mcp_tools"}
RESTART_FIELDS = {"service_port", "log_level"}
HOT_RELOAD_FIELDS = {
    "chunk_size", "chunk_overlap", "top_k", "score_threshold",
    "desktop_notifications",
    "mcp_tools",  # hot-reloads for next MCP client connection
}
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

    if req.desktop_notifications is not None:
        updates["desktop_notifications"] = bool(req.desktop_notifications)

    if req.mcp_tools is not None:
        # Normalise + coerce values to bool so a stray "true" string can't
        # sneak through as a truthy non-bool.
        cleaned: dict[str, bool] = {str(k): bool(v) for k, v in req.mcp_tools.items()}
        updates["mcp_tools"] = cleaned

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

    # Save to TOML. `mcp_tools` lives under the [mcp] section so the
    # loader's ``key_subkey`` flattener reconstructs it correctly on read.
    from ragtools.service.pages import _update_toml_config
    mcp_updates = {}
    root_updates = {}
    for k, v in updates.items():
        if k == "mcp_tools":
            mcp_updates["tools"] = v
        else:
            root_updates[k] = v
    if root_updates:
        _update_toml_config(None, root_updates)
    if mcp_updates:
        _update_toml_config("mcp", mcp_updates)

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


# --- Notifications ---


@router.post("/api/notifications/test")
def notifications_test():
    """Fire a test desktop toast so the user can verify the pipeline.

    Respects the opt-out toggle: if desktop_notifications is disabled, returns
    `{sent: false, reason: "disabled"}` so the UI can explain why nothing
    appeared. Uses a fresh CrashNotifier so repeated clicks bypass the
    per-kind cooldown — the user wants a toast on every click.
    """
    from ragtools.service.notify import CrashNotifier, _admin_url

    settings = get_settings()
    if not settings.desktop_notifications:
        return {"sent": False, "reason": "disabled"}

    notifier = CrashNotifier(settings=settings)
    dispatched = notifier.notify(
        kind="test",
        title="RAG Tools — test notification",
        message="Desktop notifications are working. This is a test from the admin panel.",
        deep_link=_admin_url(settings),
    )
    return {"sent": bool(dispatched)}


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
        # Dev/pip install: use the entry point, and a dev-specific name so it
        # coexists with the installed "ragtools" MCP in the same .mcp.json.
        config = {
            "mcpServers": {
                "ragtools-dev": {
                    "command": "rag-mcp",
                    "args": []
                }
            }
        }
    else:
        # Fallback: python module (also dev mode)
        config = {
            "mcpServers": {
                "ragtools-dev": {
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


# --- Crash history ---

@router.get("/api/crash-history")
def crash_history():
    """List any unreviewed crash markers (service crashes, supervisor give-up).

    The admin panel fetches this on every page load and renders a dismissable
    banner if the list is non-empty. Older than 30 days are filtered out so
    stale markers don't haunt the UI forever.
    """
    from ragtools.service.crash_history import list_unreviewed_crashes
    settings = get_settings()
    items = list_unreviewed_crashes(settings)
    return {"count": len(items), "items": items}


@router.post("/api/crash-history/{dismiss_key}/dismiss")
def crash_history_dismiss(dismiss_key: str):
    """Mark a crash marker as reviewed. The file is renamed with a
    `.reviewed` suffix so it is preserved for post-mortem."""
    from ragtools.service.crash_history import dismiss_crash_marker
    settings = get_settings()
    ok = dismiss_crash_marker(settings, dismiss_key)
    if not ok:
        raise HTTPException(status_code=404, detail=f"No crash marker named '{dismiss_key}' to dismiss")
    return {"dismissed": dismiss_key}


# --- Project Inspection (read-only, Family A) ---


def _resolve_project(project_id: str):
    """Look up a configured project by ID. Returns the ProjectConfig or None."""
    settings = get_settings()
    return next((p for p in settings.projects if p.id == project_id), None)


@router.get("/api/projects/{project_id}/status")
def project_status_endpoint(project_id: str):
    """Single-project state snapshot — the agent's 'orient me' call."""
    from pathlib import Path as _P
    from ragtools.indexing.state import IndexState

    project = _resolve_project(project_id)
    if project is None:
        raise HTTPException(status_code=404,
                            detail=f"Project '{project_id}' is not configured")

    settings = get_settings()
    summary = {"files": 0, "chunks": 0, "last_indexed": None}
    state_path = _P(settings.state_db)
    if state_path.exists():
        state = IndexState(settings.state_db)
        try:
            summary = state.get_project_summary(project_id)
        finally:
            state.close()

    path = _P(project.path)
    return {
        "project_id":           project.id,
        "name":                 project.name,
        "path":                 str(path),
        "path_exists":          path.is_dir(),
        "enabled":              project.enabled,
        "files":                summary["files"],
        "chunks":               summary["chunks"],
        "last_indexed":         summary["last_indexed"],
        "ignore_patterns_count": len(project.ignore_patterns or []),
    }


@router.get("/api/projects/{project_id}/summary")
def project_summary_endpoint(project_id: str, top_files: int = Query(10, ge=1, le=50)):
    """Content-focused snapshot — top files, rough size signals."""
    from pathlib import Path as _P
    from ragtools.indexing.state import IndexState

    project = _resolve_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    settings = get_settings()
    state_path = _P(settings.state_db)
    if not state_path.exists():
        return {"project_id": project_id, "top_files": [], "files": 0, "chunks": 0}

    state = IndexState(settings.state_db)
    try:
        summary = state.get_project_summary(project_id)
        top = state.get_top_files_by_chunks(project_id, limit=top_files)
    finally:
        state.close()
    return {
        "project_id":    project_id,
        "name":          project.name,
        "path":          project.path,
        "files":         summary["files"],
        "chunks":        summary["chunks"],
        "top_files":     top,
    }


@router.get("/api/projects/{project_id}/files")
def project_files_endpoint(project_id: str, limit: int = Query(200, ge=1, le=1000)):
    """List indexed file paths for one project."""
    from pathlib import Path as _P
    from ragtools.indexing.state import IndexState

    project = _resolve_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    settings = get_settings()
    state_path = _P(settings.state_db)
    if not state_path.exists():
        return {"project_id": project_id, "files": [], "count": 0}

    state = IndexState(settings.state_db)
    try:
        rows = state.get_all_for_project(project_id)[:limit]
    finally:
        state.close()
    files = [{"path": r["file_path"], "chunks": r.get("chunk_count", 0)} for r in rows]
    return {"project_id": project_id, "count": len(files), "files": files}


@router.get("/api/projects/{project_id}/ignore")
def project_ignore_endpoint(project_id: str):
    """Return the effective ignore rules for a project (layered)."""
    from pathlib import Path as _P
    from ragtools.ignore import IgnoreRules

    project = _resolve_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    settings = get_settings()
    combined = list(settings.ignore_patterns) + list(project.ignore_patterns or [])
    rules = IgnoreRules(
        content_root=project.path,
        global_patterns=combined,
        use_ragignore=settings.use_ragignore_files,
    )
    patterns = rules.get_all_patterns()
    return {
        "project_id":      project.id,
        "path":            project.path,
        "built_in":        list(patterns.get("built-in", [])),
        "config_global":   list(settings.ignore_patterns),
        "config_project":  list(project.ignore_patterns or []),
        "ragignore_files_enabled": settings.use_ragignore_files,
    }


class IgnorePreviewRequest(BaseModel):
    pattern: str


class IgnoreRuleRequest(BaseModel):
    pattern: str


@router.post("/api/projects/{project_id}/reindex")
def project_reindex_endpoint(project_id: str, request: Request):
    """Drop and re-index one project's data. Other projects are untouched."""
    project = _resolve_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    owner = get_owner()
    stats = owner.reindex_project(project_id)

    from ragtools.service.activity import log_activity
    log_activity("warning", _mcp_source(request),
                 f"Reindex executed for project '{project_id}' "
                 f"({stats.get('files_indexed', 0)} files indexed)")
    return {"status": "reindexed", "project_id": project_id, "stats": stats}


@router.post("/api/projects/{project_id}/ignore")
def project_ignore_add_endpoint(project_id: str, req: IgnoreRuleRequest, request: Request):
    """Add a pattern to the project's ignore_patterns list and persist to TOML.

    Does NOT reindex automatically — agent should call the reindex tool
    separately. This keeps cause-and-effect explicit.
    """
    from ragtools.service.activity import log_activity
    from ragtools.service.pages import _save_projects_to_toml

    project = _resolve_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    pattern = (req.pattern or "").strip()
    if not pattern:
        raise HTTPException(status_code=422, detail="Pattern is required")

    existing = list(project.ignore_patterns or [])
    if pattern in existing:
        return {"status": "unchanged", "project_id": project_id, "pattern": pattern,
                "reason": "already present"}

    existing.append(pattern)
    project.ignore_patterns = existing
    settings = get_settings()
    _save_projects_to_toml(list(settings.projects))
    get_owner().update_projects(list(settings.projects))

    log_activity("info", _mcp_source(request),
                 f"Ignore pattern '{pattern}' added to project '{project_id}'")
    return {
        "status": "added",
        "project_id": project_id,
        "pattern": pattern,
        "ignore_patterns_count": len(existing),
        "note": "Run reindex_project or run_index to propagate the change",
    }


@router.delete("/api/projects/{project_id}/ignore")
def project_ignore_remove_endpoint(
    project_id: str,
    request: Request,
    pattern: str = Query(..., description="Pattern to remove"),
):
    """Remove a pattern from the project's ignore_patterns list."""
    from ragtools.service.activity import log_activity
    from ragtools.service.pages import _save_projects_to_toml

    project = _resolve_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    pattern = pattern.strip()
    existing = list(project.ignore_patterns or [])
    if pattern not in existing:
        return {"status": "unchanged", "project_id": project_id, "pattern": pattern,
                "reason": "not present"}

    existing.remove(pattern)
    project.ignore_patterns = existing
    settings = get_settings()
    _save_projects_to_toml(list(settings.projects))
    get_owner().update_projects(list(settings.projects))

    log_activity("info", _mcp_source(request),
                 f"Ignore pattern '{pattern}' removed from project '{project_id}'")
    return {
        "status": "removed",
        "project_id": project_id,
        "pattern": pattern,
        "ignore_patterns_count": len(existing),
        "note": "Run reindex_project to pick up previously excluded files",
    }


@router.post("/api/projects/{project_id}/ignore/preview")
def project_ignore_preview_endpoint(project_id: str, req: IgnorePreviewRequest):
    """Dry-run: which currently-indexed files WOULD be excluded if we added
    this pattern? Does not modify any configuration."""
    from pathlib import Path as _P
    from ragtools.indexing.state import IndexState

    project = _resolve_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    pattern = (req.pattern or "").strip()
    if not pattern:
        raise HTTPException(status_code=422, detail="Pattern is required")

    import pathspec
    spec = pathspec.PathSpec.from_lines("gitwildmatch", [pattern])

    settings = get_settings()
    state_path = _P(settings.state_db)
    excluded: list[str] = []
    if state_path.exists():
        state = IndexState(settings.state_db)
        try:
            rows = state.get_all_for_project(project_id)
        finally:
            state.close()
        project_root = _P(project.path).resolve()
        for row in rows:
            file_path = _P(row["file_path"])
            try:
                rel = file_path.relative_to(project_root) if file_path.is_absolute() else file_path
            except ValueError:
                rel = file_path
            if spec.match_file(str(rel).replace("\\", "/")):
                excluded.append(str(rel))

    return {
        "project_id":     project_id,
        "pattern":        pattern,
        "would_exclude":  excluded,
        "count":          len(excluded),
    }


# --- Diagnostics (logs + system health) ---


@router.get("/api/logs/tail")
def logs_tail(
    source: str = Query("service", description="Log source to read"),
    limit: int = Query(50, description="Max lines to return (1-500)"),
):
    """Return the tail of a whitelisted log file.

    The whitelist prevents arbitrary-path reads. Source names that are not in
    ``ragtools.service.logs.available_sources()`` are rejected with 422.
    """
    from ragtools.service.logs import tail
    settings = get_settings()
    result = tail(settings, source=source, limit=limit)
    if "error" in result:
        raise HTTPException(status_code=422, detail=result)
    return result


@router.get("/api/system-health")
def system_health_endpoint():
    """Structured health snapshot — equivalent to the ``rag doctor`` output,
    but as JSON so both the admin UI and MCP ops tools can consume it.
    """
    import sys as _sys
    from ragtools.config import Settings as _Settings
    settings = get_settings()

    checks: list[dict] = []

    py_ver = f"{_sys.version_info.major}.{_sys.version_info.minor}.{_sys.version_info.micro}"
    checks.append({
        "component": "python",
        "status": "ok" if _sys.version_info >= (3, 10) else "error",
        "detail": py_ver,
    })

    # Collection / scale
    try:
        owner = get_owner()
        status = owner.get_status()
        from ragtools.service.owner import compute_scale_warning
        pc = status.get("points_count", 0)
        scale = compute_scale_warning(pc)
        checks.append({
            "component": "collection",
            "status": "warning" if scale["level"] == "over" else "ok",
            "detail": f"{pc} points",
            "scale_level": scale["level"],
            "scale_message": scale["message"],
        })
    except Exception as e:
        checks.append({"component": "collection", "status": "error", "detail": str(e)})

    # Startup + Watchdog (Windows only)
    if _sys.platform == "win32":
        try:
            from ragtools.service.startup import is_task_installed
            checks.append({
                "component": "login_startup",
                "status": "ok" if is_task_installed() else "missing",
                "detail": "Registered in Startup folder" if is_task_installed() else "rag service install",
            })
        except Exception as e:
            checks.append({"component": "login_startup", "status": "error", "detail": str(e)})

        try:
            from ragtools.service.watchdog import is_watchdog_installed, TASK_NAME
            checks.append({
                "component": "watchdog",
                "status": "ok" if is_watchdog_installed() else "missing",
                "detail": TASK_NAME if is_watchdog_installed() else "rag service watchdog install",
            })
        except Exception as e:
            checks.append({"component": "watchdog", "status": "error", "detail": str(e)})

    return {"checks": checks, "platform": _sys.platform}


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
