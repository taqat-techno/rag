"""Shared helpers for the ragtools MCP servers.

Why this exists
---------------
We run TWO MCP servers so agents see only the tools they need:

  - ``ragtools``       — the core search surface (always-on, low distraction)
  - ``ragtools-ops``   — operational diagnostics (opt-in, per-project)

Both need the same primitives: probe the local service, wrap responses in
a consistent envelope, and refuse cleanly when called in a mode that
can't satisfy the request. This module owns those primitives.

Response envelope
-----------------
Every ops-tool response is a JSON-serializable dict with:

    {
      "ok":   true | false,
      "mode": "proxy" | "direct" | "degraded",
      "as_of": "2026-04-18T12:34:56Z",
      "data": {...}          # on success
      "error": "string",     # on failure
      "hint":  "string",     # optional; suggests how to fix
    }

Rationale:
  - ``mode`` lets agents reason about what capability is currently live.
  - ``as_of`` lets agents reason about freshness across repeated calls.
  - ``ok`` is a cheap guard clause — ``if not r["ok"]: ...`` beats parsing
    prose for error signals.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ragtools.config import Settings

logger = logging.getLogger("ragtools.mcp")


# Session identification header. The MCP client stamps every proxied HTTP
# request with this so the server can attribute activity-log writes to a
# specific MCP session (useful when the user has two Claude Code windows
# talking to the same service).
MCP_SESSION_HEADER = "X-MCP-Session"


def _new_session_id() -> str:
    """Short random hex id (4 chars — ~65k possibilities, enough to tell
    concurrent sessions apart while staying readable in logs)."""
    return secrets.token_hex(2)


# ---------------------------------------------------------------------------
# Write-tool cooldown — rate-limit agent writes so a runaway loop can't
# hammer the backend. Each tool has its own cooldown window; they don't
# cross-throttle each other.
# ---------------------------------------------------------------------------


class WriteCooldown:
    """Per-tool cooldown tracker for agent write operations.

    Mirrors the ``DesktopNotifier`` cooldown pattern that's already proven
    itself in the notification path — one in-memory timestamp per tool name,
    checked at tool entry, updated only on a successful go-ahead.

    The concrete cooldowns are conservative defaults tuned for the typical
    agent loop — tight enough to stop runaway loops, loose enough not to
    block the legitimate "I just saved a file, please re-index" pattern.
    """

    DEFAULTS: dict[str, float] = {
        "run_index":                   2.0,
        "reindex_project":            30.0,
        "add_project_ignore_rule":     1.0,
        "remove_project_ignore_rule":  1.0,
    }

    def __init__(
        self,
        cooldowns: Optional[dict[str, float]] = None,
        clock=time.monotonic,
    ) -> None:
        self._cooldowns = dict(cooldowns or self.DEFAULTS)
        self._last: dict[str, float] = {}
        self._clock = clock

    def check(self, tool_name: str) -> Optional[float]:
        """Return remaining seconds if the tool is cooling down, else None."""
        window = self._cooldowns.get(tool_name, 0.0)
        if window <= 0:
            return None
        last = self._last.get(tool_name)
        if last is None:
            return None
        now = self._clock()
        remaining = window - (now - last)
        return remaining if remaining > 0 else None

    def mark(self, tool_name: str) -> None:
        """Record that the tool is running now. Should be called on the
        success/dispatch path, not before the check."""
        self._last[tool_name] = self._clock()


# ---------------------------------------------------------------------------
# Mode + client state — the ops server initializes these once and reuses them
# ---------------------------------------------------------------------------


class McpState:
    """Per-server state: settings, mode, and a proxy client if applicable.

    Instances are expected to be module-level singletons (one per MCP server).
    Tests can construct fresh instances.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings: Settings = settings or Settings()
        self.mode: str = "uninitialized"   # "proxy" | "direct" | "degraded" | "uninitialized" | "failed"
        self.http: Optional[httpx.Client] = None
        self.init_error: Optional[str] = None
        self.session_id: str = _new_session_id()

    def initialize(self, *, probe_retries: int = 2, retry_sleep: float = 2.0) -> None:
        """Probe the local service and lock in a mode.

        ``proxy`` — service is up; full ops surface is available.
        ``degraded`` — service is down; only filesystem-based ops tools work
                      (log tail, crash history, config, paths). Tools that
                      need live service state return a clear error.
        """
        url = f"http://{self.settings.service_host}:{self.settings.service_port}/health"
        for attempt in range(probe_retries):
            try:
                r = httpx.get(url, timeout=2.0)
                if r.status_code == 200:
                    self.mode = "proxy"
                    self.http = httpx.Client(
                        base_url=f"http://{self.settings.service_host}:{self.settings.service_port}",
                        timeout=httpx.Timeout(5.0, read=60.0),
                        headers={MCP_SESSION_HEADER: self.session_id},
                    )
                    logger.info("MCP ops: PROXY mode, session=%s, service at %s",
                                self.session_id, url)
                    return
            except Exception as e:
                logger.debug("Service probe %d failed: %s", attempt + 1, e)
            if attempt < probe_retries - 1:
                time.sleep(retry_sleep)

        self.mode = "degraded"
        self.init_error = (
            "RAG service not running. Filesystem-based ops tools still work, "
            "but tools that need live service state will return a clear error."
        )
        logger.info("MCP ops: DEGRADED mode, service unreachable")


# ---------------------------------------------------------------------------
# Response envelope helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ok(state: McpState, data: dict) -> dict:
    """Wrap a successful response in the envelope."""
    return {
        "ok": True,
        "mode": state.mode,
        "as_of": _now_iso(),
        "data": data,
    }


def err(
    state: McpState,
    message: str,
    *,
    code: str = "UNKNOWN",
    hint: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """Wrap an error response in the envelope.

    Args:
        state: the McpState (for mode).
        message: human-readable error.
        code: machine-readable code, one of the constants in
              ``mcp_errors``. Defaults to ``UNKNOWN`` — a warning is
              logged when that default is used, so call sites missing an
              explicit code are discoverable.
        hint: optional one-line remediation.
        extra: optional additional fields to merge into the envelope
               alongside ``error`` / ``error_code`` (e.g. ``retry_after_seconds``).
    """
    if code == "UNKNOWN":
        logger.warning("MCP err() called without explicit code: %s", message)
    out = {
        "ok": False,
        "mode": state.mode,
        "as_of": _now_iso(),
        "error": message,
        "error_code": code,
    }
    if hint:
        out["hint"] = hint
    if extra:
        out.update(extra)
    return out


def require_proxy(state: McpState, tool_name: str) -> Optional[dict]:
    """Return an error envelope if the given tool needs proxy mode but we
    aren't in it. Returns ``None`` if we are in proxy mode — caller then
    proceeds.

    Idiom at the top of any proxy-only tool::

        gate = require_proxy(_state, "recent_activity")
        if gate is not None:
            return gate
    """
    if state.mode == "proxy" and state.http is not None:
        return None
    from ragtools.integration.mcp_errors import DEGRADED_MODE, STARTUP_FAILED
    code = STARTUP_FAILED if state.mode == "failed" else DEGRADED_MODE
    return err(
        state,
        f"Tool '{tool_name}' requires the local RAG service to be running.",
        code=code,
        hint="Start the service with: rag service start",
    )


# ---------------------------------------------------------------------------
# Thin proxy GET/POST helpers — so tool bodies stay ≤ 10 lines
# ---------------------------------------------------------------------------


def _http_status_code(status: int) -> str:
    """Map an HTTP status to the right ``mcp_errors`` constant."""
    from ragtools.integration.mcp_errors import PROXY_HTTP_4XX, PROXY_HTTP_5XX, UNKNOWN
    if 400 <= status < 500:
        return PROXY_HTTP_4XX
    if 500 <= status < 600:
        return PROXY_HTTP_5XX
    return UNKNOWN


def proxy_get(state: McpState, path: str, **kwargs: Any) -> dict:
    """Proxy a GET and return ok/err envelope. Never raises."""
    from ragtools.integration.mcp_errors import PROXY_CONNECT_FAILED, BACKEND_ERROR
    gate = require_proxy(state, f"GET {path}")
    if gate is not None:
        return gate
    try:
        r = state.http.get(path, **kwargs)
        if r.status_code == 200:
            return ok(state, r.json())
        return err(state, f"Service returned HTTP {r.status_code}: {r.text[:200]}",
                   code=_http_status_code(r.status_code))
    except httpx.ConnectError:
        return err(
            state, "Could not connect to the RAG service.",
            code=PROXY_CONNECT_FAILED,
            hint="Start the service with: rag service start",
        )
    except Exception as e:
        return err(state, f"Proxy GET failed: {e}", code=BACKEND_ERROR)


def proxy_post(state: McpState, path: str, **kwargs: Any) -> dict:
    """Proxy a POST and return ok/err envelope. Never raises."""
    from ragtools.integration.mcp_errors import PROXY_CONNECT_FAILED, BACKEND_ERROR
    gate = require_proxy(state, f"POST {path}")
    if gate is not None:
        return gate
    try:
        r = state.http.post(path, **kwargs)
        if 200 <= r.status_code < 300:
            try:
                body = r.json()
            except Exception:
                body = {"status": "ok"}
            return ok(state, body)
        return err(state, f"Service returned HTTP {r.status_code}: {r.text[:200]}",
                   code=_http_status_code(r.status_code))
    except httpx.ConnectError:
        return err(
            state, "Could not connect to the RAG service.",
            code=PROXY_CONNECT_FAILED,
            hint="Start the service with: rag service start",
        )
    except Exception as e:
        return err(state, f"Proxy POST failed: {e}", code=BACKEND_ERROR)


def proxy_delete(state: McpState, path: str, **kwargs: Any) -> dict:
    """Proxy a DELETE and return ok/err envelope. Never raises."""
    from ragtools.integration.mcp_errors import PROXY_CONNECT_FAILED, BACKEND_ERROR
    gate = require_proxy(state, f"DELETE {path}")
    if gate is not None:
        return gate
    try:
        r = state.http.delete(path, **kwargs)
        if 200 <= r.status_code < 300:
            try:
                body = r.json()
            except Exception:
                body = {"status": "ok"}
            return ok(state, body)
        return err(state, f"Service returned HTTP {r.status_code}: {r.text[:200]}",
                   code=_http_status_code(r.status_code))
    except httpx.ConnectError:
        return err(
            state, "Could not connect to the RAG service.",
            code=PROXY_CONNECT_FAILED,
            hint="Start the service with: rag service start",
        )
    except Exception as e:
        return err(state, f"Proxy DELETE failed: {e}", code=BACKEND_ERROR)
