"""HTML page routes and htmx fragment routes for the admin panel."""

import logging
from html import escape
from pathlib import Path

from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ragtools.service.app import get_owner, get_settings

logger = logging.getLogger("ragtools.service")

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

page_router = APIRouter()


# --- Helpers ---


def _load_index_stats(settings) -> dict:
    """Load file/chunk counts per project from the index state DB."""
    from ragtools.indexing.state import IndexState

    state_path = Path(settings.state_db)
    index_data = {}
    if state_path.exists():
        state = IndexState(settings.state_db)
        for p in settings.projects:
            records = state.get_all_for_project(p.id)
            index_data[p.id] = {"files": len(records), "chunks": sum(r["chunk_count"] for r in records)}
        state.close()
    return index_data


# --- Full page routes ---


@page_router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {"page": "dashboard"})


@page_router.get("/map", response_class=HTMLResponse)
def map_page(request: Request):
    return templates.TemplateResponse(request, "map.html", {"page": "map"})


@page_router.get("/search", response_class=HTMLResponse)
def search_page(request: Request):
    return templates.TemplateResponse(request, "search.html", {"page": "search"})


@page_router.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    return templates.TemplateResponse(request, "config.html", {"page": "config"})


@page_router.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request):
    return templates.TemplateResponse(request, "projects.html", {"page": "projects"})


# --- Dashboard fragments ---


@page_router.get("/ui/dash/status", response_class=HTMLResponse)
def ui_dash_status():
    """Compact status row for the dashboard."""
    owner = get_owner()
    s = owner.get_status()
    from ragtools.service.routes import _watcher_thread, _watcher_lock
    with _watcher_lock:
        watcher_running = _watcher_thread is not None and _watcher_thread.is_alive()

    watcher_badge = '<span class="badge badge-success">Watcher running</span>' if watcher_running else '<span class="badge badge-muted">Watcher starting</span>'
    files = s["total_files"]
    chunks = s["total_chunks"]
    projects_count = len(s["projects"])

    return f"""
    <div class="dash-status-row">
        <div class="dash-stat"><strong>{files}</strong> <span>files</span></div>
        <div class="dash-stat"><strong>{chunks}</strong> <span>chunks</span></div>
        <div class="dash-stat"><strong>{projects_count}</strong> <span>projects</span></div>
        <div class="dash-stat">{watcher_badge}</div>
    </div>
    """


@page_router.get("/ui/dash/projects", response_class=HTMLResponse)
def ui_dash_projects():
    """Projects card for dashboard — shows empty state if no projects."""
    settings = get_settings()

    if not settings.projects:
        return """
        <div class="card" style="text-align:center; padding:32px 20px;">
            <p style="font-size:15px; color:var(--color-text); margin-bottom:6px; font-weight:500;">No projects configured</p>
            <p style="font-size:13px; color:var(--color-text-muted); margin-bottom:16px;">Add a content folder to start indexing and searching your knowledge base.</p>
            <a href="/projects" class="btn btn-primary">Add Your First Project</a>
        </div>
        """

    index_data = _load_index_stats(settings)

    rows = ""
    for p in settings.projects:
        idx = index_data.get(p.id, {"files": 0, "chunks": 0})
        badge = '<span class="badge badge-success">Enabled</span>' if p.enabled else '<span class="badge badge-muted">Disabled</span>'
        info = f"{idx['files']} files, {idx['chunks']} chunks" if idx["files"] > 0 else '<span style="color:var(--color-text-muted)">Not indexed</span>'
        rows += f'<tr><td><strong>{escape(p.name)}</strong></td><td>{badge}</td><td>{info}</td></tr>'

    return f"""
    <div class="card">
        <div class="card-header">Projects</div>
        <table class="table-clean">
            <thead><tr><th>Project</th><th>Status</th><th>Indexed</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
        <div style="margin-top:10px;"><a href="/projects" class="btn btn-secondary btn-sm">Manage Projects</a></div>
    </div>
    """


@page_router.get("/ui/dash/activity", response_class=HTMLResponse)
def ui_dash_activity():
    """Inline recent activity for dashboard — last 5 events."""
    from ragtools.service.activity import activity_log
    events = activity_log.get_recent(limit=5)

    if not events:
        return '<p class="activity-empty">No recent activity</p>'

    rows = []
    for e in reversed(events):
        level_class = {
            "info": "badge-accent", "success": "badge-success",
            "warning": "badge-warning", "error": "badge-danger",
        }.get(e.level, "badge-accent")

        rows.append(f"""
        <div class="activity-event">
            <span class="activity-time">{escape(e.timestamp[11:19])}</span>
            <span class="badge {level_class}" style="font-size:10px;">{escape(e.level)}</span>
            <span class="activity-source">{escape(e.source)}</span>
            <span class="activity-msg">{escape(e.message)}</span>
        </div>
        """)

    return "".join(rows)


# --- htmx fragment routes (return HTML snippets, not full pages) ---


@page_router.get("/ui/status", response_class=HTMLResponse)
def ui_status():
    """Stats fragment for dashboard and index page."""
    owner = get_owner()
    s = owner.get_status()
    return f"""
    <table class="table-clean">
        <tr><td>Total files</td><td><strong>{s['total_files']}</strong></td></tr>
        <tr><td>Total chunks</td><td><strong>{s['total_chunks']}</strong></td></tr>
        <tr><td>Points</td><td><strong>{s['points_count']}</strong></td></tr>
        <tr><td>Collection</td><td><code>{escape(s['collection_name'])}</code></td></tr>
        <tr><td>Projects</td><td>{escape(', '.join(s['projects'])) or '<span style="color:var(--color-text-muted)">none</span>'}</td></tr>
        <tr><td>Last indexed</td><td>{escape(s['last_indexed'] or 'never')}</td></tr>
    </table>
    """


@page_router.get("/ui/projects", response_class=HTMLResponse)
def ui_projects():
    """Projects table fragment for dashboard — merges config + index data."""
    owner = get_owner()
    settings = get_settings()

    if settings.has_explicit_projects:
        index_data = _load_index_stats(settings)

        if not settings.projects:
            return '<p style="color: var(--color-text-muted);">No projects configured. <a href="/projects">Add a project</a></p>'

        rows = ""
        for p in settings.projects:
            idx = index_data.get(p.id, {"files": 0, "chunks": 0})
            badge = '<span class="badge badge-success">Enabled</span>' if p.enabled else '<span class="badge badge-muted">Disabled</span>'
            info = f"{idx['files']} files, {idx['chunks']} chunks" if idx["files"] > 0 else '<span style="color:var(--color-text-muted)">Not indexed</span>'
            rows += f'<tr><td><strong>{escape(p.name)}</strong></td><td>{badge}</td><td>{info}</td></tr>'

        return f"""
        <table class="table-clean">
            <thead><tr><th>Project</th><th>Status</th><th>Indexed</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
        <div style="margin-top:10px;"><a href="/projects" class="btn btn-secondary btn-sm">Manage Projects</a></div>
        """
    else:
        projects = owner.get_projects()
        if not projects:
            return '<p style="color: var(--color-text-muted);">No projects indexed yet.</p>'
        rows = "".join(
            f"<tr><td><strong>{escape(p['project_id'])}</strong></td><td>{p['files']}</td><td>{p['chunks']}</td></tr>"
            for p in projects
        )
        return f"""
        <table class="table-clean">
            <thead><tr><th>Project</th><th>Files</th><th>Chunks</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
        """


@page_router.get("/ui/watcher", response_class=HTMLResponse)
def ui_watcher():
    """Watcher status fragment (always-on, informational only)."""
    from ragtools.service.routes import _watcher_thread, _watcher_lock
    with _watcher_lock:
        running = _watcher_thread is not None and _watcher_thread.is_alive()

    if running:
        return """
        <div style="display:flex; align-items:center; gap:12px;">
            <span>Status:</span> <span class="badge badge-success">Running</span>
            <span style="font-size:12px; color:var(--color-text-muted);">Watches project folders for changes</span>
        </div>
        """
    else:
        return """
        <div style="display:flex; align-items:center; gap:12px;">
            <span>Status:</span> <span class="badge badge-muted">Starting...</span>
        </div>
        """


@page_router.get("/ui/search", response_class=HTMLResponse)
def ui_search(
    query: str = Query("", description="Search query"),
    project: str = Query("", description="Project filter"),
    top_k: int = Query(10, description="Max results"),
):
    """Search results HTML fragment."""
    if not query.strip():
        return '<p style="color: var(--color-text-muted);">Enter a search query above.</p>'

    owner = get_owner()
    data = owner.search_formatted(
        query=query.strip(),
        project_id=project if project else None,
        top_k=top_k,
    )

    if data["count"] == 0:
        return f'<p style="color: var(--color-text-muted);">No results found for: <em>{escape(query)}</em></p>'

    cards = []
    for i, r in enumerate(data["results"], 1):
        headings = " &gt; ".join(escape(h) for h in r["headings"]) if r["headings"] else "N/A"
        confidence = r["confidence"].lower()
        badge_class = f"badge-{'success' if confidence == 'high' else 'warning' if confidence == 'moderate' else 'danger'}"
        text_preview = escape(r["text"][:300]) + ("..." if len(r["text"]) > 300 else "")
        cards.append(f"""
        <div class="result-card confidence-{confidence}">
            <div class="meta">
                <span class="badge {badge_class}">{r['confidence']}</span>
                <span>{r['score']:.3f}</span>
                <span>{escape(r['project_id'])}/{escape(r['file_path'])}</span>
                <span>{headings}</span>
            </div>
            <p class="text-preview">{text_preview}</p>
        </div>
        """)

    return f'<p><strong>{data["count"]} results</strong> for: <em>{escape(query)}</em></p>' + "".join(cards)


@page_router.post("/ui/index", response_class=HTMLResponse)
def ui_index(full: bool = Query(False)):
    """Run index and return results fragment."""
    owner = get_owner()
    if full:
        stats = owner.run_full_index()
        return f"""
        <div class="flash flash-success">
            Full index complete: {stats['files_indexed']} files, {stats['chunks_indexed']} chunks,
            projects: {', '.join(stats['projects']) or 'none'}
        </div>
        """
    else:
        stats = owner.run_incremental_index()
        return f"""
        <div class="flash flash-success">
            Incremental index: {stats['indexed']} indexed, {stats['skipped']} skipped,
            {stats['deleted']} deleted, {stats['chunks_indexed']} chunks
        </div>
        """


@page_router.post("/ui/rebuild", response_class=HTMLResponse)
def ui_rebuild():
    """Rebuild and return results fragment."""
    owner = get_owner()
    stats = owner.rebuild()
    return f"""
    <div class="flash flash-success">
        Rebuild complete: {stats['files_indexed']} files, {stats['chunks_indexed']} chunks,
        projects: {', '.join(stats['projects']) or 'none'}
    </div>
    """


@page_router.get("/ui/config", response_class=HTMLResponse)
def ui_config():
    """Config display fragment."""
    settings = get_settings()
    groups = {
        "Indexing": {
            "Chunk size": settings.chunk_size,
            "Chunk overlap": settings.chunk_overlap,
        },
        "Retrieval": {
            "Top K": settings.top_k,
            "Score threshold": settings.score_threshold,
            "Embedding model": settings.embedding_model,
        },
        "Service": {
            "Host": settings.service_host,
            "Port": settings.service_port,
            "Log level": settings.log_level,
        },
        "Storage": {
            "Qdrant path": settings.qdrant_path,
            "State DB": settings.state_db,
            "Collection": settings.collection_name,
        },
    }

    html = ""
    for group_name, fields in groups.items():
        rows = "".join(
            f"<tr><td>{escape(k)}</td><td><code>{escape(str(v))}</code></td></tr>"
            for k, v in fields.items()
        )
        html += f"""
        <div class="card">
            <div class="card-header">{escape(group_name)}</div>
            <table class="table-clean">{rows}</table>
        </div>
        """
    return html


# --- Startup fragments ---


# --- Project management fragments ---


def _render_projects_list() -> str:
    """Render the projects table HTML. Shared by all mutating project fragments."""
    settings = get_settings()
    if not settings.projects:
        return '''<div style="text-align:center; padding:24px; color:var(--color-text-muted);">
            <p>No projects configured yet.</p>
            <p style="font-size:13px;">Use the form above to add your first content folder.</p>
        </div>'''

    index_data = _load_index_stats(settings)

    rows = ""
    for p in settings.projects:
        idx = index_data.get(p.id, {"files": 0, "chunks": 0})
        badge = '<span class="badge badge-success">Enabled</span>' if p.enabled else '<span class="badge badge-muted">Disabled</span>'
        files = str(idx["files"]) if idx["files"] > 0 else "--"
        chunks = str(idx["chunks"]) if idx["chunks"] > 0 else "--"
        toggle_label = "Disable" if p.enabled else "Enable"
        path_display = escape(p.path)
        if len(p.path) > 40:
            path_display = escape("..." + p.path[-37:])

        rows += f"""<tr id="project-row-{escape(p.id)}">
            <td><strong>{escape(p.name)}</strong><br><code style="font-size:11px;color:var(--color-text-muted)">{escape(p.id)}</code></td>
            <td title="{escape(p.path)}"><code style="font-size:12px">{path_display}</code></td>
            <td>{badge}</td>
            <td>{files}</td>
            <td>{chunks}</td>
            <td style="white-space:nowrap">
                <button class="btn btn-secondary btn-sm"
                    hx-get="/ui/projects/{escape(p.id)}/edit" hx-target="#project-row-{escape(p.id)}" hx-swap="outerHTML"
                    hx-disabled-elt="this" hx-indicator="#projects-overlay">Edit</button>
                <button class="btn btn-secondary btn-sm"
                    hx-post="/ui/projects/{escape(p.id)}/toggle" hx-target="#projects-list" hx-swap="innerHTML"
                    hx-disabled-elt="this" hx-indicator="#projects-overlay">{toggle_label}</button>
                <button class="btn btn-danger btn-sm"
                    hx-delete="/ui/projects/{escape(p.id)}/remove" hx-target="#projects-list" hx-swap="innerHTML"
                    hx-confirm="Remove project '{escape(p.name)}' and its indexed data?"
                    hx-disabled-elt="this" hx-indicator="#projects-overlay">Remove</button>
            </td>
        </tr>"""

    return f"""
    <table class="table-clean">
        <thead><tr><th>Name</th><th>Path</th><th>Status</th><th>Files</th><th>Chunks</th><th>Actions</th></tr></thead>
        <tbody>{rows}</tbody>
    </table>
    """


@page_router.get("/ui/projects/list", response_class=HTMLResponse)
def ui_projects_list():
    """Full project list table fragment."""
    return _render_projects_list()


@page_router.post("/ui/projects/add", response_class=HTMLResponse)
def ui_projects_add(
    id: str = Form(""),
    name: str = Form(""),
    path: str = Form(""),
    ignore_patterns: str = Form(""),
):
    """Add a new project via UI form."""
    try:
        from fastapi.responses import HTMLResponse as HR
        from ragtools.service.routes import project_create, ProjectCreateRequest
        patterns = [line.strip() for line in ignore_patterns.splitlines() if line.strip()]
        req = ProjectCreateRequest(id=id.strip().lower(), name=name.strip(), path=path.strip(), ignore_patterns=patterns)
        project_create(req)
        response = HR(content=_render_projects_list())
        response.headers["HX-Trigger"] = "projectAdded"
        return response
    except Exception as e:
        detail = getattr(e, "detail", str(e))
        return f'<div class="flash flash-error">Failed to add project: {escape(str(detail))}</div>' + _render_projects_list()


@page_router.get("/ui/projects/{project_id}/edit", response_class=HTMLResponse)
def ui_projects_edit(project_id: str):
    """Inline edit form for a project row."""
    settings = get_settings()
    project = next((p for p in settings.projects if p.id == project_id), None)
    if not project:
        return f'<tr><td colspan="6"><div class="flash flash-error">Project not found</div></td></tr>'

    patterns_text = "\n".join(project.ignore_patterns)
    return f"""<tr id="project-row-{escape(project_id)}">
        <td colspan="6">
            <form hx-put="/ui/projects/{escape(project_id)}/save" hx-target="#projects-list" hx-swap="innerHTML">
                <div class="grid-2">
                    <div class="form-group">
                        <label class="form-label">Display Name</label>
                        <input type="text" name="name" class="form-input" value="{escape(project.name)}">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Folder Path</label>
                        <input type="text" name="path" class="form-input" value="{escape(project.path)}">
                    </div>
                </div>
                <details style="margin-top:8px;">
                    <summary style="font-size:13px;color:var(--color-text-secondary);cursor:pointer;">Ignore Patterns</summary>
                    <div class="form-group" style="margin-top:8px;">
                        <textarea name="ignore_patterns" rows="3" class="form-textarea" placeholder="One pattern per line">{escape(patterns_text)}</textarea>
                    </div>
                </details>
                <div style="display:flex;gap:8px;margin-top:10px;">
                    <button type="submit" class="btn btn-primary btn-sm"
                        hx-disabled-elt="this" hx-indicator="#projects-overlay">Save</button>
                    <button type="button" class="btn btn-secondary btn-sm"
                        hx-get="/ui/projects/list" hx-target="#projects-list" hx-swap="innerHTML"
                        hx-disabled-elt="this" hx-indicator="#projects-overlay">Cancel</button>
                </div>
            </form>
        </td>
    </tr>"""


@page_router.put("/ui/projects/{project_id}/save", response_class=HTMLResponse)
def ui_projects_save(
    project_id: str,
    name: str = Form(""),
    path: str = Form(""),
    ignore_patterns: str = Form(""),
):
    """Save edited project via UI form."""
    try:
        from ragtools.service.routes import project_update, ProjectUpdateRequest
        patterns = [line.strip() for line in ignore_patterns.splitlines() if line.strip()]
        req = ProjectUpdateRequest(name=name.strip() or None, path=path.strip() or None, ignore_patterns=patterns)
        project_update(project_id, req)
        return _render_projects_list()
    except Exception as e:
        detail = getattr(e, "detail", str(e))
        return f'<div class="flash flash-error">Save failed: {escape(str(detail))}</div>' + _render_projects_list()


@page_router.post("/ui/projects/{project_id}/toggle", response_class=HTMLResponse)
def ui_projects_toggle(project_id: str):
    """Toggle project enabled/disabled via UI."""
    try:
        from ragtools.service.routes import project_toggle
        project_toggle(project_id)
        return _render_projects_list()
    except Exception as e:
        detail = getattr(e, "detail", str(e))
        return f'<div class="flash flash-error">{escape(str(detail))}</div>' + _render_projects_list()


@page_router.delete("/ui/projects/{project_id}/remove", response_class=HTMLResponse)
def ui_projects_remove(project_id: str):
    """Remove a project via UI. Deletes data in background to avoid timeout."""
    import threading

    try:
        from ragtools.service.routes import _restart_watcher_if_running
        from ragtools.service.activity import log_activity

        settings = get_settings()
        project = next((p for p in settings.projects if p.id == project_id), None)
        if not project:
            return f'<div class="flash flash-error">Project not found</div>' + _render_projects_list()

        # Remove from config immediately (fast)
        updated = [p for p in settings.projects if p.id != project_id]
        _save_projects_to_toml(updated)
        get_owner().update_projects(updated)
        log_activity("info", "config", f"Project removed: {project_id}")
        _restart_watcher_if_running()

        # Delete indexed data in background (slow for large projects)
        def _bg_delete(pid):
            try:
                owner = get_owner()
                result = owner.delete_project_data(pid)
                files = result.get("files_deleted", 0)
                log_activity("warning", "config", f"Project data cleaned: {pid} ({files} files deleted)")
            except Exception as e:
                log_activity("error", "config", f"Failed to clean project data {pid}: {e}")

        threading.Timer(1.0, _bg_delete, args=[project_id]).start()

        return _render_projects_list()
    except Exception as e:
        detail = getattr(e, "detail", str(e))
        return f'<div class="flash flash-error">{escape(str(detail))}</div>' + _render_projects_list()


# --- Activity log fragment ---


@page_router.get("/ui/activity", response_class=HTMLResponse)
def ui_activity(after: int = Query(0)):
    """Activity log HTML fragment for the bottom drawer."""
    from ragtools.service.activity import activity_log
    events = activity_log.get_recent(limit=50, after_id=after)

    if not events:
        return '<div class="activity-empty">No recent activity</div>'

    rows = []
    latest_id = events[-1].id if events else 0
    for e in reversed(events):  # newest first
        level_class = {
            "info": "badge-accent", "success": "badge-success",
            "warning": "badge-warning", "error": "badge-danger",
        }.get(e.level, "badge-accent")

        detail_html = ""
        if e.details:
            detail_html = f'<div class="activity-details">{escape(e.details)}</div>'

        rows.append(f"""
        <div class="activity-event">
            <span class="activity-time">{escape(e.timestamp[11:19])}</span>
            <span class="badge {level_class}" style="font-size:10px;">{escape(e.level)}</span>
            <span class="activity-source">{escape(e.source)}</span>
            <span class="activity-msg">{escape(e.message)}</span>
            {detail_html}
        </div>
        """)

    # Store latest ID for next poll
    return f'<div data-latest-id="{latest_id}">' + "".join(rows) + "</div>"


# --- Config save fragment ---


@page_router.put("/ui/config/save", response_class=HTMLResponse)
def ui_config_save(
    chunk_size: int = Form(None),
    chunk_overlap: int = Form(None),
    top_k: int = Form(None),
    score_threshold: float = Form(None),
    service_port: int = Form(None),
    log_level: str = Form(None),
    startup_open_browser: str = Form(None),
    startup_delay: int = Form(None),
):
    """Save general settings via the UI."""
    try:
        settings = get_settings()
        payload = {}
        if chunk_size is not None:
            payload["chunk_size"] = chunk_size
        if chunk_overlap is not None:
            payload["chunk_overlap"] = chunk_overlap
        if top_k is not None:
            payload["top_k"] = top_k
        if score_threshold is not None:
            payload["score_threshold"] = score_threshold
        if service_port is not None:
            payload["service_port"] = service_port
        if log_level is not None and log_level.strip():
            payload["log_level"] = log_level.strip()

        # Startup settings — save to TOML [startup] section
        startup_changed = False
        open_browser = startup_open_browser == "true"
        if startup_delay is not None or startup_open_browser is not None:
            _update_toml_config("startup", {
                "open_browser": open_browser,
                "delay": startup_delay or 30,
            })
            object.__setattr__(settings, "startup_open_browser", open_browser)
            if startup_delay is not None:
                object.__setattr__(settings, "startup_delay", startup_delay)
            startup_changed = True

        if not payload and not startup_changed:
            return '<div class="flash flash-success">No changes to save.</div>'

        # Save main settings via API
        if payload:
            from ragtools.service.routes import update_config, ConfigUpdateRequest
            req = ConfigUpdateRequest(**payload)
            result = update_config(req)
            saved_keys = result["updated"]
            restart = result["restart_required"]
        else:
            saved_keys = []
            restart = False

        if startup_changed:
            saved_keys.extend(["startup_open_browser", "startup_delay"])

        msg = f'Saved: {", ".join(saved_keys)}'
        if restart:
            msg += ' <span class="badge badge-warning" style="margin-left:6px;">Restart required</span>'

        return f'<div class="flash flash-success">{msg}</div>'
    except Exception as e:
        detail = getattr(e, "detail", str(e))
        return f'<div class="flash flash-error">Save failed: {escape(str(detail))}</div>'


# --- Helpers ---


def _update_toml_config(section: str | None, data: dict) -> None:
    """Update the TOML config file. If section is None, update root level."""
    import tomli_w
    from ragtools.config import get_config_write_path

    config_path = get_config_write_path()

    existing = {}
    if config_path.exists():
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib
        with open(config_path, "rb") as f:
            existing = tomllib.load(f)

    existing.setdefault("version", 1)

    if section is None:
        existing.update(data)
    else:
        existing.setdefault(section, {})
        existing[section].update(data)

    with open(config_path, "wb") as f:
        tomli_w.dump(existing, f)

    logger.info("Config updated: section=%s, keys=%s", section or "root", list(data.keys()))
    from ragtools.service.activity import log_activity
    log_activity("info", "config", f"Config saved: {section or 'general'} ({', '.join(data.keys())})")


def _save_projects_to_toml(projects: list) -> None:
    """Write the full projects list to TOML config, setting version=2.

    Writes the entire [[projects]] array atomically (not merged key-by-key).
    """
    import tomli_w
    from ragtools.config import get_config_write_path

    config_path = get_config_write_path()

    existing = {}
    if config_path.exists():
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib
        with open(config_path, "rb") as f:
            existing = tomllib.load(f)

    existing["version"] = 2
    existing["projects"] = [
        {
            "id": p.id,
            "name": p.name,
            "path": p.path,
            "enabled": p.enabled,
            "ignore_patterns": p.ignore_patterns,
        }
        for p in projects
    ]
    # Remove legacy content_root if upgrading
    existing.pop("content_root", None)

    with open(config_path, "wb") as f:
        tomli_w.dump(existing, f)

    logger.info("Projects saved: %d projects to TOML", len(projects))
    from ragtools.service.activity import log_activity
    log_activity("info", "config", f"Projects saved: {len(projects)} projects")
