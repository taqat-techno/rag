"""Crash-history support for the admin panel.

Reads the three marker files the service may have written:

- ``last_crash.json`` — written by ``_record_fatal_crash`` in run.py when
  the service process hit an unhandled exception.
- ``supervisor_gave_up.json`` — written by the supervisor when the
  restart budget was exhausted.
- ``watcher_gave_up.json`` — written by ``WatcherThread`` when the file
  watcher exhausted its own retry budget. Separate marker because the
  watcher fails without taking the service with it — the user's search
  still works but new ``.md`` edits are no longer auto-indexed.

The admin panel fetches /api/crash-history on every page load. If anything
is returned, a dismissable banner is shown. Dismissing a marker renames
it with a ``.reviewed`` suffix so the file is preserved for post-mortem
but the banner no longer appears.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ragtools.service")

# Markers older than this are considered resolved even if never dismissed.
# 30 days is long enough that a user who has been on vacation will still
# see their recent crash, but old forgotten files don't haunt new installs.
_MAX_MARKER_AGE_SECONDS = 30 * 24 * 60 * 60


def _logs_dir_for(settings) -> Path:
    """Resolve the logs directory from Settings."""
    return Path(settings.qdrant_path).parent / "logs"


def _load_marker(path: Path) -> Optional[dict]:
    """Read a marker file if it exists and isn't expired.

    Returns None for: missing file, expired file, JSON parse error,
    or a file already marked as reviewed (``.reviewed`` suffix).
    """
    if not path.exists():
        return None
    try:
        age = time.time() - path.stat().st_mtime
        if age > _MAX_MARKER_AGE_SECONDS:
            return None
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data["_source_file"] = path.name
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read crash marker %s: %s", path, e)
        return None


def _marker_paths(settings) -> dict[str, Path]:
    """The set of marker files we surface in the banner."""
    logs = _logs_dir_for(settings)
    return {
        "service_crash": logs / "last_crash.json",
        "supervisor_gave_up": logs / "supervisor_gave_up.json",
        "watcher_gave_up": logs / "watcher_gave_up.json",
    }


def list_unreviewed_crashes(settings) -> list[dict]:
    """Return the list of unreviewed crash markers, newest first.

    Each entry contains the raw marker JSON plus a ``kind`` field
    (``"service_crash"`` or ``"supervisor_gave_up"``) the UI uses to
    render the right icon/label and a ``dismiss_key`` the UI sends back
    to POST /api/crash-history/dismiss.
    """
    results: list[dict] = []
    for kind, path in _marker_paths(settings).items():
        data = _load_marker(path)
        if data is None:
            continue
        data["kind"] = kind
        data["dismiss_key"] = kind
        # Fall back to mtime-derived timestamp if the marker payload
        # doesn't carry one (older files written before versioning).
        if "timestamp" not in data:
            try:
                data["timestamp"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(path.stat().st_mtime),
                )
            except OSError:
                pass
        results.append(data)

    # Newest first, lexicographic on ISO timestamps works.
    results.sort(key=lambda d: d.get("timestamp", ""), reverse=True)
    return results


def dismiss_crash_marker(settings, dismiss_key: str) -> bool:
    """Mark a crash marker as reviewed by renaming the file.

    Renames ``<marker>.json`` to ``<marker>.reviewed.json`` in-place so
    the file is still available for post-mortem but no longer appears
    in the banner.

    Returns True on success, False if no matching marker was found.
    """
    paths = _marker_paths(settings)
    if dismiss_key not in paths:
        return False
    src = paths[dismiss_key]
    if not src.exists():
        return False
    try:
        reviewed = src.with_name(src.stem + ".reviewed" + src.suffix)
        # If a previous reviewed file exists, overwrite — we keep only
        # the most recent one per kind to avoid log-dir bloat.
        if reviewed.exists():
            reviewed.unlink()
        src.rename(reviewed)
        logger.info("Crash marker dismissed: %s -> %s", src.name, reviewed.name)
        return True
    except OSError as e:
        logger.error("Failed to dismiss crash marker %s: %s", src, e)
        return False
