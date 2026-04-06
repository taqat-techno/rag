"""Windows Task Scheduler integration for automatic service startup.

Uses schtasks.exe to create/remove a logon-triggered task.
No admin privileges required for user-level tasks.
"""

import csv
import io
import logging
import subprocess
import sys
from pathlib import Path

from ragtools.config import Settings

logger = logging.getLogger("ragtools.service")

TASK_NAME = "RAGTools Service"


def _check_windows() -> None:
    """Raise if not on Windows."""
    if sys.platform != "win32":
        raise RuntimeError("Startup integration is only available on Windows")


def _get_exe_path() -> str:
    """Get the Python executable path for the scheduled task command."""
    return sys.executable


def _build_task_command(settings: Settings) -> str:
    """Build the command string the scheduled task will execute.

    Uses absolute paths to avoid CWD dependency (Task Scheduler runs
    from C:\\Windows\\system32 by default).
    """
    from ragtools.config import is_packaged, get_data_dir

    exe = _get_exe_path()

    if is_packaged():
        # Packaged mode: paths resolve via %LOCALAPPDATA% automatically
        return f'"{exe}" -m ragtools.service.run --from-scheduler'
    else:
        # Dev mode: set RAG_DATA_DIR to ensure absolute path resolution
        data_dir = str(Path(settings.qdrant_path).parent.resolve())
        return f'cmd /c "set RAG_DATA_DIR={data_dir} && "{exe}" -m ragtools.service.run --from-scheduler"'


def _delay_str(seconds: int) -> str:
    """Convert seconds to schtasks delay format (HHHH:MM)."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if seconds < 60:
        minutes = 1  # Minimum 1 minute for schtasks
    return f"{hours:04d}:{minutes:02d}"


def install_task(settings: Settings, delay_seconds: int | None = None) -> bool:
    """Create a Windows scheduled task that starts the service at user logon.

    Args:
        settings: Application settings.
        delay_seconds: Seconds to delay after logon. Uses settings.startup_delay if None.

    Returns:
        True if task was created successfully.

    Raises:
        RuntimeError: If not on Windows or schtasks fails.
    """
    _check_windows()

    delay = delay_seconds if delay_seconds is not None else settings.startup_delay
    command = _build_task_command(settings)
    delay_fmt = _delay_str(delay)

    cmd = [
        "schtasks", "/create",
        "/tn", TASK_NAME,
        "/tr", command,
        "/sc", "onlogon",
        "/delay", delay_fmt,
        "/f",                # Force overwrite if exists
        "/rl", "limited",    # Run with limited privileges
    ]

    logger.info("Installing scheduled task: %s (delay=%ds)", TASK_NAME, delay)
    logger.debug("Command: %s", " ".join(cmd))

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip()
        logger.error("Failed to create task: %s", error)
        raise RuntimeError(f"schtasks failed: {error}")

    logger.info("Scheduled task created successfully")
    return True


def uninstall_task() -> bool:
    """Remove the Windows scheduled task.

    Returns:
        True if task was removed or didn't exist.
    """
    _check_windows()

    cmd = ["schtasks", "/delete", "/tn", TASK_NAME, "/f"]

    logger.info("Removing scheduled task: %s", TASK_NAME)
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        # Task might not exist — that's fine
        if "cannot find" in result.stderr.lower() or "cannot find" in result.stdout.lower():
            logger.info("Task was not installed")
            return True
        error = result.stderr.strip() or result.stdout.strip()
        logger.error("Failed to delete task: %s", error)
        return False

    logger.info("Scheduled task removed")
    return True


def is_task_installed() -> bool:
    """Check if the scheduled task exists."""
    if sys.platform != "win32":
        return False

    cmd = ["schtasks", "/query", "/tn", TASK_NAME]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def get_task_info() -> dict | None:
    """Get detailed task information.

    Returns:
        Dict with task details, or None if task doesn't exist.
    """
    if sys.platform != "win32":
        return None

    cmd = ["schtasks", "/query", "/tn", TASK_NAME, "/fo", "CSV", "/v"]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        return None

    try:
        reader = csv.DictReader(io.StringIO(result.stdout))
        for row in reader:
            return {
                "task_name": row.get("TaskName", TASK_NAME),
                "status": row.get("Status", "Unknown"),
                "last_run": row.get("Last Run Time", "Never"),
                "last_result": row.get("Last Result", "N/A"),
                "next_run": row.get("Next Run Time", "N/A"),
                "command": row.get("Task To Run", "N/A"),
            }
    except Exception as e:
        logger.debug("Failed to parse task info: %s", e)
        # Fallback: task exists but can't parse details
        return {"task_name": TASK_NAME, "status": "Installed", "last_run": "Unknown"}

    return None
