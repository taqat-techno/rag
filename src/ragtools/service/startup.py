"""Windows startup integration for automatic service launch.

Uses the Windows Startup folder (shell:startup) to register a VBScript
launcher that runs on user login. No elevation or admin privileges required.

Replaces the previous schtasks approach which required elevation for
/sc onlogon triggers — causing "Access is denied" on standard user accounts.
"""

import logging
import os
import sys
from pathlib import Path

from ragtools.config import Settings

logger = logging.getLogger("ragtools.service")

TASK_NAME = "RAGTools Service"
STARTUP_FILENAME = "RAGTools.vbs"


def _check_windows() -> None:
    """Raise if not on Windows."""
    if sys.platform != "win32":
        raise RuntimeError("Startup integration is only available on Windows")


def _get_startup_folder() -> Path:
    """Get the Windows Startup folder path."""
    # %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        raise RuntimeError("APPDATA environment variable not set")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _get_startup_script_path() -> Path:
    """Get the full path to the startup script."""
    return _get_startup_folder() / STARTUP_FILENAME


def _build_startup_script(settings: Settings, delay_seconds: int) -> str:
    """Build a VBScript that starts the RAG service after a delay.

    The script:
    1. Waits for the configured delay (lets the system settle after login)
    2. Checks if the service is already running (health check)
    3. If not running, starts it silently
    4. Optionally opens the browser
    """
    from ragtools.config import is_packaged

    if is_packaged():
        exe_path = sys.executable
    else:
        exe_path = sys.executable
        # In dev mode, we need to run python -m ragtools.service.run
        # But VBScript can't easily do this, so use the CLI entry point
        # which should be on PATH after pip install -e .

    # Build the shell.Run command string with proper VBScript quoting.
    # In VBScript: "" inside a string literal = escaped double-quote.
    # shell.Run expects: shell.Run "full command string", 0, False
    if is_packaged():
        run_args = "service start"
    else:
        run_args = "-m ragtools.cli service start"

    # Escape the exe path for VBScript (double quotes around path with spaces)
    # Result: shell.Run """C:\path\to\python.exe"" -m ragtools.cli service start", 0, False
    vbs_cmd = f'"""{exe_path}"" {run_args}"'

    # Determine the working directory for the service process
    from ragtools.config import get_data_dir
    work_dir = str(get_data_dir().parent)  # Parent of data dir (e.g., %LOCALAPPDATA%\RAGTools)

    return f"""' RAGTools Auto-Start Script
' Created by RAGTools service install
' Starts the RAG service after login with a delay

Dim shell
Set shell = CreateObject("WScript.Shell")

' Wait for system to settle after login
WScript.Sleep {delay_seconds * 1000}

' Check if service is already running
Dim healthy
healthy = False
On Error Resume Next
Dim http
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "GET", "http://127.0.0.1:{settings.service_port}/health", False
http.Send
If http.Status = 200 Then healthy = True
Set http = Nothing
On Error GoTo 0

' Start service if not already running
If Not healthy Then
    shell.CurrentDirectory = "{work_dir}"
    shell.Run {vbs_cmd}, 0, False
End If
"""


def install_task(settings: Settings, delay_seconds: int | None = None) -> bool:
    """Register RAG Tools to start automatically on Windows login.

    Places a VBScript in the Windows Startup folder. This approach:
    - Requires NO elevation (unlike schtasks /sc onlogon)
    - Is visible in Task Manager > Startup tab
    - Survives reboots
    - Can be disabled by the user via Task Manager

    Args:
        settings: Application settings.
        delay_seconds: Seconds to delay after login. Uses settings.startup_delay if None.

    Returns:
        True if startup registration succeeded.

    Raises:
        RuntimeError: If not on Windows or file operation fails.
    """
    _check_windows()

    delay = delay_seconds if delay_seconds is not None else settings.startup_delay
    startup_dir = _get_startup_folder()

    if not startup_dir.exists():
        raise RuntimeError(f"Startup folder not found: {startup_dir}")

    script_content = _build_startup_script(settings, delay)
    script_path = _get_startup_script_path()

    logger.info("Installing startup script: %s (delay=%ds)", script_path, delay)
    script_path.write_text(script_content, encoding="utf-8")
    logger.info("Startup script installed successfully")
    return True


def uninstall_task() -> bool:
    """Remove RAG Tools from Windows startup.

    Returns:
        True if removed or didn't exist.
    """
    _check_windows()

    script_path = _get_startup_script_path()
    logger.info("Removing startup script: %s", script_path)

    if script_path.exists():
        script_path.unlink()
        logger.info("Startup script removed")
    else:
        logger.info("Startup script was not installed")

    # Also clean up old schtasks-based task if it exists
    try:
        import subprocess
        result = subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            logger.info("Removed legacy scheduled task: %s", TASK_NAME)
    except Exception:
        pass

    return True


def is_task_installed() -> bool:
    """Check if RAG Tools is registered for startup."""
    if sys.platform != "win32":
        return False
    return _get_startup_script_path().exists()


def get_task_info() -> dict | None:
    """Get startup registration details.

    Returns:
        Dict with details, or None if not registered.
    """
    if sys.platform != "win32":
        return None

    script_path = _get_startup_script_path()
    if not script_path.exists():
        return None

    return {
        "task_name": TASK_NAME,
        "status": "Installed",
        "location": str(script_path),
        "method": "Startup folder",
    }
