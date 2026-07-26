"""Windows adapter — Task Scheduler, and everything the old design leaves behind.

The v2 design registered autostart three different ways at once, and this
machine still carries all three: a ``RAGTools Service`` scheduled task, a
``RAGTools.vbs`` in the Startup folder, a ``RAGTools-Tray.vbs`` beside it, and a
``RAGTools Watchdog`` task that exists only to restart a service the scheduler
can restart natively. Nothing could enumerate them, so every upgrade added
rather than replaced.

This adapter registers **one** task per concern and — more importantly —
:meth:`find_autostart` reports every legacy mechanism as well, so the upgrade
can remove precisely what it recognises and nothing else.

The ``.vbs`` shims are gone. They existed to hide a console window, which is
what ``CREATE_NO_WINDOW`` is for; shipping an interpreted script to work around
a process-creation flag is how "a black box flashes on my screen every minute"
became a shipped behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Sequence

from ragtools.platform.base import (
    KIND_SERVICE,
    KIND_TRAY,
    AutostartSpec,
    CommandRunner,
    Registration,
    default_runner,
)

#: Task Scheduler paths for the new, one-per-concern registrations.
TASK_SERVICE = r"\RAGTools\Service"
TASK_TRAY = r"\RAGTools\Tray"

#: Registrations created by superseded versions. Enumerated so the upgrade can
#: remove them; never created.
LEGACY_TASKS = {
    "RAGTools Service": KIND_SERVICE,     # v2 startup.py
    "RAGTools Watchdog": KIND_SERVICE,    # v2 watchdog.py — replaced by task restart policy
}
LEGACY_STARTUP_FILES = {
    "RAGTools.vbs": KIND_SERVICE,
    "RAGTools-Tray.vbs": KIND_TRAY,
}


class WindowsAdapter:
    """Windows implementation of :class:`~ragtools.platform.base.PlatformAdapter`."""

    name = "windows"

    def __init__(
        self,
        runner: Optional[CommandRunner] = None,
        *,
        home: Optional[Path] = None,
        local_app_data: Optional[Path] = None,
        startup_dir: Optional[Path] = None,
    ):
        self._run = runner or default_runner
        self._home = Path(home) if home else Path.home()
        self._lad = Path(local_app_data) if local_app_data else self._resolve_lad()
        self._startup = Path(startup_dir) if startup_dir else self._resolve_startup()

    # --- resolution -----------------------------------------------------

    def _resolve_lad(self) -> Path:
        raw = os.environ.get("LOCALAPPDATA", "").strip()
        return Path(raw) if raw else self._home / "AppData" / "Local"

    def _resolve_startup(self) -> Path:
        appdata = os.environ.get("APPDATA", "").strip()
        base = Path(appdata) if appdata else self._home / "AppData" / "Roaming"
        return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"

    # --- paths ----------------------------------------------------------

    def app_dir(self) -> Path:
        return self._lad / "RAGTools"

    def dev_dir(self) -> Path:
        return self._lad / "RAGTools-dev"

    # --- process --------------------------------------------------------

    def spawn_detached(self, argv: Sequence[str], **kwargs) -> int:
        import subprocess

        CREATE_NO_WINDOW = 0x08000000
        DETACHED_PROCESS = 0x00000008
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW | DETACHED_PROCESS)
        kwargs.setdefault("close_fds", True)
        return subprocess.Popen(list(argv), **kwargs).pid

    def pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                # A recycled PID whose process already exited reports its exit
                # code here; only STILL_ACTIVE means "running".
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)

    def terminate(self, pid: int, force: bool = False) -> bool:
        """Terminate via the Win32 API, not ``taskkill``.

        ``taskkill`` needs the executable on PATH and costs a process spawn;
        this path is used while shutting a hung service down, which is exactly
        when neither is safe to assume. Windows has no graceful-signal
        equivalent for a non-console process, so ``force`` does not change the
        mechanism — it is honoured on the platforms where it means something.
        """
        if pid <= 0:
            return False
        import ctypes

        PROCESS_TERMINATE = 0x0001
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            return False
        try:
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)

    # --- service / tray autostart ---------------------------------------

    def supports_autostart(self) -> bool:
        return True

    def _task_path(self, kind: str) -> str:
        return TASK_TRAY if kind == KIND_TRAY else TASK_SERVICE

    def install_autostart(self, spec: AutostartSpec) -> Registration:
        """Register one at-logon task. Replaces any same-named task (`/F`)."""
        task = self._task_path(spec.kind)
        command = " ".join(_quote(a) for a in spec.argv)
        args = [
            "schtasks", "/create", "/tn", task,
            "/tr", command,
            "/sc", "onlogon",
            "/rl", "limited",          # never elevate: this is a per-user product
            "/f",
        ]
        if spec.delay_seconds:
            args += ["/delay", _delay_hhmm(spec.delay_seconds)]
        result = self._run(args)
        if not result.ok:
            raise RuntimeError(
                f"failed to register {task}: {result.stderr.strip() or result.stdout.strip()}"
            )
        return Registration(
            name=task, kind=spec.kind, mechanism="task-scheduler",
            target=command, enabled=True,
        )

    def remove_autostart(self, name: str) -> list[Registration]:
        """Remove a registration by name. Idempotent — absent is success."""
        removed: list[Registration] = []
        for existing in self.find_autostart(KIND_SERVICE) + self.find_autostart(KIND_TRAY):
            if existing.name != name:
                continue
            if existing.mechanism == "task-scheduler":
                if self._run(["schtasks", "/delete", "/tn", existing.name, "/f"]).ok:
                    removed.append(existing)
            elif existing.path is not None:
                try:
                    existing.path.unlink()
                    removed.append(existing)
                except FileNotFoundError:
                    removed.append(existing)      # already gone == removed
                except OSError:
                    pass
        return removed

    def find_autostart(self, kind: str = KIND_SERVICE) -> list[Registration]:
        found: list[Registration] = []

        # 1. The current mechanism.
        task = self._task_path(kind)
        probe = self._run(["schtasks", "/query", "/tn", task])
        if probe.ok:
            found.append(Registration(
                name=task, kind=kind, mechanism="task-scheduler",
                target=_task_target(probe.stdout), enabled=True,
            ))

        # 2. Superseded scheduled tasks.
        for legacy_name, legacy_kind in LEGACY_TASKS.items():
            if legacy_kind != kind:
                continue
            legacy_probe = self._run(["schtasks", "/query", "/tn", legacy_name])
            if legacy_probe.ok:
                found.append(Registration(
                    name=legacy_name, kind=kind, mechanism="task-scheduler",
                    target=_task_target(legacy_probe.stdout), enabled=True, legacy=True,
                ))

        # 3. Superseded Startup-folder scripts. File-backed, so no shelling out.
        for filename, legacy_kind in LEGACY_STARTUP_FILES.items():
            if legacy_kind != kind:
                continue
            candidate = self._startup / filename
            if candidate.exists():
                found.append(Registration(
                    name=filename, kind=kind, mechanism="startup-folder",
                    target=str(candidate), enabled=True, legacy=True, path=candidate,
                ))
        return found

    # --- desktop --------------------------------------------------------

    def has_desktop_session(self) -> bool:
        # A Windows interactive session always has a shell capable of a tray.
        # Session 0 (a true service context) cannot show UI, and that is what
        # SESSIONNAME being empty or "Services" indicates.
        session = os.environ.get("SESSIONNAME", "").strip().lower()
        return session != "services"

    def open_url(self, url: str) -> bool:
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except Exception:  # noqa: BLE001
            return False

    def open_path(self, path: Path) -> bool:
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
            return True
        except Exception:  # noqa: BLE001
            return False

    def copy_text(self, text: str) -> bool:
        import subprocess

        try:
            subprocess.run(["clip"], input=text.encode("utf-8"), check=False,
                           creationflags=0x08000000)
            return True
        except Exception:  # noqa: BLE001
            return False


def _quote(arg: str) -> str:
    """Quote one argv element for a schtasks `/tr` command string."""
    return f'"{arg}"' if " " in arg and not arg.startswith('"') else arg


def _delay_hhmm(seconds: int) -> str:
    """schtasks wants a delay as ``mmmm:ss`` (up to 9999 minutes)."""
    seconds = max(0, min(seconds, 9999 * 60))
    return f"{seconds // 60:04d}:{seconds % 60:02d}"


def _task_target(query_output: str) -> str:
    """Best-effort task command from `schtasks /query` output.

    Advisory only — the target is shown to a human deciding whether to remove a
    registration, never parsed for control flow.
    """
    for line in query_output.splitlines():
        if "\\" in line and (".exe" in line.lower() or ".vbs" in line.lower()):
            return line.strip()
    return ""
