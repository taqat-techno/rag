"""In-memory activity log for the admin UI.

Provides a thread-safe ring buffer of recent operational events.
Events are ephemeral (lost on service restart) and complement
the persistent file logging — they do not replace it.
"""

import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass
class ActivityEvent:
    """A single operational event."""

    id: int
    timestamp: str        # ISO 8601
    level: str            # "info" | "success" | "warning" | "error"
    source: str           # "service" | "indexer" | "watcher" | "config" | "startup" | "mcp"
    message: str
    details: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class ActivityLog:
    """Thread-safe ring buffer of recent activity events."""

    def __init__(self, maxlen: int = 500):
        self._events: deque[ActivityEvent] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._counter = 0

    def emit(self, level: str, source: str, message: str, details: str | None = None) -> None:
        """Add an event to the buffer and log to Python logger."""
        with self._lock:
            self._counter += 1
            event = ActivityEvent(
                id=self._counter,
                timestamp=datetime.now().isoformat(timespec="seconds"),
                level=level,
                source=source,
                message=message,
                details=details,
            )
            self._events.append(event)

        # Also log to Python logger (file logging continues unchanged)
        logger = logging.getLogger(f"ragtools.activity.{source}")
        log_level = {"info": 20, "success": 20, "warning": 30, "error": 40}.get(level, 20)
        logger.log(log_level, "%s", message)

    def get_recent(self, limit: int = 50, after_id: int = 0) -> list[ActivityEvent]:
        """Get recent events, optionally after a given ID for incremental polling."""
        with self._lock:
            events = [e for e in self._events if e.id > after_id]
        return events[-limit:]

    def count(self) -> int:
        with self._lock:
            return len(self._events)

    def latest_id(self) -> int:
        with self._lock:
            return self._counter


# Global singleton
activity_log = ActivityLog()


def log_activity(level: str, source: str, message: str, details: str | None = None) -> None:
    """Convenience function to emit an activity event from anywhere."""
    activity_log.emit(level, source, message, details)
