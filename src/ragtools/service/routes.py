"""HTTP API routes for the RAG service."""

import hashlib
import logging
import os
import signal
import threading
import uuid as _uuid
from dataclasses import asdict
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel

from ragtools.service.app import (
    get_owner,
    get_runtime,
    get_settings,
    get_shutdown_event,
)
from ragtools.retrieval.scope import ScopeUnresolvedError

logger = logging.getLogger("ragtools.service")

router = APIRouter()


def _parse_projects(projects: Optional[list[str]]) -> Optional[list[str]]:
    """Normalize the ``projects`` query param (S1/A3).

    Accepts BOTH repeated params (``?projects=a&projects=b``) and the comma
    form (``?projects=a,b``), and any mix — v2.7.0's ``Optional[str]`` typing
    silently kept only the last repeated value. Blanks are stripped; an
    all-blank/empty result becomes ``None`` (an unscoped request, which the
    owner then fails closed).
    """
    if projects is None:
        return None
    out: list[str] = []
    for item in projects:
        out.extend(p.strip() for p in item.split(",") if p.strip())
    return out or None


def _scope_refusal(exc: ScopeUnresolvedError) -> HTTPException:
    """Map a fail-closed scope refusal to a 422 with a machine-readable code."""
    return HTTPException(
        status_code=422,
        detail={"error_code": "SCOPE_UNRESOLVED", "error": str(exc)},
    )


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
# Desired lifecycle state: should the watcher be running? Set False only by an
# explicit user stop (POST /api/watcher/stop) so that lifecycle autostart and
# the project-edit restart path never fight a deliberate stop. Per-process: a
# service restart re-arms autostart (a restart is itself an operator action).
_watcher_desired_run = True
# Set when a *lifecycle-owned* autostart fails to even construct/start the
# thread. There is no WatcherThread to hold a last_error in that case, so the
# failure is recorded here to stay visible via the derived state + /health.
_watcher_autostart_error: Optional[str] = None
_watcher_autostart_error_at: Optional[str] = None


# --- Request/Response models ---

class IndexRequest(BaseModel):
    project: Optional[str] = None
    full: bool = False


# --- Health ---

def _migration_remedy(settings) -> str:
    """The retry instruction that works on this installation. See app.py."""
    try:
        from ragtools.service.app import migration_remedy

        return migration_remedy(settings)
    except Exception:  # noqa: BLE001
        return "restart the service — it resumes the rebuild automatically"


@router.get("/health")
def health():
    """Readiness probe. Returns 200 when encoder loaded + Qdrant open.

    The 200 body is a stable contract — see ``docs/decisions.md``
    Decision 16. Patch releases may add keys (this is one of those
    additions: ``version`` + ``watcher_running``) but never remove or
    rename existing ones. The 503 body remains FastAPI's default
    ``{"detail": "..."}`` shape.
    """
    try:
        owner = get_owner()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Service not ready")

    from ragtools import __version__

    # Cheap, non-blocking probe of the watcher daemon thread. Watcher
    # details (last_error, paths, ...) live at /api/watcher/status; this
    # field is the one-bit summary so callers can short-circuit without
    # a second request.
    watcher_running = False
    try:
        thread = _watcher_thread  # module-level singleton owned by routes
        watcher_running = thread is not None and thread.is_alive()
    except Exception:
        # Defensive: a failure to introspect the watcher must never make
        # /health return non-200. Fall through with watcher_running=False.
        watcher_running = False

    # Additive degraded signal — `status` stays "ready" for liveness back-compat;
    # this surfaces watcher-down on /health so callers needn't interpret the raw
    # bool. Only an *undesired* down counts: a watcher the user explicitly stopped
    # (desired_run False) is intentional, not degraded. Index staleness is reported
    # on /api/status + `rag doctor` (state DB); /health stays cheap and lock-free.
    issues = []
    if not watcher_running and _watcher_desired_run:
        issues.append("watcher_not_running")

    # Is the vector store actually answering? Reporting "ready" because the
    # PROCESS is up is close enough for an in-process engine, but a managed or
    # external server can die while this service keeps saying it is fine — and
    # then every search and index fails against a store nobody declared gone.
    # Cached probe, so polling /health stays cheap.
    def _storage_degradation() -> str:
        try:
            from ragtools.service.app import storage_degradation

            return storage_degradation()
        except Exception:  # noqa: BLE001 — reporting must never break liveness
            return ""

    storage_ok, storage_detail = True, ""
    try:
        storage_ok, storage_detail = owner.storage_reachable()
    except Exception:  # noqa: BLE001 — never let the probe break liveness
        storage_ok, storage_detail = True, ""
    if not storage_ok:
        issues.append("storage_unreachable")

    # The managed engine's own lifecycle, from cached state. "The engine crashed
    # and we are on restart attempt 2 of 3" and "the engine is fine but a query
    # timed out" are different facts with different remedies, and v3.2.0 could
    # express neither — the engine could die and nothing anywhere would say so.
    engine = None
    try:
        from ragtools.service.app import engine_status
        from ragtools.service.engine_lifecycle import (
            CRASHED,
            RESTART_EXHAUSTED,
            RESTARTING,
        )

        engine = engine_status()
        if engine is not None:
            if engine["state"] == CRASHED:
                issues.append("engine_crashed")
            elif engine["state"] == RESTARTING:
                issues.append("engine_restarting")
            elif engine["state"] == RESTART_EXHAUSTED:
                issues.append("engine_restart_exhausted")
            if engine.get("log_error"):
                issues.append("engine_log_unavailable")
    except Exception:  # noqa: BLE001 — reporting must never break liveness
        engine = None

    # A rebuild that started and never finished. Without this an interrupted
    # rebuild leaves an empty index and no explanation, which reads as data loss.
    try:
        from ragtools.service.destructive import pending_intent

        if pending_intent(owner.settings) is not None:
            issues.append("rebuild_interrupted")
    except Exception:  # noqa: BLE001
        pass

    # A configuration that could not be brought to the current schema is a real
    # degradation: the product is running on fallback defaults rather than on
    # what the file says, and nothing else would ever mention it. Silence here
    # is exactly how migrate_config came to be unreachable for two releases.
    config_state = "unknown"
    try:
        from ragtools.bootstrap import last_result

        bootstrap = last_result()
        if bootstrap is not None:
            config_state = bootstrap.describe()
            if bootstrap.degraded:
                issues.append("config_migration_failed")
    except Exception:  # noqa: BLE001 — never let diagnostics break liveness
        config_state = "unknown"

    # A half-rebuilt index is not a ready one.
    #
    # During a layout migration the new collections exist and are being filled.
    # Answering "ready" then means every search returns the ordinary "no
    # matches" shape from an index that simply has not been built yet — which
    # tells the user their content is gone. That answer is both wrong and
    # completely convincing, so `status` itself changes here rather than only
    # `degraded`: callers that check nothing else still get the truth.
    migration_state = None
    try:
        from ragtools.upgrade import relayout

        plan = relayout.active_plan(owner.settings)
        if plan is not None:
            report = relayout.progress(owner.settings, plan)
            if report is not None and not report.complete:
                migration_state = report
                issues.append("reindex_in_progress")
                if report.failed:
                    issues.append("reindex_incomplete")
                if report.blocked:
                    issues.append("reindex_blocked")
    except Exception:  # noqa: BLE001 — never let this break liveness
        migration_state = None

    return {
        # "ready" stays truthful about the process for liveness back-compat,
        # EXCEPT while the index is being rebuilt, when it is not true at all.
        "status": "migrating" if migration_state is not None else "ready",
        "migration": (None if migration_state is None else {
            "state": migration_state.describe(),
            "total": migration_state.total,
            "done": migration_state.done,
            "failed": migration_state.failed,
            "pending": migration_state.pending,
            # A rebuild that is WAITING on storage and one that is WORKING look
            # identical from a count of unfinished units, and they need opposite
            # responses: one wants patience, the other wants a person.
            "blocked": migration_state.blocked,
            "blocked_reason": migration_state.blocked_reason or None,
            "stalled": migration_state.stalled,
            "failures": [
                {"kind": k, "id": i, "error": e}
                for k, i, e in migration_state.failures
            ],
            # The remedy that works on THIS installation. This was hard-coded to
            # `rag upgrade --resume`, which cannot run on a managed machine in
            # either state — it refuses while the service is up, and raises while
            # it is down because the engine is down with it.
            "retry": _migration_remedy(owner.settings),
        }),
        "collection": owner.settings.collection_name,
        "version": __version__,
        "watcher_running": watcher_running,
        "storage_reachable": storage_ok,
        "storage_error": storage_detail,
        # The managed engine's lifecycle. `None` means "no managed engine in
        # this configuration" — a different fact from "the engine is down", and
        # it must not render as one.
        "engine": engine,
        # WHY the running engine differs from the configured one. Without this,
        # a machine that silently fell back to embedded is indistinguishable
        # from one deliberately configured that way — same `storage_backend`,
        # same empty-looking index, no explanation anywhere but the log.
        "storage_degraded_reason": _storage_degradation(),
        "degraded": bool(issues),
        "issues": issues,
        # Which storage engine and collection model are actually in force.
        # /health is the one endpoint every probe already calls, and "which
        # engine am I really on" was previously unanswerable without reading
        # the source.
        "storage_backend": getattr(owner.settings, "storage_backend", "embedded"),
        "collection_strategy": owner.router.strategy,
        # What the schema migration did on this boot, so "am I actually running
        # a v3 configuration?" is answerable without reading the file.
        "config_version": getattr(owner.settings, "config_version", None),
        "config_state": config_state,
    }


# --- Service identity (S16) ---

# The ACTUAL bound address, recorded by the launcher after uvicorn binds. Until
# set, /identity falls back to the configured host/port. This is the seam that
# lets ``bound_port`` be the real bind — the field that catches a
# :21422-reports-:21420 mismatch (§27.1).
_bound_address = {"host": None, "port": None}

# Per-process identity; changes on restart (distinct from the stable service_id).
_INSTANCE_ID = _uuid.uuid4().hex[:16]


def set_bound_address(host, port) -> None:
    """Record the actual bound host/port (called by the launcher post-bind)."""
    _bound_address["host"] = host
    _bound_address["port"] = port


def _install_mode() -> str:
    import ragtools

    return "packaged" if "site-packages" in (getattr(ragtools, "__file__", "") or "") else "source"


@router.get("/identity")
def identity():
    """Service identity (§27.1) — a NEW endpoint, so /health stays compatible.

    A client verifies ``service_id`` / ``profile`` / ``api_version`` before
    trusting the service, and reads ``bound_port`` as the REAL bind. Composes
    :func:`ragtools.service.identity.build_identity`.
    """
    try:
        owner = get_owner()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Service not ready")

    from ragtools import __version__
    from ragtools.service.identity import build_identity
    from ragtools.storage import resolve_backend

    settings = owner.settings
    service_id = "svc-" + hashlib.sha256(str(settings.data_dir).encode()).hexdigest()[:12]
    host = _bound_address["host"] or settings.service_host
    port = _bound_address["port"] if _bound_address["port"] is not None else settings.service_port

    try:
        backend = resolve_backend(settings)
        caps = backend.capabilities()  # capabilities() never opens the client
        storage = {
            "mode": backend.mode,
            "target": getattr(settings, "storage_url", None) or str(settings.qdrant_path),
            "engine_version": caps.server_version,
        }
        capabilities = [k for k, v in asdict(caps).items() if v is True]
    except Exception:
        storage = {"mode": "embedded", "target": str(settings.qdrant_path), "engine_version": None}
        capabilities = []

    return build_identity(
        service_id=service_id,
        instance_id=_INSTANCE_ID,
        version=__version__,
        profile=os.environ.get("RAG_PROFILE", "installed"),
        install_mode=_install_mode(),
        bound_host=host,
        bound_port=port,
        data_dir=str(settings.data_dir),
        config_path=os.environ.get("RAG_CONFIG_PATH", ""),
        storage=storage,
        auth_mode="none",
        capabilities=capabilities,
        collections_ready=True,
    )


# --- Search ---

@router.get("/api/search")
def search(
    query: str = Query(..., description="Search query"),
    project: Optional[str] = Query(None, description="Filter by a single project"),
    projects: Optional[list[str]] = Query(
        None,
        description=(
            "Project IDs to search the union of — accepts repeated params "
            "(projects=a&projects=b) and/or a comma-separated value. Takes "
            "precedence over ``project`` when both are given."
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
    """Search the knowledge base — one project or a set of projects.

    Fail-closed (S1/A2): an unscoped or empty scope is refused with 422
    ``SCOPE_UNRESOLVED`` rather than silently searching every project.
    """
    owner = get_owner()
    project_ids = _parse_projects(projects)
    try:
        result = owner.search_formatted(
            query=query,
            project_id=project,
            project_ids=project_ids,
            top_k=top_k,
            compact=compact,
        )
    except ScopeUnresolvedError as e:
        raise _scope_refusal(e)
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


@router.get("/api/dev-search")
def dev_search_endpoint(
    query: str = Query(..., description="Development / feature-request query"),
    project: Optional[str] = Query(None, description="Filter by a single project"),
    projects: Optional[list[str]] = Query(
        None, description="Project IDs (union) — repeated and/or comma-separated"
    ),
    top_k: int = Query(10, description="Max combined results"),
):
    """Codebase-first layered retrieval (Project Context Mode).

    Searches code, then documentation, then config embeddings, combines and
    reranks by context priority (source code > APIs > workflows > architecture
    > docs), and returns a formatted Project Context block. Fail-closed
    (S1/A2): an unscoped scope is refused with 422 ``SCOPE_UNRESOLVED``.
    """
    owner = get_owner()
    project_ids = _parse_projects(projects)
    try:
        return owner.search_project_context(
            query=query,
            project_id=project,
            project_ids=project_ids,
            top_k=top_k,
        )
    except ScopeUnresolvedError as e:
        raise _scope_refusal(e)


@router.get("/api/definitions")
def definitions_endpoint(
    symbol: str = Query(..., description="Symbol to locate the definition of"),
    project: Optional[str] = Query(None, description="Filter by a single project"),
    top_k: int = Query(25, ge=1, le=100),
):
    """Cross-file code-graph v1: likely definition sites for a symbol (file:line)."""
    owner = get_owner()
    defs = owner.find_definitions(symbol, project_id=project, top_k=top_k)
    return {"symbol": symbol, "project": project, "count": len(defs), "definitions": defs}


@router.get("/api/secret-audit")
def secret_audit_endpoint(
    project: Optional[str] = Query(None, description="Filter by a single project"),
    limit: int = Query(5000, ge=1, le=50000),
):
    """Audit indexed content for secret material (file:line + rule, never values)."""
    return get_owner().audit_secrets(project_id=project, limit=limit)


# --- Indexing ---

@router.post("/api/index")
def index(req: IndexRequest, wait: bool = Query(False), response: Response = None,
          idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")):
    """Trigger indexing. Incremental by default.

    Default is now ASYNC: returns ``202`` with a ``job_id`` and a ``Location``
    header. The work survives the request, reports progress, and can be
    cancelled — previously this blocked for the entire index and lost its result
    if the caller went away.

    ``?wait=true`` preserves the original synchronous contract for the CLI and
    existing tests.
    """
    owner = get_owner()
    if wait:
        if req.full:
            stats = owner.run_full_index(project_id=req.project)
        else:
            stats = owner.run_incremental_index(project_id=req.project)
        return {"stats": stats}

    try:
        runtime = get_runtime()
    except RuntimeError:
        # No job engine (degraded) — never silently drop the request.
        if req.full:
            stats = owner.run_full_index(project_id=req.project)
        else:
            stats = owner.run_incremental_index(project_id=req.project)
        return {"stats": stats}

    job = runtime.submit("index", {"project": req.project, "full": bool(req.full)},
                         idempotency_key=idempotency_key)
    if response is not None:
        response.status_code = 202
        response.headers["Location"] = f"/api/jobs/{job.id}"
    return job.to_dict()


# --- Jobs (Phase 2 / W2) ---

@router.get("/api/jobs")
def list_jobs(active: bool = Query(False), limit: int = Query(50)):
    """Job roster. ``?active=true`` is what a reloading UI reconciles against."""
    runtime = get_runtime()
    jobs = runtime.active_jobs() if active else runtime.list_jobs(limit=limit)
    return {"jobs": [j.to_dict() for j in jobs]}


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    runtime = get_runtime()
    job = runtime.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No such job: {job_id}")
    return job.to_dict()


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, response: Response = None):
    """Request cooperative cancellation; the worker stops at a safe boundary."""
    runtime = get_runtime()
    job = runtime.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No such job: {job_id}")
    if job.is_terminal:
        raise HTTPException(status_code=409,
                            detail=f"Job already finished ({job.state})")
    runtime.request_cancel(job_id)
    if response is not None:
        response.status_code = 202
    return runtime.get_job(job_id).to_dict()


@router.get("/api/events")
def get_events(after: int = Query(0), limit: int = Query(200)):
    """Durable event feed. ``after`` is the monotonic cursor (SSE Last-Event-ID)."""
    runtime = get_runtime()
    events = runtime.events_after(after, limit=limit)
    return {"events": [e.to_dict() for e in events],
            "cursor": events[-1].id if events else after}


@router.get("/events")
def events_stream(
    after: Optional[int] = Query(None),
    once: bool = Query(False),
    max_seconds: float = Query(300.0),
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
):
    """Server-Sent Events over the durable cursor.

    Where the stream starts, in precedence order:

    1. ``Last-Event-ID`` — sent automatically by a *reconnecting*
       ``EventSource``. Resumes exactly where the client dropped, because the
       store's autoincrement id *is* the event id. This is the whole point of a
       durable log: a reconnect misses nothing.
    2. An explicit ``?after=N`` — for tests and pollers that want history.
    3. Neither → start from **now** (the latest event id).

    That third case matters: the cursor used to default to 0, so every FRESH
    page load replayed the entire durable history and raised old, already-
    resolved job failures as live toasts. A first connection has no history to
    catch up on — only a reconnect does.

    Reads only ``runtime.db``: the stream can never hold the encoder or vector
    store lock. Connections are bounded by ``max_seconds`` so they recycle
    rather than living forever; ``EventSource`` reconnects transparently.
    ``?once=true`` drains what is pending and closes — used by tests and by
    clients that prefer to poll.
    """
    import json as _json
    import time as _time

    from fastapi.responses import StreamingResponse

    runtime = get_runtime()
    try:
        if last_event_id:
            cursor = int(last_event_id)
        elif after is not None:
            cursor = int(after)
        else:
            cursor = int(runtime.latest_event_id())
    except (TypeError, ValueError):
        cursor = int(after or 0)

    def _frame(e) -> str:
        return (f"id: {e.id}\n"
                f"event: {e.type}\n"
                f"data: {_json.dumps(e.to_dict())}\n\n")

    def _gen():
        nonlocal cursor
        yield ": connected\n\n"          # comment frame: proves liveness immediately
        deadline = _time.monotonic() + max_seconds
        last_beat = _time.monotonic()
        while True:
            try:
                events = runtime.events_after(cursor, limit=100)
            except Exception:
                logger.exception("event stream read failed")
                break
            for e in events:
                cursor = e.id
                yield _frame(e)
            if once:
                break
            now = _time.monotonic()
            if now >= deadline:
                break
            if now - last_beat >= 15.0:
                last_beat = now
                yield ": heartbeat\n\n"
            _time.sleep(0.5)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@router.post("/api/rebuild")
def rebuild():
    """Drop all data and rebuild index from scratch.

    Refuses with **409 Conflict** — never 500 — when the store is unreachable, a
    migration owns the index, an indexing run is active, or another destructive
    operation holds the lock. The request is well-formed; the server is
    temporarily unable to honour it, and that is a conflict, not an error.
    """
    from ragtools.service import destructive

    owner = get_owner()
    try:
        with destructive.destructive_operation(owner, operation="rebuild"):
            stats = owner.rebuild()
    except destructive.OperationRefused as refused:
        raise HTTPException(status_code=409, detail={
            "error": "rebuild_refused",
            "code": refused.code,
            "message": refused.reason,
        })
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


@router.post("/api/migration/resume")
def migration_resume():
    """Resume a parked layout migration. The remedy, where it can actually run.

    On a managed installation the service is the only process that can do this:
    it owns the engine, and ``rag upgrade --resume`` refuses while the service is
    up and cannot build a client while it is down. So the CLI forwards here
    instead of failing in both directions.

    Returns 202 — the rebuild runs on its own thread and can take hours; an HTTP
    request must not be the thing holding it.
    """
    from ragtools.upgrade import relayout

    owner = get_owner()
    plan = relayout.active_plan(owner.settings)
    if plan is None:
        return {"status": "no_migration",
                "message": "no migration is pending on this installation"}

    ok, detail = owner.storage_reachable()
    if not ok:
        raise HTTPException(status_code=409, detail={
            "error": "storage_unreachable",
            "message": f"the vector store is not reachable ({detail}); the "
                       f"rebuild would be parked again immediately",
        })

    from fastapi.responses import JSONResponse

    from ragtools.service.app import resume_migration

    # reset=True: an operator asking for this has fixed the cause, and only they
    # know that. Automatic retries stay bounded precisely so this one need not be.
    started = resume_migration(reset=True)
    report = relayout.progress(owner.settings, plan)
    return JSONResponse(status_code=202, content={
        "status": "resuming" if started else "not_started",
        "plan": plan,
        "state": report.describe() if report else "",
    })


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
    # Project Mode: docs (default) / code / general.
    mode: Optional[Literal["docs", "code", "general"]] = None
    # Shared framework / vendored-dependency roots. Declaring one moves that
    # tree out of the project's own collection and into a corpus shared by
    # every project on the same build.
    dependency_paths: list[str] = []


class ProjectUpdateRequest(BaseModel):
    name: Optional[str] = None
    path: Optional[str] = None
    enabled: Optional[bool] = None
    ignore_patterns: Optional[list[str]] = None
    # None = field not provided (leave unchanged). docs/code/general set the Mode.
    mode: Optional[Literal["docs", "code", "general"]] = None
    # None = not provided (leave unchanged); [] = explicitly clear.
    dependency_paths: Optional[list[str]] = None


def _validate_project_id(pid: str) -> str | None:
    """Return an error string if ``pid`` is invalid, else ``None``.

    Delegates to the shared validator (:func:`ragtools.identity.validate_project_id`)
    so HTTP, CLI online, and CLI offline all enforce one rule (plan §11.1). The
    error-string return shape is preserved for existing route callers.
    """
    from ragtools.identity import InvalidProjectId, validate_project_id

    try:
        validate_project_id(pid)
        return None
    except InvalidProjectId as exc:
        return str(exc)


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
            "mode": p.mode,
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


def _schedule_reindex(project_id: str):
    """Background DELETE-AWARE **incremental** sync — used when a Mode changes.

    Previously this ran ``reindex_project`` (= ``delete_project_data`` +
    ``run_full_index``), which purged the project and re-embedded everything.
    Widening `docs -> general` therefore deleted perfectly good documentation
    embeddings and paid to recompute them — minutes of wasted work on a large
    project, plus a window where the project was missing from search.

    ``run_incremental_index`` already does the correct thing in BOTH directions:

    * ``deleted_paths = tracked_paths - current_paths`` — narrowing purges the
      files (and their Qdrant points) that the new mode excludes;
    * unchanged files fail ``file_changed()`` and are **skipped**, so they are
      never re-embedded.

    A full purge-and-rebuild is still available deliberately via
    ``reindex_project`` (e.g. after a chunking or model change), but it is no
    longer what a mode change triggers.
    """
    import threading
    from ragtools.service.activity import log_activity

    # Prefer the job engine: durable record, progress, cancellation.
    try:
        runtime = get_runtime()
        runtime.submit("index", {"project": project_id, "full": False,
                                 "cause": "mode_change"},
                       idempotency_key=f"mode-sync:{project_id}")
        log_activity("info", "indexer",
                     f"Syncing {project_id} after mode change (incremental)")
        return
    except RuntimeError:
        pass  # no runtime (degraded/tests) — fall back to a thread

    def _run():
        try:
            log_activity("info", "indexer", f"Syncing {project_id} (mode change)...")
            stats = get_owner().run_incremental_index(project_id=project_id)
            log_activity("success", "indexer",
                f"Mode sync {project_id}: {stats.get('indexed', 0)} added, "
                f"{stats.get('deleted', 0)} removed, {stats.get('skipped', 0)} kept")
        except Exception as e:
            log_activity("error", "indexer", f"Mode sync failed for {project_id}: {e}")

    timer = threading.Timer(3.0, _run)
    timer.daemon = False
    timer.start()


def _adopt_paths_into_catalog(settings, project, paths: list[str]) -> list[str]:
    """Translate raw dependency paths into catalog entries + link ids.

    Keeps the legacy path-based API and the catalog from becoming two competing
    sources of truth. A path already in the catalog is REUSED rather than added
    again — which is exactly the deduplication the catalog exists for, and it
    means two projects naming the same folder converge on one entry and one
    collection instead of racing to create two.
    """
    from ragtools.config import DependencyConfig
    from ragtools.dependency_catalog import (
        _dependency_path_key, _unique_dependency_id, find_by_path,
    )

    catalog = list(settings.dependencies)
    taken = {entry.id for entry in catalog}
    linked: list[str] = []
    for raw in paths or []:
        text = str(raw).strip()
        if not text:
            continue
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = Path(project.path) / candidate
        entry = find_by_path(catalog, str(candidate))
        if entry is None:
            entry = DependencyConfig(
                id=_unique_dependency_id(candidate, taken),
                name=candidate.name or "dependency",
                path=str(candidate),
            )
            taken.add(entry.id)
            catalog.append(entry)
        if entry.id not in linked:
            linked.append(entry.id)
    settings.dependencies = catalog
    return linked


def _schedule_framework_sync(cause: str):
    """Background reconcile of declared dependencies -> framework corpora.

    Declaring a dependency has no effect until this runs: the scanner already
    excludes the root from the project scan, so between the config write and
    the sync those files are in NO collection. Every write path that can change
    ``dependency_paths`` must schedule it.

    Idempotent and cheap when nothing changed, so an extra call is harmless.
    """
    import threading
    from ragtools.service.activity import log_activity

    try:
        runtime = get_runtime()
        # No idempotency key: a second edit must not be swallowed as a
        # duplicate of the first, which would leave the later change unapplied.
        runtime.submit("sync_frameworks", {"cause": cause})
        log_activity("info", "indexer", f"Framework sync queued — {cause}")
        return
    except RuntimeError:
        pass  # no runtime (degraded/tests) — fall back to a thread

    def _run():
        try:
            synced = get_owner().sync_frameworks()
            linked = [e for e in synced if e.get("action") != "released"]
            freed = [e for e in synced if e.get("action") == "released"]
            log_activity("success", "indexer",
                         f"Framework sync: {len(linked)} linked, {len(freed)} released")
        except Exception as e:  # noqa: BLE001
            log_activity("error", "indexer", f"Framework sync failed: {e}")

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
        mode=req.mode or "docs",
        dependency_paths=req.dependency_paths,
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
    # Declared dependencies are excluded from that scan, so their corpus only
    # exists once the framework sync runs. Without this the files would simply
    # vanish from search.
    if req.dependency_paths:
        _schedule_framework_sync(f"project '{req.id}' added with dependencies")

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
    # Project Mode: capture the value before the change so we reindex only when
    # it actually flips (G1: delete-aware reindex below).
    old_mode = project.mode
    if req.mode is not None:
        project.mode = req.mode
    new_mode = project.mode
    # Same rule for dependencies: compare before/after so an unrelated edit
    # (renaming the project) never triggers a framework re-index.
    #
    # `dependency_paths` is a legacy INPUT: it is translated into catalog
    # entries and links rather than stored raw, so there is exactly one
    # in-memory mechanism. Storing both would make whichever one the caller did
    # not touch win silently.
    old_deps = list(project.dependencies)
    if req.dependency_paths is not None:
        project.dependencies = _adopt_paths_into_catalog(
            settings, project, req.dependency_paths)
        project.dependency_paths = []
    deps_changed = list(project.dependencies) != old_deps

    from ragtools.service.pages import _save_projects_to_toml
    _save_projects_to_toml(list(settings.projects))
    get_owner().update_projects(list(settings.projects))

    from ragtools.service.activity import log_activity
    log_activity("info", "config", f"Project updated: {project_id}")
    _restart_watcher_if_running()
    if new_mode != old_mode:
        _schedule_reindex(project_id)
    if deps_changed:
        # Newly declared roots need their corpus built and the project's own
        # copy purged; removed roots need unlinking. sync_frameworks reconciles
        # both directions, so one job covers either edit.
        _schedule_framework_sync(f"project '{project_id}' dependencies changed")
        if set(old_deps) - set(project.dependency_paths):
            # A root that is no longer declared stops being excluded from the
            # project scan, so its files must come back into the project's own
            # collection — otherwise un-declaring silently drops them from
            # search entirely.
            _schedule_reindex(project_id)
    return {
        "status": "updated",
        "project": {"id": project.id, "name": project.name, "path": project.path},
        "dependencies_changed": deps_changed,
    }


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


class ModeRequest(BaseModel):
    mode: Literal["docs", "code", "general"]


@router.post("/api/projects/{project_id}/mode")
def project_set_mode(project_id: str, req: ModeRequest):
    """Set a project's Mode (docs / code / general) and reindex if it changed.

    A single-purpose endpoint (vs PUT /api/projects/{id}) so the CLI/MCP can set
    the Mode without risking a stale name/path overwrite. Reindex is delete-aware
    (G1) so narrowing the Mode purges the project's now-excluded chunks.
    """
    settings = get_settings()
    project = next((p for p in settings.projects if p.id == project_id), None)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    old_mode = project.mode
    project.mode = req.mode
    new_mode = project.mode

    from ragtools.service.pages import _save_projects_to_toml
    _save_projects_to_toml(list(settings.projects))
    get_owner().update_projects(list(settings.projects))

    from ragtools.service.activity import log_activity
    log_activity("info", "config", f"Project {project_id} mode -> {req.mode}")
    _restart_watcher_if_running()
    if new_mode != old_mode:
        _schedule_reindex(project_id)
    return {
        "status": "mode_set", "project_id": project_id,
        "mode": new_mode,
        "reindex_scheduled": new_mode != old_mode,
    }


# --- Dependencies / framework corpora ---


def _inspect_dependencies(project_path: str, declared: list[str]) -> dict:
    """Resolve and describe declared dependency roots WITHOUT changing anything.

    Takes the project PATH rather than a project, so the dry-run can validate
    against the folder currently typed into the form — a project being moved
    and given a dependency in the same edit would otherwise be checked against
    its old root and reported as broken.

    Shared by the read endpoint and the dry-run preview so what the user is
    shown before saving is produced by the same code that will act on it — the
    usual failure here is a preview that validates differently from the writer
    and blesses a path the indexer then rejects.
    """
    from ragtools.frameworks import describe_dependency, resolve_dependency_roots
    from ragtools.identity import framework_collection_name

    roots, problems = resolve_dependency_roots(project_path, declared)
    by_declared = {r.declared: r for r in roots}
    # Problems are emitted as f"{text!r}: ..." — map them back to the line the
    # user typed so each row carries its own verdict instead of a page-level
    # blob they have to match up by eye.
    problem_for: dict[str, str] = {}
    for problem in problems:
        for text in declared:
            prefix = f"{text!r}: "
            if problem.startswith(prefix):
                problem_for.setdefault(text, problem[len(prefix):])
                break

    owner = get_owner()
    frameworks = owner.framework_registry
    entries = []
    for raw in declared:
        text = str(raw).strip()
        if not text:
            continue
        root = by_declared.get(text)
        if root is None:
            entries.append({
                "declared": text, "ok": False,
                "problem": problem_for.get(text, "could not be resolved"),
            })
            continue

        info = describe_dependency(root.path)
        entry = {
            "declared": text,
            "ok": True,
            "problem": "",
            "resolved": str(root.path),
            "inside_project": root.inside_project,
            "detector": info.detector if info else "",
            "framework": info.name if info else "",
            "version": info.version if info else "",
            "edition": info.edition if info else "",
            "build_id": info.build_id if info else None,
        }
        if info is not None:
            collection = framework_collection_name(
                info.name, version=info.version, edition=info.edition,
                build_id=info.build_id,
            )
            entry["collection"] = collection
            # "Already indexed by another project" is the single most useful
            # fact here: it means declaring this costs nothing and the corpus
            # is shared rather than duplicated.
            existing = frameworks.get(collection) if frameworks else None
            entry["exists"] = existing is not None
            linked = frameworks.projects_for(collection) if frameworks else []
            entry["shared_with"] = len(linked)
            entry["points"] = owner._count_points(collection) if existing else 0
        entries.append(entry)
    return {"entries": entries, "problems": problems}


@router.get("/api/projects/{project_id}/dependencies")
def project_dependencies(project_id: str):
    """Declared dependency roots, what they resolve to, and what is linked."""
    settings = get_settings()
    project = next((p for p in settings.projects if p.id == project_id), None)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    owner = get_owner()
    result = _inspect_dependencies(project.path, list(project.dependency_paths))
    linked: list[str] = []
    registry, frameworks = owner.registry, owner.framework_registry
    if registry is not None and frameworks is not None:
        record = registry.get(project_id)
        if record is not None:
            linked = frameworks.framework_collections_for(record.uuid)
    return {
        "project": project_id,
        "supported": owner.router.is_per_project,
        # `dependency_paths` is the LEGACY input, consumed into the catalog at
        # load — so it is empty for every project that declares through the
        # catalog, which is all of them after an upgrade. Reporting only that
        # made a project with a linked, working corpus read as declaring
        # nothing at all.
        "declared": list(project.dependency_paths),
        "declared_dependencies": list(project.dependencies or []),
        "linked_collections": linked,
        # Declared-but-not-yet-linked means the sync has not run (or failed):
        # those files are in NO collection right now, which is worth saying.
        "pending_sync": sorted(
            {e["collection"] for e in result["entries"]
             if e.get("collection") and e["collection"] not in linked}
        ),
        **result,
    }


class DependencyPreviewRequest(BaseModel):
    dependency_paths: list[str] = []


@router.post("/api/projects/{project_id}/dependencies/preview")
def project_dependencies_preview(project_id: str, req: DependencyPreviewRequest):
    """Dry run: what WOULD these paths resolve to? Changes nothing."""
    settings = get_settings()
    project = next((p for p in settings.projects if p.id == project_id), None)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return {"project": project_id,
            **_inspect_dependencies(project.path, req.dependency_paths)}


@router.get("/api/files/chunks")
def file_chunks(
    file: str = Query(..., description="Indexed file path (as stored)"),
    project: str = Query(..., description="Project the file was viewed from"),
    collection: str = Query("", description="Collection the point came from"),
):
    """Everything stored for one indexed file: chunk text, lines, symbols.

    Backs the map's detail panel. Scope is enforced in the owner — the
    collection must be one this project already reads.
    """
    try:
        return get_owner().get_file_chunks(project, file, collection or None)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Project '{project}' not found")


# --- Dependency catalog (first-class shared dependencies) ---


class DependencyCreateRequest(BaseModel):
    id: str
    path: str
    name: str = ""
    enabled: bool = True


class DependencyUpdateRequest(BaseModel):
    name: Optional[str] = None
    path: Optional[str] = None
    enabled: Optional[bool] = None


class ProjectDependencyLinkRequest(BaseModel):
    """Full replacement of a project's link list — not a delta.

    Replacing the whole set is what makes the multi-select honest: what the
    form shows IS what gets stored, with no invisible leftovers from an edit
    made in another tab.
    """

    dependencies: list[str] = []


def _save_catalog(settings) -> None:
    """Persist the catalog alongside projects (one atomic config write)."""
    from ragtools.service.pages import _save_projects_to_toml

    _save_projects_to_toml(list(settings.projects), dependencies=list(settings.dependencies))


def _catalog_entry_payload(settings, owner, entry) -> dict:
    """One catalog row: what it is, what it costs, who uses it."""
    from ragtools.dependency_catalog import projects_using

    inspected = _inspect_dependencies(str(Path(entry.path).parent or entry.path),
                                      [entry.path])
    detail = (inspected.get("entries") or [{}])[0]
    return {
        "id": entry.id,
        "name": entry.name,
        "path": entry.path,
        "enabled": entry.enabled,
        "exists": bool(detail.get("ok")),
        "problem": detail.get("problem", ""),
        "framework": detail.get("framework", ""),
        "version": detail.get("version", ""),
        "edition": detail.get("edition", ""),
        "detector": detail.get("detector", ""),
        "collection": detail.get("collection", ""),
        "points": detail.get("points", 0),
        "indexed": bool(detail.get("exists")),
        "projects": projects_using(settings.projects, entry.id),
    }


@router.get("/api/dependencies")
def dependencies_list():
    """The shared-dependency catalog, with usage and index state."""
    settings = get_settings()
    owner = get_owner()
    return {
        "supported": owner.router.is_per_project,
        "dependencies": [_catalog_entry_payload(settings, owner, d)
                         for d in settings.dependencies],
    }


@router.post("/api/dependencies")
def dependency_create(req: DependencyCreateRequest):
    """Add a shared dependency to the catalog. Indexes nothing by itself."""
    from ragtools.config import DependencyConfig
    from ragtools.dependency_catalog import CatalogError, validate_new_entry

    settings = get_settings()
    try:
        resolved = validate_new_entry(settings.dependencies, req.id, req.path)
    except CatalogError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    from ragtools.dependency_catalog import normalize_id
    entry = DependencyConfig(id=normalize_id(req.id), name=req.name or Path(resolved).name,
                             path=resolved, enabled=req.enabled)
    settings.dependencies = list(settings.dependencies) + [entry]
    _save_catalog(settings)
    get_owner().update_projects(list(settings.projects))

    from ragtools.service.activity import log_activity
    log_activity("info", "config", f"Shared dependency added: {entry.id}")
    # Nothing is indexed until a project links it — a catalog entry on its own
    # is a declaration, not a corpus.
    return {"status": "created", "dependency": {"id": entry.id, "name": entry.name,
                                                "path": entry.path}}


@router.put("/api/dependencies/{dependency_id}")
def dependency_update(dependency_id: str, req: DependencyUpdateRequest):
    """Rename, re-point or disable a catalog entry."""
    from ragtools.dependency_catalog import CatalogError, validate_new_entry

    settings = get_settings()
    entry = settings.dependency(dependency_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Dependency '{dependency_id}' not found")

    old_path = entry.path
    if req.path is not None:
        try:
            entry.path = validate_new_entry(settings.dependencies, dependency_id,
                                            req.path, exclude_id=dependency_id)
        except CatalogError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if req.name is not None:
        entry.name = req.name.strip() or entry.id
    if req.enabled is not None:
        entry.enabled = req.enabled

    _save_catalog(settings)
    get_owner().update_projects(list(settings.projects))

    from ragtools.service.activity import log_activity
    log_activity("info", "config", f"Shared dependency updated: {dependency_id}")
    # Re-pointing or disabling changes which corpus every linked project reads,
    # so both need a reconcile — and re-pointing also needs the projects
    # re-scanned, since the excluded tree moved.
    changed = (req.path is not None and entry.path != old_path) or req.enabled is not None
    if changed:
        _schedule_framework_sync(f"dependency '{dependency_id}' changed")
        from ragtools.dependency_catalog import projects_using
        for pid in projects_using(settings.projects, dependency_id):
            _schedule_reindex(pid)
    return {"status": "updated", "dependency": {"id": entry.id, "name": entry.name,
                                                "path": entry.path,
                                                "enabled": entry.enabled}}


@router.delete("/api/dependencies/{dependency_id}")
def dependency_delete(dependency_id: str, cascade: bool = Query(False)):
    """Remove a catalog entry. Refused while projects link it unless cascade."""
    from ragtools.dependency_catalog import projects_using, unlink_everywhere

    # Called directly (UI fragment, tests), an unpassed `cascade` is FastAPI's
    # Query sentinel — which is TRUTHY. Trusting it verbatim turns the refusal
    # safeguard into a silent cascade: exactly backwards for a destructive
    # flag, where anything short of an explicit yes must mean no.
    cascade = cascade is True

    settings = get_settings()
    entry = settings.dependency(dependency_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Dependency '{dependency_id}' not found")

    users = projects_using(settings.projects, dependency_id)
    if users and not cascade:
        # Refusing by default: deleting a linked dependency silently changes
        # what several projects search. Making that explicit costs one click.
        raise HTTPException(
            status_code=409,
            detail=(f"'{entry.name}' is used by {', '.join(users)}. "
                    "Unlink it there first, or delete with cascade to remove it "
                    "from those projects."),
        )

    unlinked = unlink_everywhere(settings.projects, dependency_id) if users else []
    settings.dependencies = [d for d in settings.dependencies if d.id != dependency_id]
    _save_catalog(settings)
    get_owner().update_projects(list(settings.projects))

    from ragtools.service.activity import log_activity
    log_activity("warning", "config",
                 f"Shared dependency removed: {dependency_id}"
                 + (f" (unlinked from {', '.join(unlinked)})" if unlinked else ""))
    _restart_watcher_if_running()
    if unlinked:
        # The corpus is released and, if nobody else reads it, dropped; each
        # affected project is re-scanned so the files return to its own
        # collection now that nothing excludes them.
        _schedule_framework_sync(f"dependency '{dependency_id}' removed")
        for pid in unlinked:
            _schedule_reindex(pid)
    return {"status": "removed", "dependency_id": dependency_id, "unlinked_from": unlinked}


@router.get("/api/projects/{project_id}/dependency-options")
def project_dependency_options(project_id: str):
    """Catalog entries with a per-project verdict — powers the multi-select.

    A catalog entry can be perfectly valid and still illegal for ONE project
    (its own root, or a parent of it), so the answer is per project, not
    global.
    """
    from ragtools.dependency_catalog import check_link

    settings = get_settings()
    project = next((p for p in settings.projects if p.id == project_id), None)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    linked = set(project.dependencies or [])
    options = []
    for entry in settings.dependencies:
        verdict = check_link(project, entry)
        options.append({
            "id": entry.id, "name": entry.name, "path": entry.path,
            "enabled": entry.enabled,
            "selected": entry.id in linked,
            "selectable": verdict.ok and entry.enabled,
            "reason": verdict.reason if verdict.blocked
                      else ("" if entry.enabled else "this dependency is disabled"),
        })
    return {"project": project_id, "options": options}


@router.put("/api/projects/{project_id}/dependencies")
def project_dependencies_set(project_id: str, req: ProjectDependencyLinkRequest):
    """Replace a project's dependency links (multi-select save)."""
    from ragtools.dependency_catalog import CatalogError, validate_link_set

    settings = get_settings()
    project = next((p for p in settings.projects if p.id == project_id), None)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    try:
        wanted = validate_link_set(project, settings.dependencies, req.dependencies)
    except CatalogError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    before = list(project.dependencies or [])
    if wanted == before:
        return {"status": "unchanged", "project": project_id, "dependencies": wanted}

    project.dependencies = wanted
    _save_catalog(settings)
    get_owner().update_projects(list(settings.projects))

    from ragtools.service.activity import log_activity
    log_activity("info", "config",
                 f"Project {project_id} dependencies -> {', '.join(wanted) or '(none)'}")
    _restart_watcher_if_running()
    _schedule_framework_sync(f"project '{project_id}' dependency links changed")
    if set(before) - set(wanted):
        # An unlinked root stops being excluded from the project scan, so its
        # files must come back into the project's own collection — otherwise
        # unlinking silently drops them from search entirely.
        _schedule_reindex(project_id)
    return {"status": "updated", "project": project_id, "dependencies": wanted,
            "added": sorted(set(wanted) - set(before)),
            "removed": sorted(set(before) - set(wanted))}


@router.get("/api/frameworks")
def frameworks_list():
    """Framework corpora, with the projects sharing each one."""
    owner = get_owner()
    frameworks = owner.framework_registry
    registry = owner.registry
    if frameworks is None or registry is None:
        return {"supported": False, "frameworks": []}

    # ProjectRecord names the field `project_id` — links store UUIDs, so this
    # map is what turns a corpus's linked set back into names a user knows.
    uuid_to_id = {r.uuid: r.project_id for r in registry.list(include_archived=True)}

    # Enumerate the REGISTRY, not the router. The router only knows corpora a
    # project already links, and linking is the LAST step of a sync — so a
    # 32,782-file dependency being indexed right now would be reported as
    # "no frameworks", which reads as "nothing happened" for the entire run.
    out = []
    for record in frameworks.list():
        collection = record.collection_name
        linked = frameworks.projects_for(collection)
        out.append({
            "collection": collection,
            "name": record.name,
            "version": record.version,
            "edition": record.edition,
            "build_id": record.build_id,
            "canonical_root": record.canonical_root,
            "points": owner._count_points(collection),
            "projects": sorted(uuid_to_id.get(u, u) for u in linked),
            # Registered but unlinked means the corpus is mid-index: it exists
            # and is filling up, but no project searches it yet.
            "linked": bool(linked),
            "state": "ready" if linked else "indexing",
        })
    return {"supported": True, "frameworks": out}


@router.post("/api/frameworks/sync")
def frameworks_sync():
    """Reconcile declared dependencies -> framework corpora (background job)."""
    if not get_owner().router.is_per_project:
        raise HTTPException(
            status_code=409,
            detail="Framework corpora require the per-project collection layout",
        )
    _schedule_framework_sync("requested from the admin panel")
    return {"status": "queued"}


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
        # Storage + collection architecture. `storage_api_key` is deliberately
        # ABSENT: it is a credential, and /api/config is readable by anything
        # that can reach the loopback port.
        "storage_backend": getattr(settings, "storage_backend", "embedded"),
        "storage_url": getattr(settings, "storage_url", None),
        "collection_strategy": getattr(settings, "collection_strategy", "shared"),
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

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _start_watcher_locked() -> dict:
    """Construct + start the watcher thread. Caller MUST hold ``_watcher_lock``.

    Idempotent — a live thread is left untouched. These lock-free internals
    exist so the FastAPI lifespan (autostart) and the project-edit restart
    path can drive startup without re-entering the non-reentrant lock through
    the route handlers (which previously self-deadlocked the restart thread).
    """
    global _watcher_thread, _watcher_autostart_error, _watcher_autostart_error_at
    if _watcher_thread is not None and _watcher_thread.is_alive():
        return {"status": "already_running"}

    from ragtools.service.watcher_thread import WatcherThread
    owner = get_owner()
    settings = get_settings()
    _watcher_thread = WatcherThread(owner=owner, settings=settings)
    _watcher_thread.start()
    _watcher_autostart_error = None
    _watcher_autostart_error_at = None
    project_count = len(settings.enabled_projects) if settings.has_explicit_projects else 0
    logger.info("Watcher started: %d projects", project_count)
    return {"status": "started", "project_count": project_count}


def _stop_watcher_locked() -> dict:
    """Stop + join the watcher thread. Caller MUST hold ``_watcher_lock``."""
    global _watcher_thread
    if _watcher_thread is None or not _watcher_thread.is_alive():
        return {"status": "not_running"}
    _watcher_thread.stop()
    _watcher_thread.join(timeout=5)
    _watcher_thread = None
    logger.info("Watcher stopped")
    return {"status": "stopped"}


def stop_watcher_for_shutdown() -> dict:
    """Stop the watcher during service teardown. Acquires the lock itself.

    The lifespan's counterpart to :func:`autostart_watcher`, which had none —
    the watcher was started by the service lifecycle and never stopped by it,
    so it went on writing activity into the runtime store while that store was
    being closed. Separate from the HTTP stop path because this one must never
    raise: shutdown continues regardless.
    """
    with _watcher_lock:
        try:
            return _stop_watcher_locked()
        except Exception as exc:  # noqa: BLE001
            logger.warning("watcher did not stop cleanly: %s", exc)
            return {"status": "error", "detail": str(exc)}


def autostart_watcher() -> dict:
    """Start the watcher from the service lifecycle (FastAPI lifespan) — M3.

    The watcher's startup is owned by the service process rather than a delayed
    HTTP self-POST that could miss the readiness window and leave the watcher
    silently inactive. Respects an explicit user stop (desired-state) and NEVER
    raises: a construction failure is recorded so it surfaces via the derived
    watcher ``state`` (``autostart_failed``) and ``/health`` degraded instead of
    crashing startup.
    """
    global _watcher_autostart_error, _watcher_autostart_error_at
    with _watcher_lock:
        if not _watcher_desired_run:
            return {"status": "skipped_user_stopped"}
        try:
            return _start_watcher_locked()
        except Exception as e:  # noqa: BLE001 — startup must never die here
            _watcher_autostart_error = f"{type(e).__name__}: {e}"
            _watcher_autostart_error_at = _now_iso()
            logger.exception("Watcher autostart failed")
            return {"status": "error", "error": _watcher_autostart_error}


@router.post("/api/watcher/start")
def watcher_start():
    """Start the file watcher as a background thread (explicit user/API start)."""
    global _watcher_desired_run

    with _watcher_lock:
        _watcher_desired_run = True  # explicit (re)arm — clears a prior user stop
        return _start_watcher_locked()


@router.post("/api/watcher/stop")
def watcher_stop():
    """Stop the file watcher (records the user's intent to stay stopped)."""
    global _watcher_desired_run, _watcher_thread
    global _watcher_autostart_error, _watcher_autostart_error_at

    with _watcher_lock:
        _watcher_desired_run = False  # user intent: do not auto-restart
        result = _stop_watcher_locked()
        # A deliberate stop also clears prior failure markers so the watcher
        # reports cleanly as "stopped" — not autostart_failed / crashed / gave_up.
        # The operator turned it off on purpose; system-health must not keep
        # flagging it as an error (Decision 17). The persistent crash marker +
        # toast already fired, so nothing diagnostic is lost. Dropping the
        # dead-but-not-None thread ref discards its stale error fingerprint too.
        _watcher_autostart_error = None
        _watcher_autostart_error_at = None
        _watcher_thread = None
        return result


def _derive_watcher_state(
    snap: dict,
    alive: bool,
    *,
    desired_run: bool = True,
    autostart_error: Optional[str] = None,
) -> str:
    """Best-effort lifecycle label so consumers can tell apart the states the
    raw null/0 observability fields otherwise collapse together (report L3 / the
    real signal A-007 lacked).

    - running         : daemon thread alive
    - autostart_failed: a lifecycle autostart could not construct/start it (M3)
    - gave_up         : exceeded the retry budget (consecutive_failures >= _MAX_RETRIES)
    - crashed         : exited with a recorded error
    - stopped         : an explicit user stop is in effect (desired_run is False)
    - exited          : started then exited cleanly (e.g. no enabled projects)
    - inactive        : never started / not yet autostarted

    ``desired_run``/``autostart_error`` default to the "should be running, no
    autostart failure" case so older positional callers keep their behavior.
    """
    from ragtools.service.watcher_thread import WatcherThread
    if alive:
        return "running"
    if autostart_error:
        return "autostart_failed"
    if snap.get("consecutive_failures", 0) >= WatcherThread._MAX_RETRIES:
        return "gave_up"
    if snap.get("last_error"):
        return "crashed"
    if not desired_run:
        return "stopped"
    if snap.get("last_started_at"):
        return "exited"
    return "inactive"


def _watcher_observability_snapshot() -> dict:
    """Pull the /api/watcher/status observability fields off the daemon thread
    (plus a derived ``state``), with safe defaults when no thread exists.

    Lives inside the route module (not the watcher) so the response shape
    is owned by the same file that defines the contract.
    """
    snap = {
        "last_started_at": None,
        "last_error": None,
        "last_error_at": None,
        "consecutive_failures": 0,
    }
    alive = False
    t = _watcher_thread
    if t is not None:
        try:
            snap.update(t.get_state_snapshot())
        except Exception:
            # A flaky introspection must never make the route 5xx.
            pass
        try:
            alive = t.is_alive()
        except Exception:
            alive = False
    # Snapshot the desired-state + autostart globals into locals once (lock-free
    # reads) so the derived state and the emitted keys below are mutually
    # consistent within one response, even if a concurrent start/stop mutates
    # them mid-call. Per-field reads are GIL-atomic; this just avoids pairing a
    # state derived from a stale error with a since-cleared error key.
    desired_run = _watcher_desired_run
    autostart_error = _watcher_autostart_error
    autostart_error_at = _watcher_autostart_error_at
    snap["state"] = _derive_watcher_state(
        snap,
        alive,
        desired_run=desired_run,
        autostart_error=autostart_error,
    )
    # Additive M3 fields: the user's desired-state, and the autostart failure
    # detail (only when one occurred). Older clients ignore the extra keys.
    snap["desired"] = "run" if desired_run else "stopped"
    if autostart_error:
        snap["autostart_error"] = autostart_error
        snap["autostart_error_at"] = autostart_error_at
    return snap


@router.get("/api/watcher/status")
def watcher_status():
    """Check watcher state.

    The four observability fields (last_started_at, last_error,
    last_error_at, consecutive_failures) are additive — see
    docs/decisions.md Decision 16 and docs/wiki-src/Reference/HTTP-API.md.
    Older clients that only look at running/paths/project_count continue
    to work unchanged.
    """
    settings = get_settings()
    obs = _watcher_observability_snapshot()
    with _watcher_lock:
        if _watcher_thread is not None and _watcher_thread.is_alive():
            if settings.has_explicit_projects:
                paths = [p.path for p in settings.enabled_projects]
                return {
                    "running": True,
                    "paths": paths,
                    "project_count": len(paths),
                    **obs,
                }
            else:
                return {"running": True, "project_count": 0, **obs}
        return {"running": False, **obs}


def _restart_watcher_if_running():
    """Restart the watcher if it's currently running. Called after project config changes.

    Runs in a background thread so it doesn't block the HTTP response.
    """
    import threading as _th
    def _do_restart():
        with _watcher_lock:
            if _watcher_thread is not None and _watcher_thread.is_alive():
                # Call the lock-free internals — NOT the route handlers, which
                # re-acquire _watcher_lock and would self-deadlock this thread
                # (and then hang every /api/watcher/status reader behind it).
                # A restart of a running watcher keeps desired_run == True.
                _stop_watcher_locked()
                _start_watcher_locked()
                logger.info("Watcher restarted after project config change")
    _th.Thread(target=_do_restart, daemon=True).start()


# --- Semantic Map ---

@router.get("/api/map/points")
def map_points(project: Optional[str] = Query(None, description="Scope to one project")):
    """2D/3D coordinates for a balanced sample of indexed files.

    ``points`` and ``count`` keep their shape for existing clients; ``coverage``,
    ``excluded`` and ``cache`` are additive.

    ``project`` COMPUTES that project's collections rather than filtering the
    global sample. Filtering was why ``?project=rag`` answered ``count: 0`` for
    a project holding 1,716 chunks — a filter cannot recover data the sampler
    never fetched.
    """
    owner = get_owner()
    result = owner.get_map_points(project_id=project)
    points = result.get("points", [])
    return {
        "points": points,
        "count": len(points),
        "coverage": result.get("coverage", {}),
        "excluded": result.get("excluded", []),
        "cache": result.get("cache", {}),
    }


@router.post("/api/map/recompute")
def map_recompute():
    """Force recomputation of map coordinates."""
    owner = get_owner()
    result = owner.get_map_points(force_recompute=True)
    return {
        "status": "recomputed",
        "count": len(result.get("points", [])),
        "coverage": result.get("coverage", {}),
        "excluded": result.get("excluded", []),
    }


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


def _file_type_breakdown(rows) -> dict[str, int]:
    """File-count breakdown by chunk_type (documentation / code / config / other)
    for a project's indexed roster. Cheap — classify_file is extension/name-based
    (no file IO). Lets the agent see at a glance whether a project holds docs,
    code, or both, which lines up with its Mode."""
    from ragtools.chunking.languages import classify_file
    counts: dict[str, int] = {}
    for r in rows:
        fc = classify_file(r["file_path"])
        bucket = fc.chunk_type if fc is not None else "other"
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def _source_class_breakdown(rows) -> dict[str, int]:
    """File-count breakdown by source_class (owned / generated / dependency ...)
    for a project's indexed roster. Cheap — path-based, no file IO. Lets the agent
    see how much of the index is project-owned vs vendored/generated noise."""
    from ragtools.source_class import classify_source_class
    counts: dict[str, int] = {}
    for r in rows:
        bucket = classify_source_class(r["file_path"])
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def _is_stale(last_indexed, stale_hours: int, now=None) -> bool:
    """True if ``last_indexed`` is older than ``stale_hours``. Never-indexed
    (``None``) is not 'stale'. Accepts an ISO string or datetime."""
    if not last_indexed:
        return False
    from datetime import datetime
    now = now or datetime.now()
    if isinstance(last_indexed, str):
        try:
            dt = datetime.fromisoformat(last_indexed)
        except ValueError:
            return False
    elif isinstance(last_indexed, datetime):
        dt = last_indexed
    else:
        return False
    return (now - dt).total_seconds() > stale_hours * 3600


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
    file_types: dict[str, int] = {}
    source_class_breakdown: dict[str, int] = {}
    state_path = _P(settings.state_db)
    if state_path.exists():
        state = IndexState(settings.state_db)
        try:
            summary = state.get_project_summary(project_id)
            roster = state.get_all_for_project(project_id)
            file_types = _file_type_breakdown(roster)
            source_class_breakdown = _source_class_breakdown(roster)
        finally:
            state.close()

    # Mode note: in Docs mode an empty code result is "not indexed", NOT "absent".
    mode_note = ""
    if project.mode == "docs":
        mode_note = (
            "Docs mode — source code is not indexed for this project; a code "
            "search returning nothing is not evidence the feature is absent."
        )

    path = _P(project.path)
    return {
        "project_id":           project.id,
        "name":                 project.name,
        "path":                 str(path),
        "path_exists":          path.is_dir(),
        "enabled":              project.enabled,
        "mode":                 project.mode,
        "mode_note":            mode_note,
        "files":                summary["files"],
        "chunks":               summary["chunks"],
        "file_types":           file_types,
        "source_class_breakdown": source_class_breakdown,
        "last_indexed":         summary["last_indexed"],
        "stale":                _is_stale(summary["last_indexed"], settings.stale_index_hours),
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
    status = None
    try:
        owner = get_owner()
        status = owner.get_status()
        # Reuse the status record rather than recomputing it. Recomputing here
        # dropped `capabilities`, so a managed or external engine — which has no
        # brute-force ceiling at all — was reported as "over" by this endpoint
        # while /api/status said "ok" about the same index.
        pc = status.get("points_count", 0)
        scale = status.get("scale") or {"level": "unknown", "message": ""}
        checks.append({
            "component": "collection",
            "status": "warning" if scale["level"] == "over" else "ok",
            "detail": f"{pc} points",
            "scale_level": scale["level"],
            "scale_message": scale["message"],
        })
    except Exception as e:
        checks.append({"component": "collection", "status": "error", "detail": str(e)})

    # Index freshness (A-008)
    if status is not None:
        fr = status.get("freshness") or {}
        checks.append({
            "component": "index_freshness",
            "status": "warning" if fr.get("level") == "stale" else "ok",
            "detail": fr.get("message") or (fr.get("level") or "unknown"),
            "level": fr.get("level"),
            "last_indexed": fr.get("last_indexed"),
            "age_seconds": fr.get("age_seconds"),
        })

    # Watcher health — the canonical health surface was previously blind to it.
    try:
        running = _watcher_thread is not None and _watcher_thread.is_alive()
        obs = _watcher_observability_snapshot()
        state = obs.get("state")
        if running:
            wstatus, detail = "ok", "running"
        elif state == "stopped":
            # Deliberately stopped by the user — intentional, not a problem.
            wstatus, detail = "ok", "stopped by user"
        elif obs.get("autostart_error"):
            wstatus, detail = "error", obs["autostart_error"]
        elif obs.get("last_error"):
            wstatus, detail = "error", obs["last_error"]
        else:
            wstatus, detail = "warning", "not running"
        checks.append({
            "component": "watcher",
            "status": wstatus,
            "detail": detail,
            "running": running,
            "state": state,
            "last_error": obs.get("last_error"),
            "autostart_error": obs.get("autostart_error"),
            "consecutive_failures": obs.get("consecutive_failures", 0),
        })
    except Exception as e:
        checks.append({"component": "watcher", "status": "error", "detail": str(e)})

    # Startup + Watchdog (Windows only)
    # Autostart, on every platform. Reports superseded and duplicate
    # registrations rather than the first match — a machine with three of them
    # used to report "installed" and stay that way through every upgrade.
    try:
        from ragtools.service.startup import get_task_info

        info = get_task_info()
        if info is None:
            checks.append({"component": "autostart", "status": "warning",
                           "detail": "not registered — run: rag service install"})
        elif info.get("problem"):
            checks.append({"component": "autostart", "status": "warning",
                           "detail": info["problem"]})
        else:
            checks.append({"component": "autostart", "status": "ok",
                           "detail": f"{info['method']} · {info['task_name']}"})
    except Exception as exc:  # noqa: BLE001
        checks.append({"component": "autostart", "status": "error", "detail": str(exc)})

    from ragtools.platform import current_platform

    return {"checks": checks, "platform": current_platform()}


# --- Shutdown ---

@router.post("/api/shutdown")
def shutdown():
    """Graceful shutdown. Stops watcher, then signals uvicorn to exit."""
    logger.info("Shutdown requested via API")
    from ragtools.service.activity import log_activity
    log_activity("warning", "service", "Shutdown requested")

    # Stop the watcher WITHOUT recording user intent — a service shutdown must
    # not flip the desired-state flag (only an explicit POST /api/watcher/stop
    # should). Use the lock-free internal, like _restart_watcher_if_running does.
    with _watcher_lock:
        _stop_watcher_locked()

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
