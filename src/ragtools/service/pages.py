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
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _asset_url(path: str) -> str:
    """Cache-busting URL for a static asset: appends ``?v=<mtime>`` so browsers
    refetch the file whenever it changes on disk (a CSS edit, or an upgrade)
    instead of serving a stale cached copy. Falls back to the bare path if the
    file can't be stat'd."""
    rel = path.split("/static/", 1)[-1] if "/static/" in path else path.lstrip("/")
    try:
        mtime = int((STATIC_DIR / rel).stat().st_mtime)
        return f"{path}?v={mtime}"
    except OSError:
        return path


templates.env.globals["asset_url"] = _asset_url

page_router = APIRouter()


# --- Helpers ---


#: Activity level -> badge class. One table, used by both the dashboard digest
#: and the activity drawer, so the two can never drift apart.
_LEVEL_BADGE = {
    "info": "badge-info",
    "success": "badge-success",
    "warning": "badge-warning",
    "error": "badge-danger",
}


def _collection_display() -> str:
    """What to SHOW as the knowledge base's identity — never a name to query.

    The diagnostics identity card and the config card both printed
    ``settings.collection_name``. Under ``per_project`` no collection carries
    that name (15 ``proj_<uuid>`` collections on the installed v3.4 machine),
    so both cards named something Qdrant does not have. Ask the router.
    """
    try:
        return get_owner().router.display_name()
    except Exception:  # noqa: BLE001 — a label must never fail a page render
        return ""


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


@page_router.get("/diagnostics", response_class=HTMLResponse)
def diagnostics_page(request: Request):
    """Service identity + storage diagnostics (surfaces the /identity data, S16)."""
    import os

    from ragtools import __version__
    from ragtools.service.identity import API_VERSION

    try:
        settings = get_owner().settings
    except RuntimeError:
        return templates.TemplateResponse(
            request, "diagnostics.html", {"page": "diagnostics", "ready": False}
        )

    owner = get_owner()
    try:
        storage = owner.storage_info()
    except Exception:
        storage = {"backend": "embedded", "engine_version": None, "hnsw": False,
                   "payload_indexes": False, "concurrent_readers": False}

    try:
        status = owner.get_status()
        collections = status.get("collections", [])
        points_total = status.get("points_count", 0)
    except Exception:
        collections, points_total = [], 0

    return templates.TemplateResponse(
        request,
        "diagnostics.html",
        {
            "page": "diagnostics",
            "ready": True,
            "version": __version__,
            "api_version": API_VERSION,
            "profile": os.environ.get("RAG_PROFILE", "installed"),
            # The identity card claims this row is what tells two services on
            # one machine apart. Under `per_project` the configured name is not
            # a collection at all — the Collections card immediately below
            # would list 15 `proj_<uuid>` names and none of them this one.
            "collection": _collection_display(),
            "data_dir": str(settings.data_dir),
            "storage_mode": storage.get("backend", "embedded"),
            "storage": storage,
            "collection_strategy": owner.router.strategy,
            "collections": collections,
            "points_total": points_total,
            "bound_host": settings.service_host,
            "bound_port": settings.service_port,
        },
    )


# --- Dashboard fragments ---


# Plain-language headline for each degraded signal. The detailed sentence
# underneath comes from the pure `compute_*` helpers (shared with the logs and
# `rag doctor`); this table exists so the panel never shows the reader a raw
# enum like `scale_over`, which is a system identifier, not English.
_ISSUE_HEADLINES = {
    "watcher_not_running": "File changes are not being picked up",
    "scale_approaching": "The index is nearing what this storage engine handles well",
    "scale_warn": "The index is nearing what this storage engine handles well",
    "scale_over": "The index is larger than this storage engine handles well",
    "index_stale": "Search results may be out of date",
    # The state the dashboard could not express. It rendered `total_chunks` from
    # the state DB — "6,546 files · 91,516 chunks" — while every collection in
    # the live store held zero points, so the page looked healthy at the exact
    # moment nothing was searchable.
    "index_rebuilding": "Search is unavailable — the index is being rebuilt",
    "index_blocked": "Search is unavailable — the rebuild has stopped",
    "index_empty": "Nothing is indexed yet",
    "index_partial": "Search is unavailable — the index is incomplete",
    "storage_unavailable": "Storage is not responding",
    "service_starting": "The service is still starting",
}

#: How each project state from :data:`ragtools.service.owner.PROJECT_STATES`
#: reads, and how urgent it looks. The vocabulary is the owner's; only the
#: wording and the badge colour are decided here. Four of these used to render
#: as the single string "Not indexed yet".
_PROJECT_STATE_BADGE = {
    "disabled": ("badge-muted", "Disabled"),
    "path_missing": ("badge-danger", "Folder missing"),
    "never_indexed": ("badge-warning", "Not indexed yet"),
    "indexed": ("badge-success", "Indexed"),
    "indexed_stale": ("badge-warning", "Indexed (stale)"),
    "no_eligible_files": ("badge-muted", "Nothing to index"),
    "failed": ("badge-danger", "Rebuild failed"),
    "storage_unavailable": ("badge-muted", "Unknown"),
    "drifted": ("badge-danger", "Index missing"),
}

#: Tile tooltips. Each says which SOURCE its number comes from, because the row
#: mixes two: the live vector store and the state DB's record of the last index.
_LIVE_HINT = "Vectors the store can answer a search from right now"
_FILES_HINT = "Files the last index recorded in the state database"
_CHUNKS_HINT = ("Chunks the last index recorded in the state database — what was "
                "written, not what the store holds now")
_CONFIGURED_HINT = "Projects in your configuration"
_INDEXED_HINT = "Configured projects with at least one file in the index"


def _stat_tile(value, label: str, hint: str) -> str:
    """One dashboard tile. ``None`` renders as UNKNOWN, never as zero.

    ``_count_points`` returns ``None`` when it could not ask, and that
    distinction has to survive the last inch to the screen: a dead engine
    rendering a confident ``0`` beside a state DB reporting 145,906 chunks is
    the exact failure the ``None`` exists to prevent.
    """
    if value is None:
        return (f'<div class="dash-stat" title="{escape(hint)}">'
                f'<strong>—</strong> <span>{escape(label)} (unknown)</span></div>')
    return (f'<div class="dash-stat" title="{escape(hint)}">'
            f'<strong>{value:,}</strong> <span>{escape(label)}</span></div>')


def _status_row(*, live, files, chunks, configured, indexed, searchable=None,
                chunks_label="chunks indexed", availability="", degraded=False,
                badges="", banner="") -> str:
    """The dashboard's vitals row — the ONE definition of these tiles.

    Both callers render through it (the live status and the not-ready state), so
    the labels cannot drift apart and a reader is never shown the same word for
    two different quantities depending on which path produced the page.
    """
    hint = _INDEXED_HINT
    if searchable is not None:
        hint += f" — {searchable:,} of them hold live vectors"
    return f"""
    <div class="dash-status-row" data-degraded="{str(degraded).lower()}"
         data-availability="{escape(availability)}">
        {_stat_tile(live, "searchable", _LIVE_HINT)}
        {_stat_tile(files, "files indexed", _FILES_HINT)}
        {_stat_tile(chunks, chunks_label, _CHUNKS_HINT)}
        {_stat_tile(configured, "projects configured", _CONFIGURED_HINT)}
        {_stat_tile(indexed, "projects indexed", hint)}
        <div class="dash-status-badges">{badges}</div>
    </div>
    {banner}
    """


def _issue_banner(issues: list[tuple[str, str]]) -> str:
    """The degraded banner. Empty string when there is nothing to say."""
    if not issues:
        return ""
    items = "".join(
        "<li><strong>{}</strong>{}</li>".format(
            escape(_ISSUE_HEADLINES.get(key, key)),
            f" — {escape(detail)}" if detail else "",
        )
        for key, detail in issues
    )
    keys = escape(",".join(key for key, _ in issues))
    return (f'<div class="dash-degraded" role="status" data-issues="{keys}">'
            f'<span aria-hidden="true">⚠</span><ul>{items}</ul>'
            "</div>")


def _configured_project_count() -> int | None:
    """How many projects the CONFIGURATION declares — no engine required.

    Asked when the service singleton is not up yet. "How many projects exist" is
    a property of the config file, so it stays answerable while every number
    that depends on the store is honestly unknown.
    """
    try:
        return len(get_settings().projects or [])
    except Exception:  # noqa: BLE001 — not initialised; read the config itself
        pass
    try:
        from ragtools.config import Settings

        return len(Settings().projects or [])
    except Exception:  # noqa: BLE001 — unreadable config: unknown, not zero
        return None

#: How each availability verdict renders. `None` means "nothing to say".
_AVAILABILITY_ISSUE = {
    "rebuilding": ("index_rebuilding", None),
    "blocked": ("index_blocked", None),
    "empty": ("index_empty",
              "Add a project and run an index, and its files become searchable."),
    "partial_unavailable": ("index_partial", None),
    "storage_unavailable": ("storage_unavailable",
                            "The vector store could not be reached, so the "
                            "counts below are the last known values."),
}


@page_router.get("/ui/dash/status", response_class=HTMLResponse)
def ui_dash_status():
    """Index vitals + honest degraded state for the dashboard."""
    try:
        owner = get_owner()
    except Exception:  # noqa: BLE001 — the engine is not up yet
        # A 500 here renders as an error toast over an empty card and tells the
        # reader nothing. The row still says what is knowable without the store
        # — how many projects are CONFIGURED — and reports the rest as unknown
        # rather than as zero.
        return _status_row(
            live=None, files=None, chunks=None,
            configured=_configured_project_count(), indexed=None,
            degraded=True, availability="",
            banner=_issue_banner([(
                "service_starting",
                "The index counts appear once the engine has finished starting.",
            )]),
        )
    s = owner.get_status()
    from ragtools.service.routes import _watcher_thread, _watcher_lock
    with _watcher_lock:
        watcher_running = _watcher_thread is not None and _watcher_thread.is_alive()

    # Honest watcher state. The old label said "Watcher starting" for ANY
    # non-running state, so a permanently failed watcher read as perpetually
    # starting. `desired_run` distinguishes "the user stopped it" from "it died".
    from ragtools.service.routes import _watcher_desired_run
    if watcher_running:
        watcher_badge = '<span class="badge badge-success badge-dot">Watching for changes</span>'
    elif _watcher_desired_run:
        watcher_badge = '<span class="badge badge-danger badge-dot">Watcher stopped unexpectedly</span>'
    else:
        watcher_badge = '<span class="badge badge-muted badge-dot">Watcher off</span>'

    # Surface the degraded signal /health has always computed but the UI never
    # rendered (master plan Phase 1 / G1).
    issues: list[tuple[str, str]] = []   # (key, detail sentence)
    if not watcher_running and _watcher_desired_run:
        issues.append(("watcher_not_running",
                       "Edits to your project folders will not appear in search until "
                       "the service is restarted or the index is rebuilt."))

    scale = (s.get("scale") or {})
    # `compute_scale_warning` emits levels ok | approaching | over. The previous
    # check only matched ("warn", "over"), so the 15k-20k soft warning was
    # computed on every request and then silently discarded. "warn" is kept as
    # an accepted alias so an older payload still surfaces.
    if scale.get("level") in ("approaching", "warn", "over"):
        issues.append((f"scale_{scale.get('level')}", scale.get("message", "")))

    fresh = (s.get("freshness") or {})
    if fresh.get("level") == "stale":
        issues.append(("index_stale", fresh.get("message", "")))

    # THE HEADLINE STATE, and it goes first because it is the one that decides
    # whether anything below is worth reading.
    availability = s.get("index_availability") or ""
    migration = s.get("migration") or {}
    mapped = _AVAILABILITY_ISSUE.get(availability)
    if mapped is not None:
        key, detail = mapped
        if migration and availability in ("rebuilding", "blocked"):
            done, total = migration.get("done", 0), migration.get("total", 0)
            detail = (f"{done} of {total} projects rebuilt so far. The counts "
                      f"below describe the index from BEFORE the rebuild.")
            activity = s.get("index_activity") or {}
            if activity.get("phase"):
                detail += (f" Currently {activity['phase']}"
                           f" ({activity.get('done', 0)}/"
                           f"{activity.get('total', 0)}).")
            if availability == "blocked":
                recorded = migration.get("blocked_reason_recorded") or ""
                if recorded:
                    # Labelled as recorded. Presenting a hours-old error as
                    # current state is how a status page loses its credibility.
                    detail += f" Last recorded reason: {recorded}"
        issues.insert(0, (key, detail or ""))

    degraded = bool(issues)
    banner = _issue_banner(issues)

    files = s.get("total_files", 0)
    chunks = s.get("total_chunks", 0)
    # TWO QUESTIONS, TWO TILES, EACH LABELLED WITH THE ONE IT ANSWERS.
    #
    # `len(s["projects"])` is "projects with at least one indexed FILE". It was
    # rendered under the bare word "projects" directly above a table iterating
    # the CONFIGURED ones, so the installed machine showed 14 over a list of 15
    # and both numbers were right. The gap between them is the signal — a
    # project that exists and holds nothing — so the page states both rather
    # than picking one and calling it "projects".
    projects_indexed = s.get("projects_indexed")
    if projects_indexed is None:
        projects_indexed = len(s.get("projects") or [])
    projects_configured = s.get("projects_configured")
    if projects_configured is None:
        projects_configured = projects_indexed

    # A snapshot served while indexing holds the lock — say so rather than
    # presenting stale counts as current.
    stale_note = ('<span class="badge badge-muted">Updating…</span>'
                  if s.get("stale") else "")

    # LIVE, beside the bookkeeping. `total_chunks` comes from the state DB and
    # can describe an index that no longer exists — during the per-project
    # migration it described the OLD shared collection while every new
    # collection held nothing. Showing only that number is what made an empty,
    # unsearchable install look like a healthy one.
    live = s.get("live_points", s.get("points_count"))

    # ONE QUANTITY MUST NOT APPEAR TWICE UNDER TWO LABELS. `searchable` (live
    # vectors) and `chunks` (the state DB's total) rendered as 26,713 twice on
    # the installed machine, because on a healthy index they agree — which is
    # exactly why presenting both as independent facts is misleading. The
    # recorded pair says it is recorded; when it disagrees with the live count
    # it is labelled rather than dropped, since it is still the truth about
    # something — just not about what you can search.
    chunks_label = ("chunks (before rebuild)"
                    if (live is not None and live == 0 and chunks)
                    else "chunks indexed")

    return _status_row(
        live=live, files=files, chunks=chunks,
        configured=projects_configured, indexed=projects_indexed,
        searchable=s.get("projects_searchable"), chunks_label=chunks_label,
        availability=availability, degraded=degraded,
        badges=f"{stale_note}{watcher_badge}", banner=banner,
    )


@page_router.get("/ui/dash/projects", response_class=HTMLResponse)
def ui_dash_projects():
    """Projects card for dashboard — shows empty state if no projects."""
    settings = get_settings()

    if not settings.projects:
        return """
        <div class="card">
            <div class="empty-state">
                <p class="empty-state-title">No projects yet</p>
                <p class="empty-state-body">
                    Point RAG Tools at a folder and it will index the files in place,
                    so Claude can search them.
                </p>
                <a href="/projects" class="btn btn-primary">Add your first project</a>
            </div>
        </div>
        """

    # ONE ROW PER CONFIGURED PROJECT, each carrying the state that names its
    # remedy. `Not indexed yet` used to render identically for a project that
    # was scanned and legitimately had nothing, one whose folder had been moved,
    # one that was switched off, and one whose rebuild had FAILED — four causes,
    # four remedies, one string.
    try:
        states = get_owner().get_status_projects()
    except Exception as exc:  # noqa: BLE001 — the card must render before the
        # engine is up; it just must not claim the index is empty while it waits.
        logger.debug("project states unavailable (non-fatal): %s", exc)
        states = [
            {"id": p.id, "name": p.name, "enabled": p.enabled,
             "files": 0, "chunks": 0, "points": None,
             "state": ("disabled" if not p.enabled else "storage_unavailable"),
             "reason": "The service is still starting, so this project's index "
                       "state is not known yet."}
            for p in settings.projects
        ]

    rows = ""
    for row in states:
        cls, label = _PROJECT_STATE_BADGE.get(
            row.get("state"), ("badge-muted", str(row.get("state") or "unknown")))
        badge = f'<span class="badge {cls}">{escape(label)}</span>'
        reason = escape(row.get("reason") or "")
        if row.get("state") in ("indexed", "indexed_stale"):
            # The counts stay the headline for a healthy project; the reason
            # (why it is stale) rides along as the tooltip rather than pushing
            # the numbers off the row.
            title = f' title="{reason}"' if reason else ""
            info = (f'<span{title}>{row.get("files", 0):,} files '
                    f'&middot; {row.get("chunks", 0):,} chunks</span>')
        elif reason:
            info = f'<span class="cell-empty">{reason}</span>'
        else:
            info = '<span class="cell-empty">—</span>'
        rows += (f'<tr data-state="{escape(str(row.get("state") or ""))}">'
                 f'<td class="cell-title">{escape(row.get("name") or row.get("id", ""))}</td>'
                 f'<td>{badge}</td><td>{info}</td></tr>')

    return f"""
    <div class="card">
        <div class="card-header">Projects</div>
        <div class="table-wrap">
            <table class="table-clean">
                <thead><tr><th>Project</th><th>Status</th><th>Indexed</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </div>
    """


@page_router.get("/ui/dash/activity", response_class=HTMLResponse)
def ui_dash_activity():
    """Inline recent activity for dashboard — last 5 events."""
    from ragtools.service.activity import activity_log
    events = activity_log.get_recent(limit=5)

    if not events:
        return ('<p class="activity-empty">Nothing has happened yet. '
                'Indexing and configuration changes show up here.</p>')

    rows = []
    for e in reversed(events):
        rows.append(f"""
        <div class="activity-event">
            <span class="activity-time">{escape(e.timestamp[11:19])}</span>
            <span class="badge {_LEVEL_BADGE.get(e.level, 'badge-info')}">{escape(e.level)}</span>
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
    projects = escape(", ".join(s["projects"])) or '<span class="cell-empty">none</span>'
    return f"""
    <table class="kv-table">
        <tr><th scope="row">Total files</th><td>{s['total_files']:,}</td></tr>
        <tr><th scope="row">Total chunks</th><td>{s['total_chunks']:,}</td></tr>
        <tr><th scope="row">Points</th><td>{s['points_count']:,}</td></tr>
        <tr><th scope="row">Collection</th><td><code>{escape(s['collection_name'])}</code></td></tr>
        <tr><th scope="row">Projects</th><td>{projects}</td></tr>
        <tr><th scope="row">Last indexed</th><td>{escape(s['last_indexed'] or 'never')}</td></tr>
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
            return ('<p class="cell-empty">No projects configured. '
                    '<a href="/projects">Add a project</a></p>')

        rows = ""
        for p in settings.projects:
            idx = index_data.get(p.id, {"files": 0, "chunks": 0})
            badge = ('<span class="badge badge-success">Enabled</span>' if p.enabled
                     else '<span class="badge badge-muted">Disabled</span>')
            if idx["files"] > 0:
                info = f'{idx["files"]:,} files &middot; {idx["chunks"]:,} chunks'
            else:
                info = '<span class="cell-empty">Not indexed yet</span>'
            rows += (f'<tr><td class="cell-title">{escape(p.name)}</td>'
                     f'<td>{badge}</td><td>{info}</td></tr>')

        return f"""
        <div class="table-wrap">
            <table class="table-clean">
                <thead><tr><th>Project</th><th>Status</th><th>Indexed</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
        <p><a href="/projects" class="btn btn-secondary btn-sm">Manage projects</a></p>
        """
    else:
        projects = owner.get_projects()
        if not projects:
            return '<p class="cell-empty">No projects indexed yet.</p>'
        rows = "".join(
            f'<tr><td class="cell-title">{escape(p["project_id"])}</td>'
            f'<td class="cell-num">{p["files"]:,}</td>'
            f'<td class="cell-num">{p["chunks"]:,}</td></tr>'
            for p in projects
        )
        return f"""
        <div class="table-wrap">
            <table class="table-clean">
                <thead><tr>
                    <th>Project</th><th class="cell-num">Files</th><th class="cell-num">Chunks</th>
                </tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
        """


@page_router.get("/ui/watcher", response_class=HTMLResponse)
def ui_watcher():
    """Watcher status fragment (always-on, informational only)."""
    from ragtools.service.routes import _watcher_thread, _watcher_lock
    with _watcher_lock:
        running = _watcher_thread is not None and _watcher_thread.is_alive()

    if running:
        return """
        <div class="inline-status">
            <span class="badge badge-success badge-dot">Running</span>
            <span class="muted">Watching your project folders for changes.</span>
        </div>
        """
    else:
        return """
        <div class="inline-status">
            <span class="badge badge-muted badge-dot">Starting…</span>
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
        return ('<div class="empty-state"><p class="empty-state-body">'
                "Enter a search query above.</p></div>")

    owner = get_owner()
    from ragtools.retrieval.scope import ScopeUnresolvedError

    try:
        data = owner.search_formatted(
            query=query.strip(),
            project_id=project if project else None,
            top_k=top_k,
        )
    except ScopeUnresolvedError:
        # Fail-closed scope (S1/A2): the panel must pick a project rather than
        # silently searching every one. Say what to do, not how the system is
        # built.
        return (
            '<div class="empty-state">'
            '<p class="empty-state-title">Select a project to search</p>'
            '<p class="empty-state-body">Searches run against one project at a '
            "time, so results always carry a source you can trace.</p></div>"
        )

    if data["count"] == 0:
        return (
            '<div class="empty-state">'
            '<p class="empty-state-title">No matches</p>'
            f'<p class="empty-state-body">Nothing in this project scored above the '
            f"threshold for <em>{escape(query)}</em>. Try broader wording, or lower the "
            "score threshold in Settings.</p></div>"
        )

    cards = []
    for r in data["results"]:
        headings = " › ".join(escape(h) for h in r["headings"]) if r["headings"] else ""
        confidence = r["confidence"].lower()
        badge_class = f"badge-{'success' if confidence == 'high' else 'warning' if confidence == 'moderate' else 'danger'}"
        text_preview = escape(r["text"][:300]) + ("…" if len(r["text"]) > 300 else "")
        heading_html = f"<span>{headings}</span>" if headings else ""
        cards.append(f"""
        <div class="result-card confidence-{confidence}">
            <div class="meta">
                <span class="badge {badge_class}">{escape(r['confidence'])}</span>
                <span class="result-score">{r['score']:.3f}</span>
                <span class="result-path">{escape(r['project_id'])}/{escape(r['file_path'])}</span>
                {heading_html}
            </div>
            <p class="text-preview">{text_preview}</p>
        </div>
        """)

    plural = "result" if data["count"] == 1 else "results"
    return (f'<p class="results-summary"><strong>{data["count"]}</strong> {plural} for '
            f"<em>{escape(query)}</em></p>") + "".join(cards)


@page_router.post("/ui/index", response_class=HTMLResponse)
def ui_index(full: bool = Query(False), request: Request = None):
    """Run index and return results fragment.

    Guarded like every other index write, and a refusal is rendered as a fragment
    the user can read — htmx does not swap a 4xx, so a status code alone would
    leave the button looking like it did nothing.
    """
    from ragtools.service import destructive

    owner = get_owner()
    try:
        with destructive.guarded(owner, "index", request=request):
            stats = owner.run_full_index() if full else owner.run_incremental_index()
    except destructive.OperationRefused as refused:
        return (f'<div class="flash flash-error">Indexing not started: '
                f'{escape(refused.reason)}</div>')
    if full:
        return f"""
        <div class="flash flash-success">
            Full index complete: {stats['files_indexed']} files, {stats['chunks_indexed']} chunks,
            projects: {', '.join(stats['projects']) or 'none'}
        </div>
        """
    return f"""
    <div class="flash flash-success">
        Incremental index: {stats['indexed']} indexed, {stats['skipped']} skipped,
        {stats['deleted']} deleted, {stats['chunks_indexed']} chunks
    </div>
    """


@page_router.post("/ui/rebuild", response_class=HTMLResponse)
def ui_rebuild(request: Request = None):
    """Rebuild and return results fragment.

    Guarded and handled. This route used to call ``owner.rebuild()`` bare: any
    exception escaped to Starlette and reached the user as
    ``Request failed (500) — Internal Server Error``, while ``/health`` was
    simultaneously reporting ``storage_unreachable`` — the service knew, and the
    button neither said so nor declined.

    The status stays 200 with an error fragment, matching every other handler in
    this module, because htmx does not swap a 4xx response and a refusal the user
    cannot see is worse than one they can. The machine-readable 409 lives on
    ``POST /api/rebuild``.
    """
    from ragtools.service import destructive

    owner = get_owner()
    try:
        with destructive.guarded(owner, "rebuild", request=request,
                                 subject="rebuild"):
            stats = owner.rebuild()
    except destructive.OperationRefused as refused:
        return (f'<div class="flash flash-error">Rebuild not started: '
                f'{escape(refused.reason)}</div>')
    except Exception as e:  # noqa: BLE001 — a failed rebuild is not a 500
        logger.exception("rebuild failed")
        return (f'<div class="flash flash-error">Rebuild failed: '
                f'{escape(str(e))}</div>')
    # A run with failures is NOT a green success. `Rebuild complete` rendered
    # success unconditionally — it said so for 0 files and 0 chunks while a
    # project had just been left with an empty collection.
    failed = stats.get("failed_projects") or []
    body = (f"Rebuild complete: {stats['files_indexed']} files, "
            f"{stats['chunks_indexed']} chunks, "
            f"projects: {', '.join(stats['projects']) or 'none'}")
    if failed:
        return (f'<div class="flash flash-error">Rebuild incomplete: '
                f'{len(failed)} project(s) failed and kept their previous index '
                f'({escape(", ".join(failed))}). Rebuilt: '
                f'{escape(", ".join(stats["projects"]) or "none")} — '
                f'{stats["files_indexed"]} files, {stats["chunks_indexed"]} '
                f'chunks.</div>')
    return f"""
    <div class="flash flash-success">
        {body}
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
            # Not `settings.collection_name`: on a per-project install this card
            # named the knowledge base after a collection Qdrant does not have.
            "Collection": _collection_display(),
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

# Inline SVG glyphs for the compact Projects-table action buttons (feather-style,
# 24x24 viewBox, currentColor stroke). aria-hidden so each button's own aria-label
# is the accessible name.
_ICON_EDIT = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M12 20h9"/>'
    '<path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>'
)
_ICON_POWER = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/>'
    '<line x1="12" y1="2" x2="12" y2="12"/></svg>'
)
_ICON_TRASH = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<polyline points="3 6 5 6 21 6"/>'
    '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
    '<line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>'
)

# Project Mode → display label. docs/code/general are the canonical stored values.
_MODE_LABELS = {"docs": "Docs", "code": "Code", "general": "General"}


def _mode_badge(mode: str) -> str:
    """Render the Project Mode badge (Docs / Code / General)."""
    label = _MODE_LABELS.get(mode, "Docs")
    return f'<span class="badge badge-accent" title="Mode: {label}">{label}</span>'


def _lines(text: str) -> list[str]:
    """Textarea -> list, dropping blanks and surrounding whitespace."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _form_text(value) -> str | None:
    """A submitted form value as text, or None when the field was not sent.

    These handlers are called directly as functions too (tests, internal reuse),
    and an unpassed parameter is then FastAPI's ``Form(...)`` sentinel object
    rather than a string. Treating that sentinel as content is how a caller that
    simply did not mention a field ends up erasing it.
    """
    return value if isinstance(value, str) else None



def _invalidating(html: str, resources=("projects", "status")):
    """Wrap a fragment so the client invalidates the named resources.

    Generalises the one `HX-Trigger: projectAdded` the codebase already emitted
    (and which nothing listened for) into a contract every mutating fragment
    uses. Same-tab convergence is then immediate, without waiting for the SSE
    round-trip.
    """
    import json as _json
    from fastapi.responses import HTMLResponse as _HR
    return _HR(content=html, headers={
        "HX-Trigger": _json.dumps({"rag-invalidate": {"resources": list(resources)}})
    })

def _render_projects_list() -> str:
    """Render the projects table HTML. Shared by all mutating project fragments."""
    settings = get_settings()
    if not settings.projects:
        return '''<div class="empty-state">
            <p class="empty-state-title">No projects yet</p>
            <p class="empty-state-body">Use <strong>Add project</strong> above to point RAG Tools
            at your first folder.</p>
        </div>'''

    index_data = _load_index_stats(settings)

    rows = ""
    for p in settings.projects:
        idx = index_data.get(p.id, {"files": 0, "chunks": 0})
        # Status = Enabled/Disabled only. Mode (Docs/Code/General) is a separate column.
        status_badge = ('<span class="badge badge-success">Enabled</span>' if p.enabled
                        else '<span class="badge badge-muted">Disabled</span>')
        mode_badge = _mode_badge(p.mode)
        # A linked dependency changes where a whole tree is searched from, so
        # it belongs on the row rather than only inside the edit form. Reads the
        # catalog links — the legacy `dependency_paths` is consumed at load, so
        # a badge driven by it would silently show nothing.
        deps = list(getattr(p, "dependencies", None) or [])
        if deps:
            names = [(settings.dependency(d).name if settings.dependency(d) else d)
                     for d in deps]
            mode_badge += (
                f' <span class="badge badge-muted" title="Shared dependencies: '
                f'{escape(", ".join(names))}">+{len(deps)} shared</span>'
            )
        files = f'{idx["files"]:,}' if idx["files"] > 0 else '<span class="cell-empty">—</span>'
        chunks = f'{idx["chunks"]:,}' if idx["chunks"] > 0 else '<span class="cell-empty">—</span>'
        toggle_label = "Disable" if p.enabled else "Enable"
        path_display = escape(p.path)
        if len(p.path) > 44:
            path_display = escape("…" + p.path[-43:])

        rows += f"""<tr id="project-row-{escape(p.id)}">
            <td>
                <span class="cell-title">{escape(p.name)}</span>
                <code class="cell-sub">{escape(p.id)}</code>
            </td>
            <td title="{escape(p.path)}"><code>{path_display}</code></td>
            <td>{status_badge}</td>
            <td>{mode_badge}</td>
            <td class="cell-num">{files}</td>
            <td class="cell-num">{chunks}</td>
            <td class="cell-actions">
                <span class="btn-icon-group">
                <button class="btn btn-icon btn-secondary" title="Edit" aria-label="Edit {escape(p.name)}"
                    hx-get="/ui/projects/{escape(p.id)}/edit" hx-target="#project-row-{escape(p.id)}" hx-swap="outerHTML"
                    hx-disabled-elt="this" hx-indicator="#projects-overlay">{_ICON_EDIT}</button>
                <button class="btn btn-icon btn-secondary" title="{toggle_label}" aria-label="{toggle_label} {escape(p.name)}"
                    hx-post="/ui/projects/{escape(p.id)}/toggle" hx-target="#projects-list" hx-swap="innerHTML"
                    hx-disabled-elt="this" hx-indicator="#projects-overlay">{_ICON_POWER}</button>
                <button class="btn btn-icon btn-danger" title="Remove" aria-label="Remove {escape(p.name)}"
                    hx-delete="/ui/projects/{escape(p.id)}/remove" hx-target="#projects-list" hx-swap="innerHTML"
                    hx-confirm="Remove '{escape(p.name)}' and delete its indexed data?"
                    hx-disabled-elt="this" hx-indicator="#projects-overlay">{_ICON_TRASH}</button>
                </span>
            </td>
        </tr>"""

    return f"""
    <div class="table-wrap">
        <table class="table-clean">
            <thead><tr>
                <th>Name</th><th>Path</th><th>Status</th><th>Mode</th>
                <th class="cell-num">Files</th><th class="cell-num">Chunks</th>
                <th class="cell-actions"><span class="sr-only">Actions</span></th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
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
    mode: str = Form("docs"),
    dependency_paths: str = Form(""),
    request: Request = None,
):
    """Add a new project via UI form."""
    try:
        from fastapi.responses import HTMLResponse as HR
        from ragtools.service.routes import project_create, ProjectCreateRequest
        patterns = [line.strip() for line in ignore_patterns.splitlines() if line.strip()]
        deps = _lines(_form_text(dependency_paths) or "")
        req = ProjectCreateRequest(id=id.strip().lower(), name=name.strip(), path=path.strip(),
                                   ignore_patterns=patterns, mode=mode,
                                   dependency_paths=deps)
        project_create(req, request)
        return _invalidating(_render_projects_list())
    except Exception as e:
        detail = getattr(e, "detail", str(e))
        return f'<div class="flash flash-error">Failed to add project: {escape(str(detail))}</div>' + _render_projects_list()


def _deps_field(project) -> str:
    """The 'Shared dependencies' selector for the project edit form.

    A multi-select over the catalog rather than a free-text path box. Typing a
    path per project made the same shared thing an invisible, repeated string:
    dedup only happened if two spellings happened to resolve to one build, and
    nothing listed what already existed. Selecting from a catalog makes "these
    two projects use the same Odoo" a fact you can see rather than infer.

    Entries this project *cannot* use are shown disabled WITH THE REASON, not
    hidden — a dependency that is a project's own root is legal elsewhere, and
    silently omitting it looks like it was never added.
    """
    from ragtools.dependency_catalog import check_link

    pid = escape(project.id)
    settings = get_settings()
    catalog = list(settings.dependencies)
    linked = set(getattr(project, "dependencies", None) or [])
    count = f' <span class="badge badge-accent">{len(linked)}</span>' if linked else ""

    if not catalog:
        body = ('<p class="hint">The catalog is empty. Add a shared dependency on the '
                '<a href="/dependencies">Shared dependencies</a> page, then select it here.</p>')
    else:
        rows = ""
        for entry in catalog:
            verdict = check_link(project, entry)
            usable = verdict.ok and entry.enabled
            checked = " checked" if entry.id in linked else ""
            disabled = "" if usable else " disabled"
            reason = verdict.reason if verdict.blocked else (
                "" if entry.enabled else "this dependency is disabled")
            why = (f'<span class="cell-sub">{escape(reason)}</span>') if reason else (
                f'<code class="cell-sub">{escape(_shorten(entry.path, 44))}</code>')
            rows += f'''<label class="check-row{"" if usable else " check-row-disabled"}">
                            <input type="checkbox" name="dependencies"
                                   value="{escape(entry.id)}"{checked}{disabled}>
                            <span>
                                <span class="check-title">{escape(entry.name)}</span>
                                {why}
                            </span>
                        </label>'''
        body = (f'<div class="check-list">{rows}</div>'
                '<p class="hint">Selected folders are indexed once into a shared collection '
                'and left out of this project\'s own. Manage the catalog on the '
                '<a href="/dependencies">Shared dependencies</a> page.</p>')

    return f'''<details class="disclosure-action"{" open" if linked else ""}>
                    <summary>Shared dependencies{count}</summary>
                    <div id="deps-select-{pid}">
                        <input type="hidden" name="deps_present" value="1">
                        {body}
                    </div>
                </details>'''


def _render_deps_check(inspection: dict) -> str:
    """Render the dry-run verdict for each declared dependency folder."""
    entries = inspection.get("entries", [])
    if not entries:
        return ('<p class="hint">No dependency folders declared — this project indexes '
                'everything under its own folder.</p>')

    rows = ""
    for e in entries:
        if not e.get("ok"):
            rows += f'''<tr>
                <td><code>{escape(e["declared"])}</code></td>
                <td colspan="3"><span class="badge badge-danger">Rejected</span>
                    <span class="cell-sub">{escape(e.get("problem", ""))}</span></td>
            </tr>'''
            continue
        name = e.get("framework") or "—"
        version = e.get("version") or ""
        edition = e.get("edition") or ""
        detail = " ".join(x for x in (version, edition) if x and x != "generic")
        detector = e.get("detector") or "generic"
        if e.get("exists"):
            shared = e.get("shared_with", 0)
            state = (f'<span class="badge badge-success">Already indexed</span> '
                     f'<span class="cell-sub">{e.get("points", 0):,} chunks, shared by '
                     f'{shared} project{"" if shared == 1 else "s"}</span>')
        else:
            state = ('<span class="badge badge-accent">New corpus</span> '
                     '<span class="cell-sub">will be indexed on save</span>')
        rows += f'''<tr>
            <td><code>{escape(e["declared"])}</code></td>
            <td>{escape(name)} <span class="cell-sub">{escape(detail)}</span></td>
            <td><span class="cell-sub">detected by {escape(detector)}</span></td>
            <td>{state}</td>
        </tr>'''

    return f'''<div class="table-wrap">
        <table class="table-clean">
            <thead><tr><th>Folder</th><th>Detected as</th><th>How</th><th>Status</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>'''


@page_router.get("/dependencies", response_class=HTMLResponse)
def dependencies_page(request: Request):
    """The shared-dependency catalog — declare once, select from any project."""
    return templates.TemplateResponse(request, "dependencies.html",
                                      {"page": "dependencies"})


@page_router.get("/ui/dependencies/list", response_class=HTMLResponse)
def ui_dependencies_list():
    """Catalog table: what exists, what it costs, who uses it."""
    try:
        from ragtools.service.routes import dependencies_list
        data = dependencies_list()
    except Exception as e:  # noqa: BLE001
        return f'<div class="flash flash-error">{escape(str(getattr(e, "detail", e)))}</div>'

    entries = data.get("dependencies", [])
    if not entries:
        return ('<div class="empty-state">'
                '<p class="empty-state-title">No shared dependencies yet</p>'
                '<p class="empty-state-body">Use <strong>Add dependency</strong> above to '
                'register a vendored framework once, then select it from the projects '
                'that use it.</p></div>')

    rows = ""
    for d in entries:
        if not d.get("exists"):
            status = ('<span class="badge badge-danger">Missing</span>'
                      f' <span class="cell-sub">{escape(d.get("problem", ""))}</span>')
        elif not d.get("enabled"):
            status = '<span class="badge badge-muted">Disabled</span>'
        elif not d.get("projects"):
            # Registered but unused: nothing has been indexed for it yet, which
            # is correct and worth saying so it does not read as a failure.
            status = ('<span class="badge badge-muted">Not used yet</span>'
                      ' <span class="cell-sub">select it on a project</span>')
        elif d.get("indexed"):
            status = '<span class="badge badge-success">Indexed</span>'
        else:
            status = ('<span class="badge badge-warning">Indexing…</span>'
                      ' <span class="cell-sub">not searchable yet</span>')

        detected = escape(d.get("framework") or "—")
        detail = " ".join(x for x in (d.get("version") or "",
                                      d.get("edition") or "") if x and x != "generic")
        users = ", ".join(d.get("projects") or []) or '<span class="cell-empty">—</span>'
        points = f'{d.get("points", 0):,}' if d.get("points") else '<span class="cell-empty">—</span>'
        cascade = " and remove it from " + ", ".join(d["projects"]) if d.get("projects") else ""
        rows += f"""<tr id="dep-row-{escape(d['id'])}">
            <td>
                <span class="cell-title">{escape(d.get('name', ''))}</span>
                <code class="cell-sub">{escape(d['id'])}</code>
            </td>
            <td title="{escape(d.get('path', ''))}"><code>{escape(_shorten(d.get('path', ''), 38))}</code></td>
            <td>{detected} <span class="cell-sub">{escape(detail)}</span></td>
            <td>{status}</td>
            <td class="cell-num">{points}</td>
            <td>{users}</td>
            <td class="cell-actions">
                <button class="btn btn-icon btn-danger" title="Remove"
                    aria-label="Remove {escape(d.get('name', ''))}"
                    hx-delete="/ui/dependencies/{escape(d['id'])}/remove"
                    hx-target="#dependencies-list" hx-swap="innerHTML"
                    hx-confirm="Remove '{escape(d.get('name', ''))}'{escape(cascade)}?"
                    hx-disabled-elt="this" hx-indicator="#dependencies-overlay">{_ICON_TRASH}</button>
            </td>
        </tr>"""

    return f"""<div class="table-wrap">
        <table class="table-clean">
            <thead><tr>
                <th>Name</th><th>Folder</th><th>Detected as</th><th>Status</th>
                <th class="cell-num">Chunks</th><th>Used by</th>
                <th class="cell-actions"><span class="sr-only">Actions</span></th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""


@page_router.post("/ui/dependencies/check", response_class=HTMLResponse)
def ui_dependencies_check(path: str = Form("")):
    """Dry-run a catalog folder before adding it."""
    try:
        from ragtools.service.routes import _inspect_dependencies
        root = (_form_text(path) or "").strip()
        if not root:
            return '<p class="hint">Enter a folder path first.</p>'
        # Checked from its PARENT, so the entry is inspected as a dependency
        # root rather than as a project of its own.
        parent = str(Path(root).parent) or root
        return _render_deps_check(_inspect_dependencies(parent, [root]))
    except Exception as e:  # noqa: BLE001
        return f'<div class="flash flash-error">Check failed: {escape(str(e))}</div>'


@page_router.post("/ui/dependencies/add", response_class=HTMLResponse)
def ui_dependencies_add(
    id: str = Form(""),
    name: str = Form(""),
    path: str = Form(""),
    request: Request = None,
):
    """Add a catalog entry via the UI form."""
    try:
        from ragtools.service.routes import DependencyCreateRequest, dependency_create
        dependency_create(DependencyCreateRequest(
            id=(_form_text(id) or "").strip().lower(),
            name=(_form_text(name) or "").strip(),
            path=(_form_text(path) or "").strip(),
        ), request)
        return _invalidating(ui_dependencies_list(), resources=("projects", "status"))
    except Exception as e:  # noqa: BLE001
        detail = getattr(e, "detail", str(e))
        return (f'<div class="flash flash-error">{escape(str(detail))}</div>'
                + ui_dependencies_list())


@page_router.delete("/ui/dependencies/{dependency_id}/remove", response_class=HTMLResponse)
def ui_dependencies_remove(dependency_id: str, request: Request = None):
    """Remove a catalog entry, unlinking it from every project that used it."""
    try:
        from ragtools.service.routes import dependency_delete
        # cascade: the confirm dialog already named the affected projects, so a
        # second refusal here would be a dead end rather than a safeguard.
        dependency_delete(dependency_id, cascade=True, request=request)
        return _invalidating(ui_dependencies_list(), resources=("projects", "status"))
    except Exception as e:  # noqa: BLE001
        detail = getattr(e, "detail", str(e))
        return (f'<div class="flash flash-error">{escape(str(detail))}</div>'
                + ui_dependencies_list())


@page_router.get("/ui/frameworks", response_class=HTMLResponse)
def ui_frameworks():
    """Shared-dependency corpora: what exists, how big, who uses it.

    Declaring a dependency moves a whole tree out of the project's own
    collection. Without a place to see the result, the only evidence that
    anything happened was the project's file count going DOWN — which reads as
    data loss, not as success.
    """
    try:
        from ragtools.service.routes import frameworks_list
        data = frameworks_list()
    except Exception as e:  # noqa: BLE001
        return f'<div class="flash flash-error">{escape(str(getattr(e, "detail", e)))}</div>'

    if not data.get("supported"):
        return ('<p class="hint">Shared dependencies need the per-project collection '
                'layout. This service is running the shared layout.</p>')

    rows_data = data.get("frameworks", [])
    if not rows_data:
        return ('<p class="hint">No shared dependencies yet. Declare one on a project '
                '(edit &rarr; <strong>Shared dependencies</strong>) to index a vendored '
                'framework once instead of once per project.</p>')

    rows = ""
    for f in rows_data:
        detail = " ".join(x for x in (f.get("version") or "",
                                      f.get("edition") or "") if x and x != "generic")
        if f.get("state") == "ready":
            state = '<span class="badge badge-success">Ready</span>'
        else:
            state = ('<span class="badge badge-warning">Indexing…</span>'
                     ' <span class="cell-sub">not searchable yet</span>')
        users = ", ".join(f.get("projects") or []) or '<span class="cell-empty">—</span>'
        rows += f"""<tr>
            <td>
                <span class="cell-title">{escape(f.get("name", ""))}</span>
                <code class="cell-sub">{escape(detail)}</code>
            </td>
            <td title="{escape(f.get("canonical_root", ""))}">
                <code>{escape(_shorten(f.get("canonical_root", ""), 40))}</code>
            </td>
            <td>{state}</td>
            <td class="cell-num">{f.get("points", 0):,}</td>
            <td>{users}</td>
        </tr>"""

    return f"""<div class="table-wrap">
        <table class="table-clean">
            <thead><tr>
                <th>Dependency</th><th>Folder</th><th>Status</th>
                <th class="cell-num">Chunks</th><th>Used by</th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""


def _shorten(text: str, width: int) -> str:
    """Middle-truncate a path for a table cell (full value goes in `title`)."""
    return text if len(text) <= width else "…" + text[-(width - 1):]


@page_router.post("/ui/projects/dependencies/check", response_class=HTMLResponse)
def ui_projects_deps_check_new(
    path: str = Form(""),
    dependency_paths: str = Form(""),
):
    """Dry-run for the ADD form, where no project exists to look up yet.

    Worth having separately: getting a dependency wrong on the first save means
    the tree is excluded from the project scan and indexed nowhere, which looks
    like data loss rather than a typo.
    """
    try:
        from ragtools.service.routes import _inspect_dependencies
        root = (path or "").strip()
        if not root:
            return '<p class="hint">Enter the folder path above first.</p>'
        return _render_deps_check(
            _inspect_dependencies(root, _lines(_form_text(dependency_paths) or "")))
    except Exception as e:  # noqa: BLE001
        return f'<div class="flash flash-error">Check failed: {escape(str(e))}</div>'


@page_router.post("/ui/projects/{project_id}/dependencies/check", response_class=HTMLResponse)
def ui_projects_deps_check(
    project_id: str,
    dependency_paths: str = Form(""),
    path: str = Form(""),
):
    """Dry-run the typed dependency folders. Mutates nothing."""
    try:
        from ragtools.service.routes import _inspect_dependencies
        settings = get_settings()
        project = next((p for p in settings.projects if p.id == project_id), None)
        if not project:
            return '<div class="flash flash-error">Project not found</div>'
        # Validate against the path currently TYPED in the form, not the saved
        # one — otherwise moving a project and adding a dependency in the same
        # edit checks the paths against the old root and reports nonsense.
        root = (path or "").strip() or project.path
        return _render_deps_check(
            _inspect_dependencies(root, _lines(_form_text(dependency_paths) or "")))
    except Exception as e:  # noqa: BLE001
        return f'<div class="flash flash-error">Check failed: {escape(str(e))}</div>'


@page_router.get("/ui/projects/{project_id}/edit", response_class=HTMLResponse)
def ui_projects_edit(project_id: str):
    """Inline edit form for a project row."""
    settings = get_settings()
    project = next((p for p in settings.projects if p.id == project_id), None)
    if not project:
        return f'<tr><td colspan="7"><div class="flash flash-error">Project not found</div></td></tr>'

    patterns_text = "\n".join(project.ignore_patterns)
    cur_mode = project.mode

    def _sel(v):
        return " selected" if cur_mode == v else ""

    mode_select = f'''<div class="form-group">
                    <label class="form-label" for="edit-mode-{escape(project_id)}">Mode</label>
                    <select name="mode" id="edit-mode-{escape(project_id)}" class="form-select">
                        <option value="docs"{_sel('docs')}>Docs — Markdown, text and README files</option>
                        <option value="code"{_sel('code')}>Code — source and config files</option>
                        <option value="general"{_sel('general')}>General — both docs and code</option>
                    </select>
                    <small class="hint">Files that carry secrets (.env, keys, credentials) are never
                    indexed, whichever you pick.</small>
                </div>'''

    deps_field = _deps_field(project)
    return f"""<tr id="project-row-{escape(project_id)}" class="row-editing">
        <td colspan="7">
            <form hx-put="/ui/projects/{escape(project_id)}/save" hx-target="#projects-list" hx-swap="innerHTML">
                <div class="grid-2">
                    <div class="form-group">
                        <label class="form-label" for="edit-name-{escape(project_id)}">Display name</label>
                        <input type="text" name="name" id="edit-name-{escape(project_id)}"
                               class="form-input" value="{escape(project.name)}">
                    </div>
                    <div class="form-group">
                        <label class="form-label" for="edit-path-{escape(project_id)}">Folder path</label>
                        <input type="text" name="path" id="edit-path-{escape(project_id)}"
                               class="form-input" value="{escape(project.path)}">
                    </div>
                </div>
                {mode_select}
                <details class="disclosure-action">
                    <summary>Ignore patterns</summary>
                    <div class="form-group">
                        <textarea name="ignore_patterns" rows="3" class="form-textarea"
                                  placeholder="One pattern per line">{escape(patterns_text)}</textarea>
                    </div>
                </details>
                {deps_field}
                <div class="form-inline-actions">
                    <button type="submit" class="btn btn-primary btn-sm"
                        hx-disabled-elt="this" hx-indicator="#projects-overlay">Save changes</button>
                    <button type="button" class="btn btn-ghost btn-sm"
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
    mode: str = Form("docs"),
    dependencies: list[str] = Form(None),
    deps_present: str = Form(""),
    request: Request = None,
):
    """Save edited project via UI form."""
    try:
        from ragtools.service.routes import (
            ProjectDependencyLinkRequest, ProjectUpdateRequest,
            project_dependencies_set, project_update,
        )
        patterns = [line.strip() for line in ignore_patterns.splitlines() if line.strip()]
        req = ProjectUpdateRequest(name=name.strip() or None, path=path.strip() or None,
                                   ignore_patterns=patterns, mode=mode)
        project_update(project_id, req, request)

        # Unchecked boxes submit NOTHING, so "no dependencies key" is
        # ambiguous: it means either "none selected" or "this form has no
        # dependency selector at all". A hidden marker disambiguates — without
        # it, any form lacking the selector would clear every link on save.
        if _form_text(deps_present):
            selected = [d for d in (dependencies or []) if d]
            project_dependencies_set(
                project_id, ProjectDependencyLinkRequest(dependencies=selected),
                request)
        return _invalidating(_render_projects_list())
    except Exception as e:
        detail = getattr(e, "detail", str(e))
        return f'<div class="flash flash-error">Save failed: {escape(str(detail))}</div>' + _render_projects_list()


@page_router.post("/ui/projects/{project_id}/toggle", response_class=HTMLResponse)
def ui_projects_toggle(project_id: str, request: Request = None):
    """Toggle project enabled/disabled via UI."""
    try:
        from ragtools.service.routes import project_toggle
        project_toggle(project_id, request)
        return _invalidating(_render_projects_list())
    except Exception as e:
        detail = getattr(e, "detail", str(e))
        return f'<div class="flash flash-error">{escape(str(detail))}</div>' + _render_projects_list()


@page_router.delete("/ui/projects/{project_id}/remove", response_class=HTMLResponse)
def ui_projects_remove(project_id: str, request: Request = None):
    """Remove a project via UI. Delegates to the guarded API handler.

    This used to remove the project from the config immediately and then delete
    the indexed data from a ``threading.Timer`` whose failures went nowhere the
    user could see. Three things were wrong with that, and all three were
    invisible:

    * the response had already been sent, so a failed delete rendered exactly
      like a successful one — the row disappeared either way;
    * the config was written FIRST, so a failed delete left vectors belonging to
      a project that no longer existed, unreachable and un-deletable through the
      UI that had just orphaned them;
    * nothing checked whether storage was even reachable before starting.

    Doing the delete inline through :func:`ragtools.service.routes.project_delete`
    fixes all three at once: one gate, one order (data then config), and a
    failure the user reads as a failure.
    """
    from ragtools.service import destructive

    try:
        from ragtools.service.routes import project_delete

        project_delete(project_id, request)
        return _invalidating(_render_projects_list())
    except destructive.OperationRefused as refused:
        return (f'<div class="flash flash-error">Project not removed: '
                f'{escape(refused.reason)}</div>') + _render_projects_list()
    except Exception as e:
        detail = getattr(e, "detail", str(e))
        logger.exception("project remove failed: %s", project_id)
        return (f'<div class="flash flash-error">Could not remove '
                f'{escape(project_id)}: {escape(str(detail))}</div>'
                ) + _render_projects_list()


# --- Activity log fragment ---


@page_router.get("/ui/activity", response_class=HTMLResponse)
def ui_activity(after: int = Query(0)):
    """Activity log fragment — INCREMENTAL.

    The client sends the cursor it already holds (`after`) and prepends only
    what is new (`hx-swap="afterbegin"`). The new cursor is returned in an
    ``HX-Trigger`` header rather than in the markup, so the caller can track it
    without parsing the DOM.

    Two rules make append mode safe:
      * nothing new -> return an EMPTY body (an "empty" placeholder would
        otherwise be prepended on every poll, forever);
      * the no-activity placeholder is only rendered on a first load.
    """
    import json as _json

    from ragtools.service.activity import activity_log
    events = activity_log.get_recent(limit=50, after_id=after)

    if not events:
        if after:
            # Incremental poll with nothing new: prepend nothing at all.
            return HTMLResponse(content="", headers={
                "HX-Trigger": _json.dumps({"rag-activity-cursor": {"id": after}})
            })
        return '<div class="activity-empty">No recent activity</div>'

    rows = []
    latest_id = events[-1].id if events else 0
    for e in reversed(events):  # newest first
        detail_html = ""
        if e.details:
            detail_html = f'<div class="activity-details">{escape(e.details)}</div>'

        rows.append(f"""
        <div class="activity-event">
            <span class="activity-time">{escape(e.timestamp[11:19])}</span>
            <span class="badge {_LEVEL_BADGE.get(e.level, 'badge-info')}">{escape(e.level)}</span>
            <span class="activity-source">{escape(e.source)}</span>
            <span class="activity-msg">{escape(e.message)}</span>
            {detail_html}
        </div>
        """)

    # The cursor travels in a header so append-mode swaps stay flat (no nested
    # wrapper per poll). `data-latest-id` is retained for backwards compat.
    return HTMLResponse(
        content=f'<div data-latest-id="{latest_id}">' + "".join(rows) + "</div>",
        headers={"HX-Trigger": _json.dumps({"rag-activity-cursor": {"id": latest_id}})},
    )


# --- Crash banner fragment ---


@page_router.get("/ui/crash-banner", response_class=HTMLResponse)
def ui_crash_banner():
    """HTML fragment for the crash banner. Empty string if no unreviewed
    crashes are present, which leaves the banner slot collapsed.
    """
    from ragtools.service.crash_history import list_unreviewed_crashes
    items = list_unreviewed_crashes(get_settings())
    if not items:
        return ""

    blocks = []
    for item in items:
        kind = item.get("kind", "service_crash")
        dismiss_key = item.get("dismiss_key", kind)
        timestamp = item.get("timestamp", "unknown time")

        if kind == "supervisor_gave_up":
            title = "Supervisor stopped restarting the service"
            short = item.get("reason", "Too many crashes in a short window.")
            full_detail = item.get("reason", "")
        elif kind == "watcher_gave_up":
            title = "File watcher stopped — changes are no longer being indexed"
            retries = item.get("retries", "?")
            error = item.get("error", "")
            short = (
                f"Watcher exhausted {retries} restart attempts. "
                f"Use Rebuild or restart the service to recover."
            )
            full_detail = error
        else:
            title = "The service crashed in the previous session"
            exc_type = item.get("exception_type", "Exception")
            message = item.get("message", "")
            short = f"{exc_type}: {message}" if message else exc_type
            full_detail = item.get("traceback") or ""

        details_html = ""
        if full_detail:
            details_html = f"""
            <details class="crash-banner-details">
                <summary>Full details</summary>
                <pre>{escape(full_detail)}</pre>
            </details>
            """

        blocks.append(f"""
        <div class="crash-banner" role="alert"
             id="crash-banner-{escape(dismiss_key)}">
            <div class="crash-banner-head">
                <strong class="crash-banner-title">{escape(title)}</strong>
                <span class="crash-banner-time">{escape(timestamp)}</span>
                <button class="crash-banner-dismiss"
                        title="Mark reviewed"
                        hx-post="/api/crash-history/{escape(dismiss_key)}/dismiss"
                        hx-target="#crash-banner-{escape(dismiss_key)}"
                        hx-swap="outerHTML"
                        hx-disabled-elt="this">&times;</button>
            </div>
            <div class="crash-banner-msg">{escape(short)}</div>
            {details_html}
        </div>
        """)

    return "\n".join(blocks)


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
    desktop_notifications: str = Form(None),
    request: Request = None,
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
        # Unchecked HTML checkboxes aren't sent at all, so a missing value
        # means "off" — we must record False explicitly so the toggle can
        # ever be disabled.
        payload["desktop_notifications"] = (desktop_notifications == "true")

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
            result = update_config(req, request)
            saved_keys = result["updated"]
            restart = result["restart_required"]
        else:
            saved_keys = []
            restart = False

        if startup_changed:
            saved_keys.extend(["startup_open_browser", "startup_delay"])

        msg = f'Saved: {", ".join(saved_keys)}'
        if restart:
            msg += ' <span class="badge badge-warning">Restart required</span>'

        return f'<div class="flash flash-success">{msg}</div>'
    except Exception as e:
        detail = getattr(e, "detail", str(e))
        return f'<div class="flash flash-error">Save failed: {escape(str(detail))}</div>'


# --- Helpers ---


def _update_toml_config(section: str | None, data: dict) -> None:
    """Update the TOML config file. If section is None, update root level."""
    import tomli_w
    from ragtools.config import CONFIG_VERSION, get_config_write_path

    config_path = get_config_write_path()

    existing = {}
    if config_path.exists():
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib
        with open(config_path, "rb") as f:
            existing = tomllib.load(f)

    # Preserve whatever schema version the file already declares; stamp the
    # current one only when creating a file that has none. A settings writer
    # must never *decide* the schema version — that is the migrator's job, and
    # this line previously wrote `1`, silently declaring a v1 config.
    existing.setdefault("version", CONFIG_VERSION)

    if section is None:
        existing.update(data)
    else:
        existing.setdefault(section, {})
        existing[section].update(data)

    # Atomic write (S1/A4): serialize fully in memory, then temp+fsync+replace
    # so an interruption can never truncate the live config.
    import io
    from ragtools.atomicio import atomic_write_bytes

    buf = io.BytesIO()
    tomli_w.dump(existing, buf)
    atomic_write_bytes(config_path, buf.getvalue(), backup=True)

    logger.info("Config updated: section=%s, keys=%s", section or "root", list(data.keys()))
    from ragtools.service.activity import log_activity
    log_activity("info", "config", f"Config saved: {section or 'general'} ({', '.join(data.keys())})")


def _save_projects_to_toml(projects: list, dependencies: list | None = None) -> None:
    """Write the full projects list to TOML config, setting version=2.

    Writes the entire [[projects]] array atomically (not merged key-by-key).

    ``dependencies`` is the shared-dependency catalog. It is written only when
    supplied, so the many existing callers that touch projects alone cannot
    erase the catalog by omission — the failure that would look like every
    project quietly losing its shared dependencies at once.
    """
    import tomli_w
    from ragtools.config import CONFIG_VERSION, get_config_write_path

    config_path = get_config_write_path()

    existing = {}
    if config_path.exists():
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib
        with open(config_path, "rb") as f:
            existing = tomllib.load(f)

    # PRESERVE the declared version; never assert one.
    #
    # This line read `existing["version"] = 2` unconditionally. Reached from
    # sixteen production call sites — every project add, remove, edit, mode
    # change, ignore rule and dependency change, from CLI, admin panel and MCP
    # alike — it meant a v3 config was demoted to v2 by the user's next edit,
    # and re-migrated on the following boot, forever. Measured: version
    # oscillating 3 -> 2 -> 3 while the v3 keys themselves survived, which made
    # "has this been migrated?" unanswerable from the version field.
    #
    # Saving projects says nothing about the schema version, so it now says
    # nothing about the schema version.
    existing.setdefault("version", CONFIG_VERSION)
    # Serialize via model_dump so every ProjectConfig field round-trips (no
    # hand-maintained key list to forget). exclude_none drops any None values —
    # required because tomli_w cannot serialize None (TOML has no null). The
    # canonical `mode` field always has a value (default "docs"), so it always
    # persists.
    existing["projects"] = [p.model_dump(exclude_none=True) for p in projects]
    if dependencies is not None:
        existing["dependencies"] = [d.model_dump(exclude_none=True) for d in dependencies]
    # Remove legacy content_root if upgrading
    existing.pop("content_root", None)

    # Atomic write (S1/A4): serialize fully in memory, then temp+fsync+replace.
    import io
    from ragtools.atomicio import atomic_write_bytes

    buf = io.BytesIO()
    tomli_w.dump(existing, buf)
    atomic_write_bytes(config_path, buf.getvalue(), backup=True)

    logger.info("Projects saved: %d projects to TOML", len(projects))
    from ragtools.service.activity import log_activity
    log_activity("info", "config", f"Projects saved: {len(projects)} projects")


# ---------------------------------------------------------------------------
# Client access profiles (Settings page "Client Access" card)  — S12
# ---------------------------------------------------------------------------


def _client_store(settings):
    from pathlib import Path as _P

    from ragtools.profile_store import ProfileStore
    return ProfileStore(str(_P(settings.data_dir) / "profiles.db"))


def _render_clients_fragment(settings, *, error: str = "", message: str = "",
                             edit_profile=None) -> str:
    """Build the Client Access card body: existing clients + the add form.

    Capability groups render as security checkboxes grouped by risk tier; the
    owner-only administration group is never offered; destructive access is a
    separate, prominent opt-in.
    """
    from ragtools.client_admin import profile_summary

    with _client_store(settings) as _store:
        profiles = _store.list()
    project_ids = [p.id for p in settings.projects]

    rows = ""
    for p in profiles:
        s = profile_summary(p)
        scope = s["scope"] if isinstance(s["scope"], str) else (", ".join(s["scope"]) or "none")
        caps = ", ".join(s["capabilities"]) or "none"
        dz = ('<span class="badge badge-warning">Allowed</span>' if s["destructive"]
              else '<span class="cell-empty">No</span>')
        pid = escape(s["profile_id"])
        rows += (
            f'<tr><td><span class="cell-title">{pid}</span>'
            f'<span class="cell-sub">{escape(s["display_name"])}</span></td>'
            f'<td>{escape(scope)}</td><td>{escape(caps)}</td><td>{dz}</td>'
            f'<td class="client-actions">'
            f'<button class="btn btn-sm btn-secondary" hx-get="/ui/clients/{pid}/edit" '
            f'hx-target="#clients-panel" hx-swap="innerHTML">Edit</button> '
            f'<button class="btn btn-sm btn-danger" hx-delete="/ui/clients/{pid}/remove" '
            f'hx-target="#clients-panel" hx-swap="innerHTML" '
            f'hx-confirm="Remove the client profile {pid}?">Remove</button>'
            f'</td></tr>'
        )

    alert = ""
    if error:
        alert = f'<div class="alert alert-danger" role="alert">{escape(error)}</div>'
    elif message:
        alert = f'<div class="alert alert-success" role="status">{escape(message)}</div>'

    if not rows:
        # An empty table with a "no rows" line is chrome around nothing. Say
        # what the current state means instead, then offer the action.
        table = ('<p class="cell-empty">No client profiles. Every connected client is '
                 "treated as the owner and has full access.</p>")
    else:
        table = f"""<div class="table-wrap"><table class="table">
  <thead><tr><th>Client</th><th>Projects</th><th>Capabilities</th><th>Destructive</th>
  <th class="cell-actions"><span class="sr-only">Actions</span></th></tr></thead>
  <tbody>{rows}</tbody>
</table></div>"""

    return f"""{alert}
{table}
{_client_form(project_ids, edit_profile)}"""


def _client_form(project_ids, profile=None) -> str:
    """The add/edit form. When ``profile`` is given, pre-fill it as an edit
    (id locked, current scope/capabilities/destructive pre-checked)."""
    from ragtools.client_admin import AGENT_GRANTABLE_GROUPS, CAPABILITY_CATALOG

    editing = profile is not None
    cur_caps = set(profile.capability_groups) if editing else set()
    all_proj = editing and profile.allowed_projects is None
    cur_projects = set(profile.allowed_projects) if (editing and not all_proj) else set()
    destructive = editing and profile.destructive_policy != "forbidden"

    def _boxes(tier: str) -> str:
        html = ""
        for c in CAPABILITY_CATALOG:
            if c.tier != tier or c.group not in AGENT_GRANTABLE_GROUPS:
                continue
            ck = " checked" if c.group in cur_caps else ""
            html += (
                f'<label class="check"><input type="checkbox" name="caps" value="{c.group}"{ck}> '
                f'<strong>{escape(c.label)}</strong> '
                f'<span class="muted">— {escape(c.description)}</span></label>'
            )
        return html

    proj_boxes = "".join(
        f'<label class="check"><input type="checkbox" name="projects" value="{escape(pid)}"'
        f'{" checked" if pid in cur_projects else ""}> <span>{escape(pid)}</span></label>'
        for pid in project_ids
    ) or '<span class="cell-empty">No projects configured yet.</span>'

    if editing:
        id_field = f'<input name="id" value="{escape(profile.profile_id)}" readonly>'
        name_val = escape(profile.display_name)
        btn, summary, open_attr = "Update client", f"Editing {escape(profile.profile_id)}", " open"
        cancel = ('<button type="button" class="btn btn-ghost btn-sm" hx-get="/ui/clients" '
                  'hx-target="#clients-panel" hx-swap="innerHTML">Cancel</button>')
    else:
        id_field = '<input name="id" placeholder="docs-bot" required pattern="[a-z0-9][a-z0-9_-]*">'
        name_val = ""
        # "Add client", not "Save client": Settings has exactly one Save action
        # (the page header). This creates a new object — same verb as "Add
        # project" — so it must not read as a second way to save the page.
        btn, summary, open_attr, cancel = "Add client", "Add a client", "", ""

    all_ck = " checked" if all_proj else ""
    dz_ck = " checked" if destructive else ""

    return f"""<details class="client-add disclosure-action"{open_attr}>
  <summary>{summary}</summary>
  <form hx-post="/ui/clients/add" hx-target="#clients-panel" hx-swap="innerHTML">
    <div class="form-row">
      <label class="form-label">Client ID{id_field}</label>
      <label class="form-label">Display name<input name="name" value="{name_val}" placeholder="Docs Bot"></label>
    </div>
    <fieldset>
      <legend>Project access</legend>
      <label class="check"><input type="checkbox" name="all_projects" value="1"{all_ck}>
        <span><strong>All projects</strong></span></label>
      <div class="check-grid">{proj_boxes}</div>
    </fieldset>
    <fieldset>
      <legend>Read access</legend>{_boxes("read")}
    </fieldset>
    <fieldset>
      <legend>Write access</legend>{_boxes("write")}
    </fieldset>
    <fieldset class="fieldset-danger">
      <legend>Destructive</legend>
      <label class="check"><input type="checkbox" name="allow_destructive" value="1"{dz_ck}>
        <span><strong>Allow deleting and restoring collections</strong>
        <small class="hint">Off by default. Only grant this to a client you would trust to
        wipe the index.</small></span></label>
    </fieldset>
    <div class="form-inline-actions">
      <button class="btn btn-primary btn-sm" type="submit">{btn}</button>{cancel}
    </div>
  </form>
</details>"""


@page_router.get("/ui/clients", response_class=HTMLResponse)
def ui_clients(request: Request):
    return _render_clients_fragment(get_settings())


@page_router.get("/ui/clients/{profile_id}/edit", response_class=HTMLResponse)
def ui_clients_edit(profile_id: str):
    """Load the Client Access form pre-filled with an existing client's settings."""
    settings = get_settings()
    with _client_store(settings) as _store:
        profile = _store.get(profile_id)
    if profile is None:
        return _render_clients_fragment(settings, error=f"No such client: {profile_id}")
    return _render_clients_fragment(settings, edit_profile=profile)


@page_router.post("/ui/clients/add", response_class=HTMLResponse)
def ui_clients_add(
    id: str = Form(""),
    name: str = Form(""),
    all_projects: list[str] = Form(default=[]),
    projects: list[str] = Form(default=[]),
    caps: list[str] = Form(default=[]),
    allow_destructive: list[str] = Form(default=[]),
):
    from ragtools.client_admin import ClientAdminError, build_profile

    settings = get_settings()
    try:
        profile = build_profile(
            profile_id=id, display_name=name,
            all_projects=bool(all_projects), projects=list(projects),
            capabilities=list(caps), allow_destructive=bool(allow_destructive),
        )
        with _client_store(settings) as _store:
            _store.add(profile)
        from ragtools.service.activity import log_activity
        log_activity("info", "config", f"Client profile saved: {profile.profile_id}")
        return _render_clients_fragment(settings, message=f"Client '{profile.profile_id}' saved.")
    except ClientAdminError as exc:
        return _render_clients_fragment(settings, error=str(exc))


@page_router.delete("/ui/clients/{profile_id}/remove", response_class=HTMLResponse)
def ui_clients_remove(profile_id: str):
    settings = get_settings()
    with _client_store(settings) as _store:
        _store.remove(profile_id)
    from ragtools.service.activity import log_activity
    log_activity("info", "config", f"Client profile removed: {profile_id}")
    return _render_clients_fragment(settings, message=f"Client '{profile_id}' removed.")
