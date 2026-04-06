"""Service process management — start, stop, status via subprocess + PID file."""

import logging
import os
import sys
import time
from pathlib import Path

from ragtools.config import Settings

logger = logging.getLogger("ragtools.service")


def get_pid_file_path(settings: Settings) -> Path:
    """Get the PID file path based on data directory."""
    return Path(settings.qdrant_path).parent / "service.pid"


def _read_pid(settings: Settings) -> int | None:
    """Read PID from file. Returns None if file doesn't exist or is invalid."""
    pid_path = get_pid_file_path(settings)
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None


def _process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _clean_stale_pid(settings: Settings) -> None:
    """Remove PID file if the process is not running."""
    pid = _read_pid(settings)
    if pid and not _process_alive(pid):
        get_pid_file_path(settings).unlink(missing_ok=True)


def start_service(settings: Settings) -> int:
    """Launch service as a detached background process. Returns PID.

    Raises RuntimeError if service is already running.
    """
    _clean_stale_pid(settings)
    pid = _read_pid(settings)
    if pid and _process_alive(pid):
        raise RuntimeError(f"Service already running (PID {pid})")

    import subprocess

    # Build command
    cmd = [
        sys.executable, "-m", "ragtools.service.run",
        "--host", settings.service_host,
        "--port", str(settings.service_port),
    ]

    # Log file
    log_dir = Path(settings.qdrant_path).parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "service.log"

    # Platform-specific process creation
    kwargs = {}
    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        DETACHED_PROCESS = 0x00000008
        kwargs["creationflags"] = CREATE_NO_WINDOW | DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    with open(log_file, "a") as lf:
        proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=lf,
            **kwargs,
        )

    # Write PID file
    pid_path = get_pid_file_path(settings)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(proc.pid))

    return proc.pid


def stop_service(settings: Settings) -> bool:
    """Stop the running service. Returns True if stopped successfully.

    Tries graceful HTTP shutdown first, then force kills via PID.
    """
    import httpx

    url = f"http://{settings.service_host}:{settings.service_port}"

    # Try graceful shutdown via API
    try:
        r = httpx.post(f"{url}/api/shutdown", timeout=5.0)
        if r.status_code == 200:
            # Wait for process to exit
            for _ in range(30):
                pid = _read_pid(settings)
                if pid is None or not _process_alive(pid):
                    get_pid_file_path(settings).unlink(missing_ok=True)
                    return True
                time.sleep(1)
    except Exception:
        pass

    # Force kill via PID
    return _force_kill(settings)


def _force_kill(settings: Settings) -> bool:
    """Force kill service process via PID file."""
    pid = _read_pid(settings)
    if pid is None:
        return False

    if not _process_alive(pid):
        get_pid_file_path(settings).unlink(missing_ok=True)
        return True

    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_TERMINATE = 0x0001
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                kernel32.TerminateProcess(handle, 1)
                kernel32.CloseHandle(handle)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)

        # Wait briefly for process to exit
        for _ in range(5):
            if not _process_alive(pid):
                break
            time.sleep(1)

        get_pid_file_path(settings).unlink(missing_ok=True)
        return True
    except Exception as e:
        logger.error("Force kill failed: %s", e)
        return False


def service_status(settings: Settings) -> dict:
    """Check if service is running and get status info."""
    import httpx

    url = f"http://{settings.service_host}:{settings.service_port}"

    # Try health endpoint
    try:
        r = httpx.get(f"{url}/health", timeout=2.0)
        if r.status_code == 200:
            pid = _read_pid(settings)
            return {
                "running": True,
                "status": "ready",
                "pid": pid,
                "port": settings.service_port,
                "host": settings.service_host,
                **r.json(),
            }
    except Exception:
        pass

    # Check PID file
    pid = _read_pid(settings)
    if pid and _process_alive(pid):
        return {
            "running": True,
            "status": "starting",
            "pid": pid,
            "port": settings.service_port,
        }

    # Clean stale PID
    _clean_stale_pid(settings)
    return {"running": False}
