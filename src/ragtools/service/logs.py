"""Safe log-file tailing for the admin panel and MCP ops tools.

Exposes ``tail(source, limit)`` which reads the last ``limit`` lines from
a whitelisted log file in the service's ``logs/`` directory. A whitelist
is used (not an arbitrary-path reader) so a prompt-injected MCP tool
call can't convert this into ``cat /etc/passwd``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

from ragtools.config import Settings

logger = logging.getLogger("ragtools.service")


# Whitelist maps a short source name to the on-disk filename. The MCP tool
# accepts only these keys — the actual paths never leak in or out.
_LOG_FILES: Dict[str, str] = {
    "service": "service.log",
    "watcher": "service.log",        # watcher logs into the same file under its logger name
    "launcher": "launcher.log",
    "watchdog": "watchdog.log",      # DELL's external watchdog — may not exist yet
    "supervisor": "service.log",     # supervisor also multiplexes here
    "tray": "tray-dev.log",          # only present in dev runs
}


def available_sources() -> list[str]:
    """Public list of valid ``source`` values."""
    return sorted(_LOG_FILES.keys())


def _logs_dir(settings: Settings) -> Path:
    return Path(settings.qdrant_path).parent / "logs"


def tail(settings: Settings, source: str, limit: int = 50) -> dict:
    """Return the last ``limit`` lines of the named log.

    Returns a dict with either:
        {"source": s, "lines": [...], "truncated": bool, "path": str}
      or
        {"error": "...", "available_sources": [...]}

    Never raises.
    """
    if source not in _LOG_FILES:
        return {
            "error": f"Unknown log source: {source!r}",
            "available_sources": available_sources(),
        }
    limit = max(1, min(int(limit), 500))  # cap to prevent token blowout

    path = _logs_dir(settings) / _LOG_FILES[source]
    if not path.is_file():
        return {
            "source": source,
            "path": str(path),
            "lines": [],
            "truncated": False,
            "note": "log file does not exist yet",
        }

    try:
        # Simple tail — reads whole file for small logs (<10 MB). For larger
        # logs we'd want a seek-from-end strategy, but our rotating handler
        # caps files at 10 MB so this is acceptable.
        text = path.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        truncated = len(all_lines) > limit
        tail_lines = all_lines[-limit:]
        return {
            "source": source,
            "path": str(path),
            "lines": tail_lines,
            "truncated": truncated,
            "total_lines_in_file": len(all_lines),
        }
    except OSError as e:
        logger.warning("log tail failed: %s", e)
        return {"error": f"Could not read {path}: {e}"}
