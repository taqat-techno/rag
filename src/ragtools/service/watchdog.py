"""Windows Task Scheduler watchdog for the RAG service.

Relationship to the supervisor
------------------------------
The supervisor (see supervisor.py) is a parent process that respawns the
service if it crashes. This watchdog is a *separate* safety net — it
catches the cases the supervisor cannot:

  - The supervisor itself was killed (OOM, SIGKILL, taskkill /F).
  - The whole process tree went down with the machine (reboot, BSOD).
  - The user disabled the login-startup task but still wants auto-recovery.

How it works
------------
A Task Scheduler task fires every N minutes (default 15) and invokes
``rag service watchdog check``. The check:

  1. Probes ``http://{host}:{port}/health``.
  2. If healthy → exit 0, log nothing.
  3. If dead   → call ``start_service(settings, supervise=True)`` and exit 0.

The task runs at LIMITED run-level as the current user, so it does NOT
require admin — this matched the constraint the existing login-task
integration already respects (see ``startup.py``).

Design
------
The decision layer (``decide_action``) and the argv-builder
(``_build_schtasks_install_args``) are pure functions, which lets the
tests cover the full surface without touching Task Scheduler.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional

from ragtools.config import Settings

logger = logging.getLogger("ragtools.watchdog")


TASK_NAME = "RAGTools Watchdog"
DEFAULT_INTERVAL_MINUTES = 15


class WatchdogAction(str, Enum):
    """What the watchdog should do after one health probe."""

    NOTHING = "nothing"           # service is healthy — leave it alone
    START = "start"               # service is dead — launch it
    ALREADY_STARTING = "already"  # something else is starting it; no-op this tick


@dataclass
class WatchdogResult:
    """Outcome of a single `run_check` call. Useful for status/telemetry."""

    action: WatchdogAction
    started_pid: Optional[int] = None
    note: str = ""


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------


def decide_action(is_alive: bool) -> WatchdogAction:
    """Single-responsibility decision: alive → NOTHING; dead → START.

    Kept trivially simple so future decisions (e.g. rate-limit restarts,
    suppress during installs) can be layered on top without reshaping
    the call sites.
    """
    return WatchdogAction.NOTHING if is_alive else WatchdogAction.START


# ---------------------------------------------------------------------------
# schtasks argv builder — pure, no subprocess
# ---------------------------------------------------------------------------


def _current_user() -> str:
    """Return the current user's domain\\user string, suitable for /ru."""
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    if domain and user:
        return f"{domain}\\{user}"
    return user or "SYSTEM"


def _check_cmd() -> List[str]:
    """Command the scheduled task will run to perform the health check.

    - Packaged (frozen exe): call the installed ``rag.exe`` directly.
    - Dev / source: call the venv's Python with ``-m ragtools.cli``.
    """
    from ragtools.config import is_packaged

    if is_packaged():
        exe_dir = os.path.dirname(sys.executable)
        rag_exe = os.path.join(exe_dir, "rag.exe")
        if os.path.exists(rag_exe):
            return [rag_exe, "service", "watchdog", "check"]
        # Fallback: call the bundled python with -m
        return [sys.executable, "-m", "ragtools.cli", "service", "watchdog", "check"]
    return [sys.executable, "-m", "ragtools.cli", "service", "watchdog", "check"]


def _build_schtasks_install_args(
    task_name: str = TASK_NAME,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    user: Optional[str] = None,
    command_parts: Optional[List[str]] = None,
) -> List[str]:
    """Assemble the schtasks /create command line.

    Args:
        task_name: display/lookup name in Task Scheduler.
        interval_minutes: minutes between runs. Task Scheduler requires 1+.
        user: /ru value. Defaults to the current domain\\user.
        command_parts: the argv the task will execute. Defaults to the
            output of `_check_cmd()` (computed here rather than at module
            load so test monkeypatches take effect).

    Returns:
        argv for ``subprocess.run``.
    """
    if interval_minutes < 1:
        raise ValueError(f"interval_minutes must be >= 1 (got {interval_minutes})")

    parts = command_parts if command_parts is not None else _check_cmd()

    # schtasks /tr accepts a single quoted string. Wrap each argv element in
    # double-quotes and join with spaces — handles paths with spaces safely.
    def _q(s: str) -> str:
        return f'"{s}"' if (" " in s or '"' in s) else s

    tr_value = " ".join(_q(p) for p in parts)

    return [
        "schtasks",
        "/create",
        "/tn", task_name,
        "/tr", tr_value,
        "/sc", "minute",
        "/mo", str(interval_minutes),
        "/rl", "limited",
        "/ru", user or _current_user(),
        "/f",  # overwrite if exists
    ]


def _build_schtasks_delete_args(task_name: str = TASK_NAME) -> List[str]:
    return ["schtasks", "/delete", "/tn", task_name, "/f"]


def _build_schtasks_query_args(task_name: str = TASK_NAME) -> List[str]:
    return ["schtasks", "/query", "/tn", task_name, "/fo", "LIST", "/v"]


# ---------------------------------------------------------------------------
# Platform guard
# ---------------------------------------------------------------------------


def _windows_only() -> bool:
    if sys.platform != "win32":
        logger.info("Watchdog: skipped (platform=%s is not Windows)", sys.platform)
        return False
    return True


# ---------------------------------------------------------------------------
# Install / uninstall / status — thin subprocess wrappers
# ---------------------------------------------------------------------------


def install_watchdog_task(
    settings: Settings,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    runner: Callable[[List[str]], subprocess.CompletedProcess] = None,
) -> bool:
    """Register the watchdog task. Idempotent — ``/f`` overwrites.

    Args:
        settings: service settings (for future use — e.g. port-aware args).
        interval_minutes: task repetition interval.
        runner: for tests — callable that replaces ``subprocess.run``.

    Returns:
        True on successful registration, False on platform guard or failure.
    """
    if not _windows_only():
        return False

    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    args = _build_schtasks_install_args(interval_minutes=interval_minutes)
    logger.info("Installing watchdog task: %s (every %d min)", TASK_NAME, interval_minutes)
    result = runner(args)
    if result.returncode != 0:
        logger.warning(
            "Watchdog install failed (rc=%d): %s",
            result.returncode, (result.stderr or result.stdout or "").strip(),
        )
        return False
    logger.info("Watchdog task installed")
    return True


def uninstall_watchdog_task(
    runner: Callable[[List[str]], subprocess.CompletedProcess] = None,
) -> bool:
    """Remove the watchdog task. Returns True if removed OR already absent."""
    if not _windows_only():
        return True

    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    args = _build_schtasks_delete_args()
    result = runner(args)
    if result.returncode == 0:
        logger.info("Watchdog task removed")
        return True
    # schtasks returns 1 when the task doesn't exist; treat as success.
    msg = (result.stderr or result.stdout or "").lower()
    if "cannot find" in msg or "does not exist" in msg or "0x80070002" in msg:
        logger.info("Watchdog task was not installed")
        return True
    logger.warning("Watchdog uninstall failed (rc=%d): %s", result.returncode, msg)
    return False


def is_watchdog_installed(
    runner: Callable[[List[str]], subprocess.CompletedProcess] = None,
) -> bool:
    """Return True if the watchdog task is currently registered."""
    if sys.platform != "win32":
        return False
    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    args = _build_schtasks_query_args()
    result = runner(args)
    return result.returncode == 0


def get_watchdog_info(
    runner: Callable[[List[str]], subprocess.CompletedProcess] = None,
) -> Optional[dict]:
    """Return a summary dict describing the installed task, or None."""
    if sys.platform != "win32":
        return None
    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    args = _build_schtasks_query_args()
    result = runner(args)
    if result.returncode != 0:
        return None

    fields = _parse_schtasks_list_output(result.stdout)
    return {
        "task_name": TASK_NAME,
        "status": fields.get("Status", "unknown"),
        "next_run": fields.get("Next Run Time", "unknown"),
        "last_run": fields.get("Last Run Time", "unknown"),
        "last_result": fields.get("Last Result", ""),
        "task_to_run": fields.get("Task To Run", ""),
    }


def _parse_schtasks_list_output(text: str) -> dict:
    """Parse the LIST-format output of `schtasks /query /v /fo LIST`.

    The format is "Key: Value" per line, with blank lines between tasks.
    We only register one task with this name, so the first record wins.
    """
    out: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key and key not in out:
            out[key] = value.strip()
    return out


# ---------------------------------------------------------------------------
# Run-check — what the task actually executes
# ---------------------------------------------------------------------------


def run_check(
    settings: Settings,
    probe: Callable[[Settings], bool] = None,
    starter: Callable[[Settings], int] = None,
) -> WatchdogResult:
    """Health-probe the service; relaunch if dead. Always succeeds.

    Returns a WatchdogResult so callers (CLI, tests, telemetry) can see
    what happened without parsing logs. Exceptions from ``starter`` are
    caught and recorded — the task must never exit non-zero, or Task
    Scheduler will spam "task failed" notifications.

    Args:
        settings: service settings.
        probe: for tests — callable returning health bool. Defaults to
            the CLI's ``_probe_service`` (imported lazily to avoid cycles).
        starter: for tests — callable that starts the service and
            returns its PID. Defaults to ``start_service``.
    """
    if probe is None:
        from ragtools.cli import _probe_service as _default_probe
        probe = _default_probe
    if starter is None:
        from ragtools.service.process import start_service as _default_starter
        starter = lambda s: _default_starter(s, supervise=True)

    alive = probe(settings)
    action = decide_action(alive)

    if action == WatchdogAction.NOTHING:
        return WatchdogResult(action=WatchdogAction.NOTHING)

    # Service looks dead — try to start it.
    try:
        pid = starter(settings)
        logger.warning("Watchdog relaunched the service (PID %s)", pid)
        try:
            from ragtools.service.activity import log_activity
            log_activity("warning", "service", f"Watchdog relaunched service (PID {pid})")
        except Exception:
            pass
        return WatchdogResult(action=WatchdogAction.START, started_pid=pid)
    except RuntimeError as e:
        # Already running — someone else (supervisor, user) raced us. That's
        # the happy path for a watchdog; don't treat it as an error.
        logger.info("Watchdog: start skipped, already running: %s", e)
        return WatchdogResult(action=WatchdogAction.ALREADY_STARTING, note=str(e))
    except Exception as e:
        # Don't propagate — Task Scheduler would flag the task as failed.
        logger.error("Watchdog: start_service raised: %s", e)
        return WatchdogResult(action=WatchdogAction.START, note=f"error: {e}")
