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


@page_router.get("/index", response_class=HTMLResponse)
def index_page(request: Request):
    return templates.TemplateResponse(request, "index.html", {"page": "index"})


@page_router.get("/ignore", response_class=HTMLResponse)
def ignore_page(request: Request):
    return templates.TemplateResponse(request, "ignore.html", {"page": "ignore"})


@page_router.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    return templates.TemplateResponse(request, "config.html", {"page": "config"})


@page_router.get("/startup", response_class=HTMLResponse)
def startup_page(request: Request):
    return templates.TemplateResponse(request, "startup.html", {"page": "startup"})


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
    """Projects table fragment."""
    owner = get_owner()
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
    """Watcher status fragment with start/stop button."""
    from ragtools.service.routes import _watcher_thread, _watcher_lock
    with _watcher_lock:
        running = _watcher_thread is not None and _watcher_thread.is_alive()

    if running:
        return """
        <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
            <span>Status:</span> <span class="badge badge-success">Running</span>
            <button class="btn btn-secondary btn-sm" hx-post="/ui/watcher/toggle" hx-target="#watcher-area" hx-swap="innerHTML"
                    hx-confirm="Stop the file watcher?">Stop Watcher</button>
        </div>
        """
    else:
        return """
        <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
            <span>Status:</span> <span class="badge badge-muted">Stopped</span>
            <button class="btn btn-primary btn-sm" hx-post="/ui/watcher/toggle" hx-target="#watcher-area" hx-swap="innerHTML">Start Watcher</button>
        </div>
        """


@page_router.post("/ui/watcher/toggle", response_class=HTMLResponse)
def ui_watcher_toggle():
    """Toggle watcher and return updated fragment."""
    from ragtools.service.routes import _watcher_thread, _watcher_lock, watcher_start, watcher_stop
    with _watcher_lock:
        running = _watcher_thread is not None and _watcher_thread.is_alive()

    if running:
        watcher_stop()
    else:
        watcher_start()

    return ui_watcher()


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


@page_router.get("/ui/ignore/builtin", response_class=HTMLResponse)
def ui_ignore_builtin():
    """Built-in ignore patterns fragment."""
    owner = get_owner()
    patterns = owner.ignore_rules.get_all_patterns()
    return escape("\n".join(patterns["built-in"]))


@page_router.get("/ui/ignore/config", response_class=HTMLResponse)
def ui_ignore_config():
    """Config ignore patterns for textarea."""
    owner = get_owner()
    patterns = owner.ignore_rules.get_all_patterns()
    return "\n".join(patterns["config"])


@page_router.get("/ui/ignore/ragignore", response_class=HTMLResponse)
def ui_ignore_ragignore():
    """List .ragignore files found."""
    owner = get_owner()
    patterns = owner.ignore_rules.get_all_patterns()
    ragignore_files = patterns.get("ragignore_files", {})
    if not ragignore_files:
        return '<p style="color: var(--color-text-muted);">No .ragignore files found.</p>'

    html = ""
    for filepath, rules in ragignore_files.items():
        rules_str = escape("\n".join(rules))
        html += f"<details><summary><code>{escape(filepath)}</code></summary><pre>{rules_str}</pre></details>"
    return html


@page_router.put("/ui/ignore/save", response_class=HTMLResponse)
def ui_ignore_save(
    patterns: str = Form(""),
    use_ragignore: bool = Form(False),
):
    """Save ignore patterns to config file."""
    try:
        pattern_list = [line.strip() for line in patterns.splitlines() if line.strip()]
        settings = get_settings()

        _save_ignore_config(settings, pattern_list, use_ragignore)

        owner = get_owner()
        from ragtools.ignore import IgnoreRules
        owner._ignore_rules = IgnoreRules(
            content_root=settings.content_root,
            global_patterns=pattern_list,
            use_ragignore=use_ragignore,
        )

        return f'<div class="flash flash-success">Saved {len(pattern_list)} pattern(s).</div>'
    except Exception as e:
        return f'<div class="flash flash-error">Save failed: {escape(str(e))}</div>'


@page_router.post("/ui/ignore/test", response_class=HTMLResponse)
def ui_ignore_test(path: str = Form("")):
    """Test if a path would be ignored."""
    if not path.strip():
        return '<p style="color: var(--color-text-muted);">Enter a path to test.</p>'
    owner = get_owner()
    reason = owner.ignore_rules.get_reason(Path(path.strip()))
    if reason:
        return f'<p><span class="badge badge-danger">IGNORED</span> &mdash; {escape(reason)}</p>'
    else:
        return '<p><span class="badge badge-success">NOT IGNORED</span> &mdash; this file would be indexed</p>'


@page_router.get("/ui/config", response_class=HTMLResponse)
def ui_config():
    """Config display fragment."""
    settings = get_settings()
    groups = {
        "Indexing": {
            "Content root": settings.content_root,
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


@page_router.get("/ui/startup/status", response_class=HTMLResponse)
def ui_startup_status():
    """Startup registration status fragment."""
    from ragtools.service.startup import is_task_installed, get_task_info

    installed = is_task_installed()
    if installed:
        info = get_task_info()
        details = ""
        if info:
            details = f"""
            <table class="table-clean" style="margin-top:10px;">
                <tr><td>Status</td><td>{escape(str(info.get('status', 'Unknown')))}</td></tr>
                <tr><td>Last run</td><td>{escape(str(info.get('last_run', 'Never')))}</td></tr>
                <tr><td>Next run</td><td>{escape(str(info.get('next_run', 'N/A')))}</td></tr>
            </table>
            """
        return f"""
        <div style="display:flex; align-items:center; gap:12px; margin-bottom:12px;">
            <span>Startup task:</span> <span class="badge badge-success">Installed</span>
        </div>
        {details}
        <button class="btn btn-secondary btn-sm" hx-post="/ui/startup/toggle" hx-target="#startup-status" hx-swap="innerHTML"
                hx-confirm="Remove automatic startup?">Uninstall Startup Task</button>
        """
    else:
        return """
        <div style="display:flex; align-items:center; gap:12px; margin-bottom:12px;">
            <span>Startup task:</span> <span class="badge badge-muted">Not installed</span>
        </div>
        <p style="font-size:13px; color:var(--color-text-secondary);">The service will not start automatically on Windows login.</p>
        <button class="btn btn-primary btn-sm" hx-post="/ui/startup/toggle" hx-target="#startup-status" hx-swap="innerHTML">
            Install Startup Task</button>
        """


@page_router.post("/ui/startup/toggle", response_class=HTMLResponse)
def ui_startup_toggle():
    """Toggle startup task install/uninstall."""
    from ragtools.service.startup import is_task_installed, install_task, uninstall_task

    if is_task_installed():
        uninstall_task()
    else:
        settings = get_settings()
        try:
            install_task(settings, delay_seconds=settings.startup_delay)
        except RuntimeError as e:
            return f'<div class="flash flash-error">Install failed: {escape(str(e))}</div>'

    return ui_startup_status()


@page_router.put("/ui/startup/save", response_class=HTMLResponse)
def ui_startup_save(
    startup_watcher: bool = Form(False),
    startup_open_browser: bool = Form(False),
    startup_delay: int = Form(30),
):
    """Save startup behavior settings to config."""
    try:
        settings = get_settings()
        _save_startup_config(settings, startup_watcher, startup_open_browser, startup_delay)

        from ragtools.service.startup import is_task_installed, install_task
        if is_task_installed():
            install_task(settings, delay_seconds=startup_delay)

        return '<div class="flash flash-success">Startup settings saved.</div>'
    except Exception as e:
        return f'<div class="flash flash-error">Save failed: {escape(str(e))}</div>'


# --- Config save fragment ---


@page_router.put("/ui/config/save", response_class=HTMLResponse)
def ui_config_save(
    chunk_size: int = Form(None),
    chunk_overlap: int = Form(None),
    content_root: str = Form(None),
    top_k: int = Form(None),
    score_threshold: float = Form(None),
    service_port: int = Form(None),
    log_level: str = Form(None),
):
    """Save general settings via the UI."""
    try:
        import httpx
        settings = get_settings()
        payload = {}
        if chunk_size is not None:
            payload["chunk_size"] = chunk_size
        if chunk_overlap is not None:
            payload["chunk_overlap"] = chunk_overlap
        if content_root is not None and content_root.strip():
            payload["content_root"] = content_root.strip()
        if top_k is not None:
            payload["top_k"] = top_k
        if score_threshold is not None:
            payload["score_threshold"] = score_threshold
        if service_port is not None:
            payload["service_port"] = service_port
        if log_level is not None and log_level.strip():
            payload["log_level"] = log_level.strip()

        if not payload:
            return '<div class="flash flash-success">No changes to save.</div>'

        # Call the API endpoint directly (internal)
        from ragtools.service.routes import update_config, ConfigUpdateRequest
        req = ConfigUpdateRequest(**payload)
        result = update_config(req)

        msg = f'Saved: {", ".join(result["updated"])}'
        if result["restart_required"]:
            msg += ' <span class="badge badge-warning" style="margin-left:6px;">Restart required</span>'

        return f'<div class="flash flash-success">{msg}</div>'
    except Exception as e:
        detail = str(e)
        if hasattr(e, 'detail'):
            detail = e.detail
        return f'<div class="flash flash-error">Save failed: {escape(str(detail))}</div>'


# --- Helpers ---


def _update_toml_config(section: str | None, data: dict) -> None:
    """Update the TOML config file. If section is None, update root level."""
    import os
    import tomli_w
    from ragtools.config import _find_config_path

    # Check explicit override first (for both reading and writing)
    explicit = os.environ.get("RAG_CONFIG_PATH")
    if explicit:
        config_path = Path(explicit)
    else:
        config_path = _find_config_path()
        if config_path is None:
            config_path = Path("ragtools.toml")

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


def _save_startup_config(settings, watcher: bool, open_browser: bool, delay: int) -> None:
    """Write startup settings to the TOML config file."""
    _update_toml_config("startup", {
        "watcher": watcher,
        "open_browser": open_browser,
        "delay": delay,
    })


def _save_ignore_config(settings, patterns: list[str], use_ragignore: bool) -> None:
    """Write ignore patterns to the TOML config file."""
    _update_toml_config("ignore", {
        "patterns": patterns,
        "use_ragignore_files": use_ragignore,
    })
