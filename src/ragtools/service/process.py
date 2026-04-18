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


def get_supervisor_pid_file_path(settings: Settings) -> Path:
    """PID file for the supervisor process (when supervised mode is used)."""
    return Path(settings.qdrant_path).parent / "supervisor.pid"


def _read_supervisor_pid(settings: Settings) -> int | None:
    """Same contract as _read_pid() but for the supervisor PID file."""
    path = get_supervisor_pid_file_path(settings)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
    except (ValueError, OSError):
        return None
    if _process_alive(pid):
        return pid
    path.unlink(missing_ok=True)
    logger.info("Removed stale supervisor PID file (dead PID %d).", pid)
    return None


def _read_pid_raw(settings: Settings) -> int | None:
    """Read the PID value from the file without checking liveness."""
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


def _read_pid(settings: Settings) -> int | None:
    """Read the PID and verify the process is alive.

    Returns the PID only if the process is actually running. If the file
    exists but points at a dead PID (hard-crash with no cleanup by the
    crashed process), the file is removed and None is returned.
    """
    pid = _read_pid_raw(settings)
    if pid is None:
        return None
    if _process_alive(pid):
        return pid
    # Stale PID — self-heal the file so downstream code (automation,
    # admin panel, other callers) sees an accurate "not running" state.
    get_pid_file_path(settings).unlink(missing_ok=True)
    logger.info("Removed stale PID file (dead PID %d).", pid)
    return None


def _clean_stale_pid(settings: Settings) -> None:
    """Back-compat wrapper. _read_pid now self-cleans."""
    _read_pid(settings)


def _build_service_run_cmd(settings: Settings) -> list[str]:
    """Command line that starts the real service in the foreground."""
    if getattr(sys, "frozen", False):
        return [
            sys.executable, "service", "run",
            "--host", settings.service_host,
            "--port", str(settings.service_port),
        ]
    return [
        sys.executable, "-m", "ragtools.service.run",
        "--host", settings.service_host,
        "--port", str(settings.service_port),
    ]


def _build_service_supervise_cmd(settings: Settings) -> list[str]:
    """Command line that starts the supervisor (which spawns the real service)."""
    if getattr(sys, "frozen", False):
        return [
            sys.executable, "service", "supervise",
            "--host", settings.service_host,
            "--port", str(settings.service_port),
        ]
    return [
        sys.executable, "-m", "ragtools.cli", "service", "supervise",
        "--host", settings.service_host,
        "--port", str(settings.service_port),
    ]


def start_service(settings: Settings, supervise: bool = True) -> int:
    """Launch service as a detached background process. Returns the PID
    of the detached process (supervisor when ``supervise=True``, else the
    real service process).

    Raises RuntimeError if service is already running.

    Args:
        settings: Current Settings.
        supervise: When True (default), the detached process is the
            supervisor, which spawns and respawns the real service on
            crash. When False, the real service is launched directly,
            which matches the pre-v2.4.3 behavior.
    """
    # Clean up any stale state
    _clean_stale_pid(settings)
    pid = _read_pid(settings)
    if pid and _process_alive(pid):
        raise RuntimeError(f"Service already running (PID {pid})")

    if supervise:
        sup_pid = _read_supervisor_pid(settings)
        if sup_pid:
            raise RuntimeError(f"Supervisor already running (PID {sup_pid})")

    import subprocess

    cmd = (
        _build_service_supervise_cmd(settings)
        if supervise
        else _build_service_run_cmd(settings)
    )

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

    # When supervised, the supervisor writes its own supervisor.pid.
    # When not supervised, write service.pid here for parity with legacy behavior.
    if not supervise:
        pid_path = get_pid_file_path(settings)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(proc.pid))

    return proc.pid


_GRACEFUL_SHUTDOWN_WAIT_SECONDS = 6


def stop_service(settings: Settings) -> bool:
    """Stop the running service. Returns True if stopped successfully.

    Flow:
      1. Try graceful HTTP shutdown. When the service is supervised, a
         child-exit with code 0 tells the supervisor to exit cleanly too.
      2. If the process hasn't exited within ``_GRACEFUL_SHUTDOWN_WAIT_SECONDS``
         after the 200 response, fall through to force-kill. This cap exists
         because external callers (tray apps, watchdogs, CI scripts) commonly
         wrap this in a ``subprocess.run(timeout=N)`` and we don't want a
         slow Qdrant close to starve them into timing out before force-kill
         ever gets a chance to fire.
      3. When supervised, kill the supervisor FIRST so it doesn't respawn
         the child we're about to kill.
    """
    import httpx

    url = f"http://{settings.service_host}:{settings.service_port}"

    # Try graceful shutdown via API
    try:
        r = httpx.post(f"{url}/api/shutdown", timeout=5.0)
        if r.status_code == 200:
            # Poll once per 500 ms — faster feedback than 1s and still cheap.
            deadline = time.monotonic() + _GRACEFUL_SHUTDOWN_WAIT_SECONDS
            while time.monotonic() < deadline:
                pid = _read_pid(settings)
                sup_pid = _read_supervisor_pid(settings)
                if pid is None and sup_pid is None:
                    get_pid_file_path(settings).unlink(missing_ok=True)
                    get_supervisor_pid_file_path(settings).unlink(missing_ok=True)
                    return True
                time.sleep(0.5)
            logger.warning(
                "Graceful shutdown accepted but process did not exit within %ds; escalating to force-kill.",
                _GRACEFUL_SHUTDOWN_WAIT_SECONDS,
            )
    except Exception:
        pass

    # Force kill — order matters: supervisor first to prevent respawn.
    return _force_kill(settings)


def _terminate_pid(pid: int) -> None:
    """Platform-appropriate force-terminate of a single PID."""
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


def _force_kill(settings: Settings) -> bool:
    """Force kill the service (and supervisor if present).

    Order is important: kill supervisor first so it cannot respawn the
    real service while we're in the middle of killing it.
    """
    sup_pid = _read_supervisor_pid(settings)
    svc_pid = _read_pid(settings)

    if sup_pid is None and svc_pid is None:
        return False

    try:
        # 1. Kill supervisor to disarm respawn.
        if sup_pid is not None:
            _terminate_pid(sup_pid)
            for _ in range(5):
                if not _process_alive(sup_pid):
                    break
                time.sleep(1)

        # 2. Kill the real service.
        if svc_pid is not None and _process_alive(svc_pid):
            _terminate_pid(svc_pid)
            for _ in range(5):
                if not _process_alive(svc_pid):
                    break
                time.sleep(1)

        # Clean up both PID files regardless of liveness outcomes.
        get_pid_file_path(settings).unlink(missing_ok=True)
        get_supervisor_pid_file_path(settings).unlink(missing_ok=True)
        return True
    except Exception as e:
        logger.error("Force kill failed: %s", e)
        return False


def service_status(settings: Settings) -> dict:
    """Check if service is running and get status info.

    When supervised, the returned dict includes `supervisor_pid` so operators
    and automation can tell who is keeping the service alive.
    """
    import httpx

    url = f"http://{settings.service_host}:{settings.service_port}"

    # Try health endpoint (authoritative "running" signal)
    try:
        r = httpx.get(f"{url}/health", timeout=2.0)
        if r.status_code == 200:
            return {
                "running": True,
                "status": "ready",
                "pid": _read_pid(settings),
                "supervisor_pid": _read_supervisor_pid(settings),
                "port": settings.service_port,
                "host": settings.service_host,
                **r.json(),
            }
    except Exception:
        pass

    # Health is down but the service process may still be coming up.
    # Consider either PID file as evidence the system is in a transient state.
    pid = _read_pid(settings)
    sup_pid = _read_supervisor_pid(settings)
    if (pid and _process_alive(pid)) or (sup_pid and _process_alive(sup_pid)):
        return {
            "running": True,
            "status": "starting",
            "pid": pid,
            "supervisor_pid": sup_pid,
            "port": settings.service_port,
        }

    # Not running at all; any stale files have already been cleaned by
    # _read_pid / _read_supervisor_pid.
    return {"running": False}
