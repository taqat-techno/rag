"""MCP server exposing RAG Tools to Claude CLI.

Architecture
------------
A **single** MCP server with per-tool access control. Three tool tiers:

  Core tools — always registered (3):
      - search_knowledge_base   — retrieve content from the local KB
      - list_projects           — enumerate indexed project IDs
      - index_status            — basic "is the KB ready?" check

  Optional diagnostic tools — default OFF, user grants per-tool (9):
      - service_status, recent_activity, tail_logs, crash_history,
        get_config, get_ignore_rules, get_paths, system_health,
        list_indexed_paths

  Optional project-inspection tools — default OFF (5):
      - project_status, project_summary, list_project_files,
        get_project_ignore_rules, preview_ignore_effect

  Optional project-scoped writes — default ON, guarded (5):
      - run_index, reindex_project, add_project, add_project_ignore_rule,
        remove_project_ignore_rule

Disabled tools are NEVER registered — invisible to the agent, zero token
cost, zero distraction.

Runtime modes (shared across all tools)
---------------------------------------
Two module-level mode names exist and map to each other:

  ``_mode``          ``_ops_state.mode``   meaning
  ---------          --------------------  -------------------------------
  "proxy"            "proxy"               service is up; everything works
  "direct"           "degraded"            service is down; only core search
                                           works (direct Qdrant). Ops tools
                                           that need live state refuse.

The two names exist because retrieval (core tools) has a well-defined
fallback (``direct`` mode — load encoder + open Qdrant per request), but
operational tools largely don't (they need the service's in-memory state:
activity log, watcher, scale info). Calling that ``degraded`` in the agent
envelope signals honestly that some tools won't work.

Safety boundaries (release-critical)
------------------------------------
  - All write tools (``run_index``, ``reindex_project``, ignore-rule
    add/remove) refuse in anything other than proxy mode.
  - ``reindex_project`` additionally requires ``confirm_token == project``
    to defeat blind prompt-injected invocations that don't know which
    project the user is actually working on.
  - ``add_project`` requires proxy mode (config writes need the service) and
    delegates all validation — path existence, ID uniqueness, duplicate-path
    detection — to the server. Auto-indexes after add, same as the admin UI.
  - No MCP tool can **delete** projects, shut down the service, or restore
    from backup — those stay CLI-only (destructive: wipes indexed data).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP

from ragtools.config import Settings
from ragtools.integration.mcp_common import (
    McpState,
    WriteCooldown,
    err,
    ok,
    proxy_delete,
    proxy_get,
    proxy_post,
    require_proxy,
)
from ragtools.integration import mcp_errors as _errcodes

logger = logging.getLogger(__name__)


# Module-level state. The state object holds both search-mode globals
# (_encoder, _init_error) and ops-mode plumbing (httpx proxy client, mode).
_settings: Settings | None = None
_mode: str = "uninitialized"  # "proxy" | "direct" | "uninitialized"
_http_client = None
_encoder = None
_init_error: str | None = None

# Ops-tool state — only used when ops tools are registered.
_ops_state: McpState | None = None

# Shared per-process session id. Stamped on every proxy request so audit-log
# entries carry an attribution tag (``source = "mcp:<id>"``) — lets users
# distinguish between concurrent Claude Code sessions hitting the same service.
_session_id: str | None = None

# Per-process cooldown state for agent write tools. Shared across all write
# calls in this MCP session; reset only when the MCP process restarts.
_write_cooldown = WriteCooldown()


def _cooldown_guard(tool: str) -> Optional[dict]:
    """Return a COOLDOWN err envelope if ``tool`` is still cooling down,
    otherwise None. Never marks — callers mark on successful dispatch."""
    remaining = _write_cooldown.check(tool)
    if remaining is None:
        return None
    state = _ops_state or _fallback_state()
    return err(
        state,
        f"Tool '{tool}' is cooling down; wait {remaining:.1f}s before retrying.",
        code=_errcodes.COOLDOWN,
        hint="Cooldown resets automatically after the window expires.",
        extra={"retry_after_seconds": round(remaining, 2)},
    )


mcp_app = FastMCP("ragtools")


# ---------------------------------------------------------------------------
# Initialisation — probe for the service, pick a mode, register tools
# ---------------------------------------------------------------------------


def _initialize() -> None:
    """Probe the local service, lock in a mode, and register tools.

    Core tools are always registered. Ops tools are registered per the
    ``mcp_tools`` access dict in settings (default: all enabled).
    """
    global _mode, _http_client, _settings, _encoder, _init_error, _ops_state, _session_id

    _settings = Settings()

    # --- Try proxy mode (service probe) ---
    import httpx
    import time

    url = f"http://{_settings.service_host}:{_settings.service_port}/health"
    for attempt in range(2):
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                _mode = "proxy"
                # Share the session id between the core tools and the ops
                # tools so audit-log entries from both carry the same id.
                from ragtools.integration.mcp_common import MCP_SESSION_HEADER, _new_session_id
                _session_id = _new_session_id() if _session_id is None else _session_id
                _http_client = httpx.Client(
                    base_url=f"http://{_settings.service_host}:{_settings.service_port}",
                    timeout=httpx.Timeout(5.0, read=120.0),
                    headers={MCP_SESSION_HEADER: _session_id},
                )
                _init_error = None
                logger.info("MCP initialized in PROXY mode (service at %s:%d)",
                            _settings.service_host, _settings.service_port)
                break
        except Exception as e:
            logger.debug("Service probe %d failed: %s", attempt + 1, e)
        if attempt == 0:
            time.sleep(2)

    if _mode != "proxy":
        _mode = "direct"
        logger.info("MCP initializing in DIRECT mode (service unavailable)")

        try:
            from pathlib import Path
            from ragtools.embedding.encoder import Encoder

            _encoder = Encoder(_settings.embedding_model)

            qdrant_path = Path(_settings.qdrant_path)
            if not qdrant_path.exists():
                _init_error = (
                    "Knowledge base not initialized. "
                    "Run `rag index <path>` to index your Markdown files first."
                )
            else:
                client = _settings.get_qdrant_client()
                try:
                    collections = [c.name for c in client.get_collections().collections]
                    if _settings.collection_name not in collections:
                        _init_error = (
                            f"Collection '{_settings.collection_name}' not found. "
                            "Run `rag index <path>` to create the index."
                        )
                finally:
                    del client
        except Exception as e:
            _init_error = f"Failed to initialize RAG server: {e}"
            logger.exception("Direct mode initialization failed")

    # --- Shared ops state (even in direct mode; some ops tools work from
    #     filesystem, others refuse cleanly). ---
    _ops_state = McpState(_settings)
    _ops_state.mode = _mode if _mode == "proxy" else "degraded"
    _ops_state.http = _http_client
    # Share the session id so both core and ops tools produce identical
    # ``mcp:<id>`` source tags in the activity log.
    if _session_id is None:
        _session_id = _ops_state.session_id
    else:
        _ops_state.session_id = _session_id

    # --- Register the optional tools the user has granted access to ---
    _register_ops_tools()


def _register_ops_tools() -> None:
    """Conditionally register ops tools based on settings.mcp_tools.

    Called from ``_initialize()`` after the core state is set. Each entry
    in the access dict is consulted; tools not in the dict default to
    enabled (so adding new tools upstream doesn't silently lock them out
    of old configs).
    """
    # Not an ``assert`` — python -O strips those and would hand us a
    # NoneType-access error deep inside the loop.
    if _settings is None:
        logger.error(
            "MCP: _register_ops_tools called before _initialize; "
            "skipping optional-tool registration."
        )
        return
    access = getattr(_settings, "mcp_tools", {}) or {}

    tools = [
        # Phase 1 — diagnostics
        ("service_status",           service_status),
        ("recent_activity",          recent_activity),
        ("tail_logs",                tail_logs),
        ("crash_history",            crash_history),
        ("get_config",               get_config),
        ("get_ignore_rules",         get_ignore_rules),
        ("get_paths",                get_paths),
        ("system_health",            system_health),
        ("list_indexed_paths",       list_indexed_paths),
        # Phase 2 — project inspection
        ("project_status",           project_status),
        ("project_summary",          project_summary),
        ("list_project_files",       list_project_files),
        ("get_project_ignore_rules", get_project_ignore_rules),
        ("preview_ignore_effect",    preview_ignore_effect),
        # Phase 3 — project-scoped writes (Family B)
        ("run_index",                   run_index),
        ("reindex_project",             reindex_project),
        ("add_project",                 add_project),
        ("add_project_ignore_rule",     add_project_ignore_rule),
        ("remove_project_ignore_rule", remove_project_ignore_rule),
    ]
    registered = []
    for name, fn in tools:
        if access.get(name, True):
            mcp_app.add_tool(fn, name=name)
            registered.append(name)
    logger.info("MCP registered %d/%d optional tools: %s",
                len(registered), len(tools), ", ".join(registered) or "(none)")


def _check_ready() -> Optional[str]:
    """Return error message if not ready, or None if OK.

    Treats ``failed`` mode the same as ``direct`` mode with an init error —
    core tools return the structured error message instead of attempting
    an operation that will fail deeper in the stack.
    """
    if _mode == "proxy":
        return None
    if _mode == "failed":
        return f"[RAG STARTUP ERROR] {_init_error or 'MCP initialization failed.'}"
    return _init_error


# ---------------------------------------------------------------------------
# CORE tools — always registered
# ---------------------------------------------------------------------------


@mcp_app.tool()
def search_knowledge_base(
    query: str,
    project: str | None = None,
    projects: list[str] | None = None,
    top_k: int = 10,
    structured: bool = False,
) -> str | dict:
    """Search the local Markdown knowledge base for information relevant to a query.

    USE FIRST for any project-specific knowledge question. Returns
    formatted context with source attribution and confidence scores.

    Scope:
      - Pass neither ``project`` nor ``projects`` → search ALL indexed content.
      - Pass ``project="foo"`` → search only project ``foo``.
      - Pass ``projects=["a","b","c"]`` → search the union of those projects.

    Output shape:
      - ``structured=False`` (default) → returns a formatted string suitable
        for direct injection into Claude's context (backward compatible).
      - ``structured=True`` → returns a dict::

            {
              "context": "...formatted string, same as default mode...",
              "results": [ {score, confidence, text, file_path,
                            project_id, headings}, ... ],
              "meta":    {query, count, project, projects, top_k, compact}
            }

        Use structured mode when the agent needs to count/filter/rank
        results programmatically rather than just read them.

    Args:
        query:    Natural-language search query.
        project:  Optional single project ID.
        projects: Optional list of project IDs (union search). Takes
                  precedence over ``project`` when both are given.
        top_k:    Maximum number of results to return (default 10).
        structured: When True, returns a structured dict instead of a string.
    """
    if not query or not query.strip():
        if structured:
            return {
                "context": "[RAG ERROR] Query cannot be empty.",
                "results": [],
                "meta": {"query": query, "count": 0, "error_code": _errcodes.INVALID_ARG},
            }
        return "[RAG ERROR] Query cannot be empty."

    if _mode == "proxy":
        return _proxy_search(query.strip(), project, projects, top_k, structured)
    return _direct_search(query.strip(), project, projects, top_k, structured)


@mcp_app.tool()
def list_projects() -> str:
    """List all indexed projects in the knowledge base.

    USE TO discover valid project IDs for the ``project`` parameter of
    ``search_knowledge_base``.
    """
    if _mode == "proxy":
        return _proxy_list_projects()
    return _direct_list_projects()


@mcp_app.tool()
def index_status() -> str:
    """Quick "is the knowledge base ready?" check.

    USE TO decide whether a knowledge-base query can succeed right now.
    For a richer operational view, the optional ``service_status`` tool
    is more detailed.
    """
    if _mode == "proxy":
        return _proxy_index_status()
    return _direct_index_status()


# ---------------------------------------------------------------------------
# OPTIONAL tools — registered per settings.mcp_tools
# Each has a WHEN / DO NOT USE first-line docstring to guide agent selection.
# ---------------------------------------------------------------------------


def service_status() -> dict:
    """Live state of the RAG service (collection, watcher, scale, mode).

    USE WHEN: diagnosing failures, reporting status to the user, or
              deciding whether search results may be stale.
    DO NOT USE: for general knowledge queries — ``index_status`` is the
                lighter "is the KB ready?" check.
    """
    if _ops_state is None or _ops_state.mode != "proxy":
        return err(_ops_state or _fallback_state(),
                   "Service is not running — status unavailable.",
                   code=_errcodes.SERVICE_DOWN,
                   hint="Start the service with: rag service start")

    parts: dict = {}
    for label, path in (("status", "/api/status"), ("watcher", "/api/watcher/status")):
        r = proxy_get(_ops_state, path)
        parts[label] = r["data"] if r.get("ok") else {"error": r.get("error", "unknown")}
    return ok(_ops_state, parts)


def recent_activity(limit: int = 50, level: str | None = None) -> dict:
    """Structured slice of the service activity log.

    USE WHEN: investigating "search is wrong" / "nothing got indexed" or
              tracing what happened between two user actions.
    DO NOT USE: for general knowledge queries.

    Args:
        limit: Max events (1-200).
        level: Optional filter — ``info`` / ``warning`` / ``error`` / ``success``.
    """
    limit = max(1, min(int(limit), 200))
    r = proxy_get(_ops_state, "/api/activity", params={"limit": limit})
    if not r.get("ok"):
        return r
    events = r["data"].get("events", [])
    if level:
        events = [e for e in events if e.get("level", "").lower() == level.lower()]
    return ok(_ops_state, {"events": events, "count": len(events)})


def tail_logs(source: str = "service", limit: int = 50) -> dict:
    """Last N lines of a whitelisted service log file.

    USE WHEN: the user asks you to help debug a failure or
              ``service_status`` returned something concerning.
    DO NOT USE: for general knowledge queries.

    Args:
        source: One of ``service``, ``watcher``, ``launcher``, ``watchdog``,
                ``supervisor``, ``tray``. Other values are rejected.
        limit:  Max lines (capped at 500 to keep responses small).
    """
    if _ops_state is not None and _ops_state.mode == "proxy":
        return proxy_get(_ops_state, "/api/logs/tail",
                         params={"source": source, "limit": limit})
    try:
        from ragtools.service.logs import tail
        state = _ops_state or _fallback_state()
        data = tail(state.settings, source=source, limit=limit)
        if "error" in data:
            return err(state, data["error"],
                       code=_errcodes.INVALID_ARG,
                       hint=f"Valid sources: {data.get('available_sources')}")
        return ok(state, data)
    except Exception as e:
        return err(_ops_state or _fallback_state(),
                   f"Log tail failed: {e}", code=_errcodes.BACKEND_ERROR)


def crash_history() -> dict:
    """Unreviewed crash markers (service / supervisor / watcher give-ups).

    USE WHEN: the user asks "did anything go wrong recently?" or you need
              to explain why the index may be stale.
    DO NOT USE: unless the question is about RAG system reliability.
    """
    try:
        from ragtools.service.crash_history import list_unreviewed_crashes
        state = _ops_state or _fallback_state()
        items = list_unreviewed_crashes(state.settings)
        return ok(state, {"count": len(items), "items": items})
    except Exception as e:
        return err(_ops_state or _fallback_state(),
                   f"Could not read crash markers: {e}",
                   code=_errcodes.BACKEND_ERROR)


def get_config() -> dict:
    """Current RAG Tools configuration — chunks, paths, notifications, projects, ports.

    USE WHEN: explaining surprising behaviour to the user or auditing their setup.
    DO NOT USE: for general knowledge queries.
    """
    if _ops_state is not None and _ops_state.mode == "proxy":
        return proxy_get(_ops_state, "/api/config")
    try:
        state = _ops_state or _fallback_state()
        s = state.settings
        payload = {
            "embedding_model": s.embedding_model,
            "chunk_size": s.chunk_size,
            "chunk_overlap": s.chunk_overlap,
            "top_k": s.top_k,
            "score_threshold": s.score_threshold,
            "collection_name": s.collection_name,
            "service_host": s.service_host,
            "service_port": s.service_port,
            "log_level": s.log_level,
            "qdrant_path": s.qdrant_path,
            "state_db": s.state_db,
            "desktop_notifications": s.desktop_notifications,
            "projects": [p.model_dump() for p in s.projects],
        }
        return ok(state, payload)
    except Exception as e:
        return err(_ops_state or _fallback_state(),
                   f"Could not load config: {e}",
                   code=_errcodes.BACKEND_ERROR)


def get_ignore_rules() -> dict:
    """Full set of ignore rules that decide which files get indexed.

    USE WHEN: the user asks "why isn't X being indexed?" — ignore rules
              are the most common cause. Three layers: built-in defaults,
              config ``ignore_patterns``, and ``.ragignore`` files.
    DO NOT USE: for general queries.
    """
    try:
        from ragtools.ignore import IgnoreRules
        state = _ops_state or _fallback_state()
        rules = IgnoreRules(
            content_root=state.settings.content_root,
            global_patterns=state.settings.ignore_patterns,
            use_ragignore=state.settings.use_ragignore_files,
        )
        patterns = rules.get_all_patterns()
        return ok(state, {
            "built_in": list(patterns.get("built-in", [])),
            "config": list(patterns.get("config", [])),
            "ragignore_files_enabled": state.settings.use_ragignore_files,
        })
    except Exception as e:
        return err(_ops_state or _fallback_state(),
                   f"Could not load ignore rules: {e}",
                   code=_errcodes.BACKEND_ERROR)


def get_paths() -> dict:
    """Absolute filesystem paths RAG Tools uses (data dir, logs, backups, PIDs).

    USE WHEN: you need to tell the user where to look for a log or where
              the index lives.
    DO NOT USE: for general queries.
    """
    from pathlib import Path
    state = _ops_state or _fallback_state()
    s = state.settings
    data_dir = Path(s.qdrant_path).parent.resolve()
    return ok(state, {
        "data_dir":       str(data_dir),
        "qdrant_path":    str(Path(s.qdrant_path).resolve()),
        "state_db":       str(Path(s.state_db).resolve()),
        "logs_dir":       str(data_dir / "logs"),
        "backups_dir":    str(data_dir / "backups"),
        "service_pid":    str(data_dir / "service.pid"),
        "supervisor_pid": str(data_dir / "supervisor.pid"),
        "tray_pid":       str(data_dir / "tray.pid"),
    })


def system_health() -> dict:
    """One-call structured health check — JSON form of ``rag doctor``.

    USE WHEN: the user opens with "is my RAG setup working?" or after a
              crash to confirm what recovered.
    DO NOT USE: for general queries.
    """
    if _ops_state is not None and _ops_state.mode == "proxy":
        return proxy_get(_ops_state, "/api/system-health")
    return err(_ops_state or _fallback_state(),
               "system_health requires the service to be running.",
               code=_errcodes.SERVICE_DOWN,
               hint="Start the service with: rag service start")


def add_project(
    project_id: str,
    path: str,
    name: str | None = None,
    enabled: bool = True,
) -> dict:
    """Onboard a new project folder. Persists to config and auto-indexes.

    USE WHEN: the user explicitly asks to add / onboard / register a
              specific folder as a project (e.g. "add C:/Work/docs as
              project `docs`"). The ``path`` MUST be an absolute local
              filesystem path the user provided — never guess it.
    DO NOT USE: to rename / edit an existing project (no such tool by
                design — edit via the admin panel), or to delete one.

    Behaviour:
      * Validates the path exists and is a directory (server-side).
      * Rejects duplicate ``project_id`` and duplicate absolute paths.
      * Writes the new entry to the TOML config.
      * Triggers an auto-index 3 s after the response returns, so the
        folder becomes searchable without a separate ``run_index`` call.

    Args:
        project_id: Short lowercase identifier, used in storage keys and as
                    the ``project`` argument elsewhere (e.g. in
                    ``search_knowledge_base``). Must be unique.
        path:       Absolute filesystem path to the project's root folder.
                    Must exist and be a directory. Provided by the user.
        name:       Display name (defaults to ``project_id``).
        enabled:    Whether the project is immediately active (default True).
    """
    if _ops_state is None or _ops_state.mode != "proxy":
        return err(
            _ops_state or _fallback_state(),
            "add_project requires the service to be running — config writes "
            "cannot be persisted in direct mode.",
            code=_errcodes.SERVICE_DOWN,
            hint="Start the service with: rag service start",
        )
    if not project_id or not project_id.strip():
        return err(
            _ops_state,
            "project_id cannot be empty.",
            code=_errcodes.INVALID_ARG,
        )
    if not path or not path.strip():
        return err(
            _ops_state,
            "path cannot be empty. Pass the absolute folder path the user "
            "specified.",
            code=_errcodes.INVALID_ARG,
        )
    gate = _cooldown_guard("add_project")
    if gate is not None:
        return gate
    result = proxy_post(
        _ops_state,
        "/api/projects",
        json={
            "id": project_id.strip(),
            "name": (name or project_id).strip(),
            "path": path.strip(),
            "enabled": bool(enabled),
            "ignore_patterns": [],
        },
    )
    if result.get("ok"):
        _write_cooldown.mark("add_project")
    return result


def run_index(project: str) -> dict:
    """Run an incremental index for one project. Idempotent.

    USE WHEN: the user just added/edited a file in the project and wants
              it searchable now. Safe to call repeatedly — unchanged files
              are skipped.
    DO NOT USE: for general queries. This is a maintenance action.

    Args:
        project: Project ID.
    """
    gate = _cooldown_guard("run_index")
    if gate is not None:
        return gate
    result = proxy_post(_ops_state, "/api/index",
                       json={"project": project, "full": False})
    if result.get("ok"):
        _write_cooldown.mark("run_index")
    return result


def reindex_project(project: str, confirm_token: str) -> dict:
    """Drop a project's indexed data and re-index it from scratch.

    USE WHEN: the project's index is known-corrupt, after a major content
              change, or after tightening ignore rules.
    DO NOT USE: as a first-line fix for search quality — try ``run_index``
                first. This is destructive per-project; auto-backed-up.

    Args:
        project: Project ID.
        confirm_token: Must equal the project ID exactly. Defeats blind
                       invocation by a prompt-injected instruction that
                       doesn't know the specific project name the user
                       is working on.
    """
    if confirm_token != project:
        return err(
            _ops_state or _fallback_state(),
            "reindex_project requires confirm_token to equal the project ID.",
            code=_errcodes.CONFIRM_TOKEN_MISMATCH,
            hint=f"Pass confirm_token={project!r} to proceed.",
        )
    gate = _cooldown_guard("reindex_project")
    if gate is not None:
        return gate
    result = proxy_post(_ops_state, f"/api/projects/{project}/reindex")
    if result.get("ok"):
        _write_cooldown.mark("reindex_project")
    return result


def add_project_ignore_rule(project: str, pattern: str) -> dict:
    """Add an ignore pattern to a specific project's config.

    USE WHEN: the user wants certain files excluded from indexing.
              Ideally call ``preview_ignore_effect`` FIRST to verify what
              the pattern would affect. After adding, call ``run_index``
              or ``reindex_project`` to make the change effective.
    DO NOT USE: as a fix for search relevance — it removes content from
                the index rather than reranking.

    Args:
        project: Project ID.
        pattern: Gitignore-style pattern.
    """
    gate = _cooldown_guard("add_project_ignore_rule")
    if gate is not None:
        return gate
    result = proxy_post(_ops_state, f"/api/projects/{project}/ignore",
                       json={"pattern": pattern})
    if result.get("ok"):
        _write_cooldown.mark("add_project_ignore_rule")
    return result


def remove_project_ignore_rule(project: str, pattern: str) -> dict:
    """Remove an ignore pattern from a specific project's config.

    USE WHEN: reverting a previously added ignore rule. After removing,
              call ``reindex_project`` so previously excluded files get
              picked up.
    DO NOT USE: as a content-management tool. This only affects future
                index behaviour.

    Args:
        project: Project ID.
        pattern: Pattern to remove (exact match).
    """
    gate = _cooldown_guard("remove_project_ignore_rule")
    if gate is not None:
        return gate
    result = proxy_delete(_ops_state, f"/api/projects/{project}/ignore",
                         params={"pattern": pattern})
    if result.get("ok"):
        _write_cooldown.mark("remove_project_ignore_rule")
    return result


def project_status(project: str) -> dict:
    """Single-project orientation: enabled, path, file/chunk counts, last indexed.

    USE WHEN: the user just said they're working on project X and you want a
              one-call overview before searching or acting on it.
    DO NOT USE: for general knowledge queries — search the project directly.
    """
    if _ops_state is None or _ops_state.mode != "proxy":
        return err(_ops_state or _fallback_state(),
                   "project_status requires the service to be running.",
                   code=_errcodes.SERVICE_DOWN,
                   hint="Start the service with: rag service start")
    return proxy_get(_ops_state, f"/api/projects/{project}/status")


def project_summary(project: str, top_files: int = 10) -> dict:
    """Content-focused snapshot for a project — top files by chunk count.

    USE WHEN: the user asks "what's in project X?" — answers via file sizes
              and top contributors, not a search hit list.
    DO NOT USE: for searching content — use ``search_knowledge_base``.

    Args:
        project:   Project ID.
        top_files: Number of top-chunk-count files to return (1-50).
    """
    if _ops_state is None or _ops_state.mode != "proxy":
        return err(_ops_state or _fallback_state(),
                   "project_summary requires the service to be running.",
                   code=_errcodes.SERVICE_DOWN,
                   hint="Start the service with: rag service start")
    return proxy_get(
        _ops_state,
        f"/api/projects/{project}/summary",
        params={"top_files": top_files},
    )


def list_project_files(project: str, limit: int = 200) -> dict:
    """List files recorded in the state DB for one project.

    USE WHEN: the user says "I added file X, did it get indexed?" — lets you
              check for that specific path.
    DO NOT USE: for general search — content search is ``search_knowledge_base``.
    """
    if _ops_state is None or _ops_state.mode != "proxy":
        return err(_ops_state or _fallback_state(),
                   "list_project_files requires the service to be running.",
                   code=_errcodes.SERVICE_DOWN,
                   hint="Start the service with: rag service start")
    return proxy_get(
        _ops_state,
        f"/api/projects/{project}/files",
        params={"limit": limit},
    )


def get_project_ignore_rules(project: str) -> dict:
    """Effective ignore rules for a project — built-in, config, project-specific.

    USE WHEN: the user asks "why isn't file X being indexed in project Y?"
              Ignore rules are the most common cause.
    DO NOT USE: for the global ignore view — ``get_ignore_rules`` covers that.
    """
    if _ops_state is None or _ops_state.mode != "proxy":
        return err(_ops_state or _fallback_state(),
                   "get_project_ignore_rules requires the service to be running.",
                   code=_errcodes.SERVICE_DOWN,
                   hint="Start the service with: rag service start")
    return proxy_get(_ops_state, f"/api/projects/{project}/ignore")


def preview_ignore_effect(project: str, pattern: str) -> dict:
    """Dry-run: which files in the project WOULD be excluded by this pattern?

    USE WHEN: you're about to suggest adding an ignore pattern and want to
              verify its effect first. Returns the list of currently-indexed
              files the pattern would exclude. DOES NOT modify anything.
    DO NOT USE: to actually add the pattern — that requires a write tool.

    Args:
        project: Project ID.
        pattern: Gitignore-style pattern to test.
    """
    # preview_ignore_effect doesn't take a confirm token, so it's just a
    # proxy POST — reuse the shared helper.
    return proxy_post(
        _ops_state,
        f"/api/projects/{project}/ignore/preview",
        json={"pattern": pattern},
    )


def list_indexed_paths(project: str | None = None, limit: int = 200) -> dict:
    """List files currently recorded in the state DB.

    USE WHEN: the user reports "I added a file but it's not showing up".
    DO NOT USE: for general queries.

    Args:
        project: Optional project-ID filter.
        limit:   Max rows to return (1-1000).
    """
    import sqlite3
    from pathlib import Path
    state = _ops_state or _fallback_state()
    try:
        db_path = Path(state.settings.state_db)
        if not db_path.is_file():
            return err(state, "State DB does not exist yet.",
                       code=_errcodes.BACKEND_ERROR,
                       hint="Run: rag index .")
        limit = max(1, min(int(limit), 1000))
        conn = sqlite3.connect(db_path)
        try:
            if project:
                cur = conn.execute(
                    "SELECT file_path, project_id FROM file_state WHERE project_id = ? LIMIT ?",
                    (project, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT file_path, project_id FROM file_state LIMIT ?", (limit,),
                )
            rows = [{"path": r[0], "project": r[1]} for r in cur.fetchall()]
            return ok(state, {"count": len(rows), "files": rows})
        finally:
            conn.close()
    except Exception as e:
        return err(state, f"Could not read state DB: {e}",
                   code=_errcodes.BACKEND_ERROR)


def _fallback_state() -> McpState:
    """Build an ephemeral McpState when _ops_state wasn't initialised.

    This only happens in odd test paths or misuse — the real path always
    calls ``_initialize()`` which sets ``_ops_state``.
    """
    s = McpState(_settings or Settings())
    s.mode = "degraded"
    return s


# ---------------------------------------------------------------------------
# Proxy-mode search impls (unchanged from pre-consolidation)
# ---------------------------------------------------------------------------


def _proxy_search(
    query: str,
    project: str | None,
    projects: list[str] | None,
    top_k: int,
    structured: bool = False,
) -> str | dict:
    try:
        params = {"query": query, "top_k": top_k, "compact": True}
        if projects:
            params["projects"] = ",".join(p for p in projects if p)
        elif project:
            params["project"] = project
        if structured:
            params["structured"] = "true"
        r = _http_client.get("/api/search", params=params)
        if r.status_code == 200:
            body = r.json()
            if structured:
                # The backend already shaped this into {context, results, meta}.
                return body
            return body["formatted"]
        if structured:
            return {
                "context": f"[RAG ERROR] Service returned {r.status_code}",
                "results": [],
                "meta": {"query": query, "count": 0,
                          "error_code": _errcodes.PROXY_HTTP_5XX if r.status_code >= 500 else _errcodes.PROXY_HTTP_4XX},
            }
        return f"[RAG ERROR] Service returned {r.status_code}: {r.text}"
    except Exception as e:
        if structured:
            return {
                "context": f"[RAG ERROR] {e}",
                "results": [],
                "meta": {"query": query, "count": 0,
                          "error_code": _errcodes.PROXY_CONNECT_FAILED},
            }
        return _proxy_error(e)


def _proxy_list_projects() -> str:
    try:
        r = _http_client.get("/api/projects")
        if r.status_code == 200:
            data = r.json()["projects"]
            if not data:
                return "No projects found in the knowledge base."
            lines = [f"Indexed projects ({len(data)}):"]
            for p in data:
                lines.append(f"  - {p['project_id']} ({p['files']} files, {p['chunks']} chunks)")
            return "\n".join(lines)
        return f"[RAG ERROR] Service returned {r.status_code}: {r.text}"
    except Exception as e:
        return _proxy_error(e)


def _proxy_index_status() -> str:
    try:
        r = _http_client.get("/api/status")
        if r.status_code == 200:
            data = r.json()
            projects = data.get("projects", []) or []
            return (
                f"[RAG STATUS] Knowledge base is ready (proxy mode).\n"
                f"  Collection: {data.get('collection_name', 'unknown')}\n"
                f"  Total files: {data.get('total_files', 0)}\n"
                f"  Total chunks: {data.get('total_chunks', 0)}\n"
                f"  Points: {data.get('points_count', 0)}\n"
                f"  Projects: {', '.join(projects) if projects else '-'}\n"
                f"  Embedding model: {_settings.embedding_model}\n"
                f"  Score threshold: {_settings.score_threshold}\n"
                f"  Mode: proxy (forwarding to service)"
            )
        return f"[RAG ERROR] Service returned {r.status_code}: {r.text}"
    except Exception as e:
        return _proxy_error(e)


def _proxy_error(e: Exception) -> str:
    import httpx
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
        return (
            "[RAG ERROR] Service unavailable. "
            "The RAG service may have stopped. "
            "Restart with: rag service start"
        )
    return f"[RAG ERROR] Proxy request failed: {e}"


# ---------------------------------------------------------------------------
# Direct-mode search impls (unchanged from pre-consolidation)
# ---------------------------------------------------------------------------


def _get_direct_client():
    return _settings.get_qdrant_client()


def _direct_search(
    query: str,
    project: str | None,
    projects: list[str] | None,
    top_k: int,
    structured: bool = False,
) -> str | dict:
    error = _check_ready()
    if error:
        if structured:
            return {
                "context": f"[RAG ERROR] {error}",
                "results": [],
                "meta": {"query": query, "count": 0,
                          "error_code": _errcodes.SERVICE_DOWN},
            }
        return f"[RAG ERROR] {error}"

    client = None
    try:
        from ragtools.retrieval.formatter import format_context_compact
        from ragtools.retrieval.searcher import Searcher

        client = _get_direct_client()
        searcher = Searcher(client=client, encoder=_encoder, settings=_settings)

        results = searcher.search(
            query=query,
            project_id=project,
            project_ids=projects,
            top_k=top_k,
            score_threshold=_settings.score_threshold,
        )
        context = format_context_compact(results, query)
        if structured:
            return {
                "context": context,
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
                "meta": {
                    "query": query,
                    "count": len(results),
                    "project": project,
                    "projects": projects,
                    "top_k": top_k,
                    "mode": "direct",
                },
            }
        return context
    except Exception as e:
        logger.exception("Search failed")
        if structured:
            return {
                "context": f"[RAG ERROR] Search failed: {e}",
                "results": [],
                "meta": {"query": query, "count": 0,
                          "error_code": _errcodes.BACKEND_ERROR},
            }
        return f"[RAG ERROR] Search failed: {e}"
    finally:
        if client:
            del client


def _direct_list_projects() -> str:
    error = _check_ready()
    if error:
        return f"[RAG ERROR] {error}"

    client = None
    try:
        client = _get_direct_client()
        collection_name = _settings.collection_name

        # Count chunks per project so the output matches proxy-mode shape:
        # "Indexed projects (N):\n  - pid (F files, C chunks)".
        project_counts: dict[str, int] = {}
        project_files: dict[str, set[str]] = {}
        offset = None
        while True:
            results, offset = client.scroll(
                collection_name=collection_name,
                limit=256,
                offset=offset,
                with_payload=["project_id", "file_path"],
                with_vectors=False,
            )
            for point in results:
                pid = point.payload.get("project_id")
                if not pid:
                    continue
                project_counts[pid] = project_counts.get(pid, 0) + 1
                fp = point.payload.get("file_path")
                if fp:
                    project_files.setdefault(pid, set()).add(fp)
            if offset is None:
                break

        if not project_counts:
            return "No projects found in the knowledge base."

        lines = [f"Indexed projects ({len(project_counts)}):"]
        for p in sorted(project_counts):
            files = len(project_files.get(p, set()))
            chunks = project_counts[p]
            lines.append(f"  - {p} ({files} files, {chunks} chunks)")
        return "\n".join(lines)

    except Exception as e:
        logger.exception("list_projects failed")
        return f"[RAG ERROR] Failed to list projects: {e}"
    finally:
        if client:
            del client


def _direct_index_status() -> str:
    error = _check_ready()
    if error:
        return f"[RAG STATUS] {error}"

    client = None
    try:
        client = _get_direct_client()
        collection_name = _settings.collection_name
        info = client.get_collection(collection_name)
        count = info.points_count

        if count == 0:
            return (
                "[RAG STATUS] Collection exists but is empty. "
                "Run `rag index <path>` to populate it."
            )

        # Augment direct-mode output to match proxy-mode key set so agents
        # can parse consistently across modes. Pull file/project counts from
        # the state DB when available — fall back to dashes if the state DB
        # doesn't exist yet (index was created out-of-band).
        total_files = "-"
        projects_str = "-"
        try:
            from pathlib import Path as _P
            from ragtools.indexing.state import IndexState
            if _P(_settings.state_db).exists():
                state = IndexState(_settings.state_db)
                try:
                    summary = state.get_summary()
                    total_files = str(summary.get("total_files", "-"))
                    projects = summary.get("projects", []) or []
                    projects_str = ", ".join(projects) if projects else "-"
                finally:
                    state.close()
        except Exception:
            # State DB read is best-effort — don't fail the status call.
            pass

        return (
            f"[RAG STATUS] Knowledge base is ready (direct mode).\n"
            f"  Collection: {collection_name}\n"
            f"  Total files: {total_files}\n"
            f"  Total chunks: {count}\n"
            f"  Points: {count}\n"
            f"  Projects: {projects_str}\n"
            f"  Embedding model: {_settings.embedding_model}\n"
            f"  Score threshold: {_settings.score_threshold}\n"
            f"  Mode: direct (per-request Qdrant access — lock released between queries)"
        )

    except Exception as e:
        logger.exception("index_status failed")
        return f"[RAG ERROR] Failed to get index status: {e}"
    finally:
        if client:
            del client


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _safe_initialize() -> None:
    """Wrap ``_initialize`` so an unexpected exception doesn't crash the
    MCP process. Instead we set ``_mode = "failed"`` + ``_init_error`` and
    let the tool entry points return a structured error envelope.

    This turns "MCP disconnected with no detail" into "agent receives a
    `STARTUP_FAILED` error it can show the user."
    """
    global _mode, _init_error, _ops_state
    try:
        _initialize()
    except BaseException as exc:
        import traceback
        _mode = "failed"
        _init_error = f"MCP initialization crashed: {type(exc).__name__}: {exc}"
        logger.error(
            "MCP _initialize() crashed — staying alive in FAILED mode.\n%s",
            traceback.format_exc(),
        )
        # Build a minimal ops state so tools can construct error envelopes.
        if _ops_state is None:
            try:
                _ops_state = McpState(_settings or Settings())
            except Exception:
                # Even Settings() failed. Build a best-effort state.
                _ops_state = McpState.__new__(McpState)
                _ops_state.mode = "failed"
                _ops_state.http = None
                _ops_state.session_id = "unknwn"
                _ops_state.init_error = _init_error
        _ops_state.mode = "failed"


def main():
    """Entry point for the MCP server."""
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _safe_initialize()
    mcp_app.run(transport="stdio")


if __name__ == "__main__":
    main()
