"""Machine-readable error codes for MCP tool envelopes.

Every ``err()`` call in the MCP layer should pass one of these as the
``code`` kwarg. Agents branch on ``r["error_code"]`` instead of
string-matching ``r["error"]``. The ``error`` field stays around as a
human-readable explanation — the code is for logic, the message is for
users.
"""

from __future__ import annotations

# --- Service / mode availability ---
SERVICE_DOWN = "SERVICE_DOWN"
"""The local RAG service is not responding. Direct/degraded fallback."""

DEGRADED_MODE = "DEGRADED_MODE"
"""The tool needs proxy mode but the MCP is running in degraded mode."""

STARTUP_FAILED = "STARTUP_FAILED"
"""MCP initialization crashed before tools were usable. The MCP stays
alive so it can report this, but no tool will succeed until restart."""

# --- Request-side errors (client's fault) ---
INVALID_ARG = "INVALID_ARG"
"""Agent passed an argument the tool can't accept (e.g. empty query,
unknown log source, negative limit)."""

CONFIRM_TOKEN_MISMATCH = "CONFIRM_TOKEN_MISMATCH"
"""``reindex_project`` was called without ``confirm_token == project``."""

COOLDOWN = "COOLDOWN"
"""Write tool called again inside its cooldown window. Response includes
``retry_after_seconds`` in the data so the agent can decide whether to wait."""

# --- Backend / transport errors ---
PROXY_CONNECT_FAILED = "PROXY_CONNECT_FAILED"
"""HTTP connection to the service failed (service died mid-session?)."""

PROXY_HTTP_4XX = "PROXY_HTTP_4XX"
"""Service returned a 4xx. Usually means the request was malformed."""

PROXY_HTTP_5XX = "PROXY_HTTP_5XX"
"""Service returned a 5xx. The backend hit an error."""

BACKEND_ERROR = "BACKEND_ERROR"
"""Filesystem / state-DB / Qdrant error — not a transport issue."""

# --- Catch-all ---
UNKNOWN = "UNKNOWN"
"""An ``err()`` call was made without a specific code. Logged as a
warning so we can catch and fix the missing label."""


__all__ = [
    "SERVICE_DOWN", "DEGRADED_MODE", "STARTUP_FAILED",
    "INVALID_ARG", "CONFIRM_TOKEN_MISMATCH", "COOLDOWN",
    "PROXY_CONNECT_FAILED", "PROXY_HTTP_4XX", "PROXY_HTTP_5XX", "BACKEND_ERROR",
    "UNKNOWN",
]
