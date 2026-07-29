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

Registration goes through a **task XML document**, not ``schtasks /sc onlogon``.
That is not a stylistic preference. ``/sc onlogon`` builds a logon trigger with
no ``<UserId>`` — "at logon of *any* user" — which the scheduler will only
accept from an administrator, so the earlier implementation could not register
autostart for a standard account at all: measured on a non-admin machine,
``/sc onlogon`` is refused with *Access is denied* while ``/sc once`` succeeds
in the same namespace. A per-user product that needs elevation to start itself
is broken for the user it is aimed at. Naming the user in the trigger removes
the requirement entirely.

Writing the XML ourselves also settles three defaults that Windows gets wrong
for a background service and that ``/sc onlogon`` left untouched: it would not
start on battery, it would be *stopped* on switching to battery, and it would be
killed after the default 72-hour execution limit. It also supplies the native
``RestartOnFailure`` policy that makes a bespoke watchdog process unnecessary.
"""

from __future__ import annotations

import os
import re
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

#: Task Scheduler folder for the product's registrations. Injectable so a
#: verification probe can register somewhere it is IMPOSSIBLE to collide with a
#: real entry — `spec.name` cannot serve that purpose, because the target is
#: derived from `kind` to keep "exactly one registration per concern" true.
TASK_PREFIX = r"\RAGTools"
TASK_SERVICE = TASK_PREFIX + r"\Service"
TASK_TRAY = TASK_PREFIX + r"\Tray"

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

#: The GUI-subsystem sibling of `rag.exe`, built from the same bundle by
#: `rag.spec`. Windows allocates a console for a console-subsystem image
#: whenever the OS starts it, and Task Scheduler starts the autostart entry
#: itself — so the executable named in the task is the only thing that decides
#: whether a terminal window appears at login. See `background_executable`.
WINDOWED_EXE_NAME = "ragw.exe"

#: Inno's AppId for RAG Tools. Its uninstall key is `{AppId}_is1`, and that key
#: is what Add/Remove Programs, winget and Inno's own upgrade detection read —
#: so an upgrade that replaces files without updating it leaves the rest of the
#: system describing the previous version.
APP_ID = "{7E4B2A3C-F1D8-4A5E-B9C0-1234567890AB}"
UNINSTALL_KEY = (r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
                 rf"\{APP_ID}_is1")

#: Images an installation owns. `qdrant.exe` counts: managed storage supervises
#: one, and it holds handles an upgrade may need released.
OWNED_IMAGES = ("rag.exe", "ragw.exe", "qdrant.exe")
_POWERSHELL_OWNED_IMAGES = ",".join(f"'{name}'" for name in OWNED_IMAGES)

#: Task Scheduler accepts nothing but UTF-16 for `/xml`. A UTF-8 file — even
#: with a BOM — is rejected outright as "The task XML is malformed", which reads
#: like a schema error and is not one. Measured, not assumed.
TASK_XML_ENCODING = "utf-16"

#: How the scheduler restarts a service that dies. This is the native
#: replacement for the 462-line watchdog process: the previous implementation
#: registered no restart policy at all, so the watchdog was doing by hand what
#: nothing had ever asked the scheduler to do.
RESTART_INTERVAL = "PT1M"
RESTART_COUNT = 3

#: Task-folder names this adapter is willing to interpolate into a command.
_SAFE_FOLDER = re.compile(r"[A-Za-z0-9 _.\-]+")


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
        task_prefix: str = TASK_PREFIX,
        user: Optional[str] = None,
    ):
        self._run = runner or default_runner
        self._task_prefix = task_prefix
        self._user = user
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

    def child_process_flags(self) -> dict:
        """``CREATE_NO_WINDOW`` — but NOT ``DETACHED_PROCESS``.

        The managed engine is a CONSOLE-subsystem image (``qdrant.exe``).
        Launched from a process that has no console of its own, Windows would
        otherwise give it one — the same stray-window class already fixed for
        the launcher. Detaching it, on the other hand, is exactly wrong here:
        this child must stay ours so a thread can wait on it and read its exit
        code, which is the whole of the v3.3.0 supervision fix.
        """
        return {"creationflags": 0x08000000}      # CREATE_NO_WINDOW

    #: Windows ships one; see WINDOWED_EXE_NAME.
    windowed_executable_name = WINDOWED_EXE_NAME

    #: Windows keeps one (the uninstall registry), so "nothing recorded"
    #: on Windows is a finding rather than a non-question.
    records_installed_version = True

    def recorded_version(self) -> Optional[str]:
        """`DisplayVersion` from Inno's uninstall key, or None if absent.

        Read from both hives: an install can be per-user or machine-wide, and
        looking in only one is how a present entry reads as missing.
        """
        try:
            import winreg
        except ImportError:  # pragma: no cover — Windows always has it
            return None

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, UNINSTALL_KEY) as key:
                    value, _ = winreg.QueryValueEx(key, "DisplayVersion")
            except OSError:
                continue
            return str(value)
        return None

    def owned_processes(self) -> Optional[list[tuple[int, str, str]]]:
        """Running rag/ragw/qdrant processes and the images they came from."""
        script = (
            "Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.Name -in {_POWERSHELL_OWNED_IMAGES} }} | "
            "ForEach-Object { \"$($_.ProcessId)|$($_.Name)|$($_.ExecutablePath)\" }"
        )
        result = self._run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", script])
        if not result.ok:
            # Could not look. Saying so is the point — see the contract.
            return None
        found: list[tuple[int, str, str]] = []
        for line in (result.stdout or "").splitlines():
            parts = line.strip().split("|")
            if len(parts) == 3 and parts[0].isdigit():
                found.append((int(parts[0]), parts[1], parts[2]))
        return found

    def background_executable(self, executable: str) -> str:
        """Swap in the windowless sibling when the bundle ships one.

        Presence, not configuration: a source checkout points at `python.exe`
        and a pip shim at `Scripts\\rag.exe`, and neither has a `ragw.exe`
        beside it, so both fall through with no `is_packaged()` question asked.
        A bundle built before v3.0.1 has no sibling either — its registrations
        keep working exactly as they did, minus the fix.

        Falling back rather than raising is the deliberate choice: the failure
        this replaces is cosmetic, and refusing to register autostart because a
        window might appear would trade an ugly login for no service at all.
        """
        try:
            candidate = Path(executable).with_name(WINDOWED_EXE_NAME)
        except (ValueError, OSError):
            # `with_name` refuses on an empty or drive-only path. Nothing to
            # resolve against, so there is nothing to improve.
            return executable
        return str(candidate) if candidate.is_file() else executable

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
        """The registration target. Derived from `kind`, never from `spec.name`.

        `spec.name` is a DISPLAY name. Treating it as the target would let a
        caller create a second service registration under another name, which
        is exactly the duplication this design exists to prevent — and a probe
        that thought it had chosen a sandbox name would in fact have overwritten
        the real one.
        """
        leaf = "Tray" if kind == KIND_TRAY else "Service"
        return self._task_prefix + "\\" + leaf

    def install_autostart(self, spec: AutostartSpec) -> Registration:
        """Register one at-logon task from an XML definition. Replaces (`/F`).

        The XML is written to a temp file rather than piped, because `schtasks`
        takes a path and nothing else, and removed in a `finally` so a failed
        registration does not leave a task definition — which names the account
        it runs as — lying around in the temp directory.
        """
        import tempfile

        task = self._task_path(spec.kind)
        document = render_task_xml(spec, user=self.current_user(), task_path=task)

        handle, raw = tempfile.mkstemp(prefix="ragtools-task-", suffix=".xml")
        os.close(handle)
        definition = Path(raw)
        try:
            definition.write_text(document, encoding=TASK_XML_ENCODING)
            result = self._run(
                ["schtasks", "/create", "/tn", task, "/xml", str(definition), "/f"])
        finally:
            try:
                definition.unlink()
            except OSError:
                pass

        if not result.ok:
            raise RuntimeError(
                f"failed to register {task}: {result.stderr.strip() or result.stdout.strip()}"
            )

        # Registering the current mechanism is also when the superseded ones go.
        #
        # `find_autostart` has always REPORTED legacy registrations, and nothing
        # ever acted on the report: the installer's comment says the upgrade
        # engine removes them, but that engine is `rag upgrade`, which does not
        # exist. So every upgrade from v2 left "RAGTools Watchdog" registered,
        # pointing at a watchdog v3 deleted — a task that fires at every logon
        # and fails, forever. Caught by `rag selfcheck` on a real 2.7.0 -> 3.1.0
        # upgrade, which is precisely the job it was added to do.
        #
        # Best-effort: a legacy task that cannot be removed must not fail the
        # registration that just succeeded. `selfcheck` still reports it.
        self._sweep_legacy(spec.kind)

        return Registration(
            name=task, kind=spec.kind, mechanism="task-scheduler",
            target=_command_line(spec.argv), enabled=True,
        )

    def _sweep_legacy(self, kind: str) -> list[str]:
        """Delete superseded registrations of `kind`. Returns what went."""
        removed: list[str] = []
        for registration in self.find_autostart(kind):
            if not registration.legacy:
                continue
            try:
                if registration.mechanism == "task-scheduler":
                    if self._run(["schtasks", "/delete", "/tn",
                                  registration.name, "/f"]).ok:
                        removed.append(registration.name)
                elif registration.path is not None:
                    registration.path.unlink(missing_ok=True)
                    removed.append(registration.name)
            except OSError:
                continue
        return removed

    def current_user(self) -> str:
        """The account autostart is registered for. Injectable for tests."""
        return self._user or _current_user()

    def remove_autostart(self, name: str) -> list[Registration]:
        """Remove a registration by name. Idempotent — absent is success."""
        removed: list[Registration] = []
        pruneable = False
        for existing in self.find_autostart(KIND_SERVICE) + self.find_autostart(KIND_TRAY):
            if existing.name != name:
                continue
            if existing.mechanism == "task-scheduler":
                if self._run(["schtasks", "/delete", "/tn", existing.name, "/f"]).ok:
                    removed.append(existing)
                    pruneable = existing.name.startswith(self._task_prefix + "\\")
            elif existing.path is not None:
                try:
                    existing.path.unlink()
                    removed.append(existing)
                except FileNotFoundError:
                    removed.append(existing)      # already gone == removed
                except OSError:
                    pass
        if pruneable:
            self._prune_task_folder()
        return removed

    def _prune_task_folder(self) -> None:
        """Drop the product's task folder once its last task is gone.

        Task Scheduler keeps a folder after everything inside it is deleted, so
        removing both registrations still leaves a ``RAGTools`` node in the
        scheduler tree — residue that an uninstall claiming "nothing survives"
        would be wrong about.

        ``DeleteFolder`` refuses a folder that still has contents, which makes
        this self-guarding: it can only succeed when there is genuinely nothing
        left. Best effort throughout — failing to tidy a folder must never turn
        a successful removal into an error.
        """
        folder = self._task_prefix.strip("\\")
        # The prefix is ours, not user input; the pattern is belt-and-braces
        # against ever interpolating a quote or a statement separator.
        if not folder or not _SAFE_FOLDER.fullmatch(folder):
            return
        self._run([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "$s = New-Object -ComObject Schedule.Service; $s.Connect(); "
            f"try {{ $s.GetFolder('\\').DeleteFolder('{folder}', 0) }} catch {{ }}",
        ])

    def find_autostart(self, kind: str = KIND_SERVICE) -> list[Registration]:
        found: list[Registration] = []

        # 1. The current mechanism.
        #
        # `/fo LIST /v` is not decoration. The default output is a three-column
        # summary — TaskName, Next Run Time, Status — and the registered command
        # is simply not in it, so every target came back empty and `selfcheck`
        # reported "no autostart registered" for two correctly-registered tasks.
        # Only the verbose list format emits `Task To Run:`.
        task = self._task_path(kind)
        probe = self._run(["schtasks", "/query", "/tn", task, "/fo", "LIST", "/v"])
        if probe.ok:
            found.append(Registration(
                name=task, kind=kind, mechanism="task-scheduler",
                target=_task_target(probe.stdout), enabled=True,
            ))

        # 2. Superseded scheduled tasks.
        for legacy_name, legacy_kind in LEGACY_TASKS.items():
            if legacy_kind != kind:
                continue
            legacy_probe = self._run(
                ["schtasks", "/query", "/tn", legacy_name, "/fo", "LIST", "/v"])
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
    """Quote one argv element for a Windows command line."""
    return f'"{arg}"' if " " in arg and not arg.startswith('"') else arg


def _command_line(argv: Sequence[str]) -> str:
    return " ".join(_quote(a) for a in argv)


def _current_user() -> str:
    """``DOMAIN\\user`` for the account this process runs as.

    Resolved through ``GetUserNameExW`` rather than ``%USERDOMAIN%\\%USERNAME%``
    because the environment is inherited and can be stale or overridden, and a
    logon trigger naming the wrong account is a task that never fires — a
    failure that only shows up at the next login, on someone else's machine.
    The environment is the fallback, and the only path on non-Windows hosts
    (the suite renders this XML on Linux and macOS runners).
    """
    import ctypes

    windll = getattr(ctypes, "windll", None)
    if windll is not None:
        NAME_SAM_COMPATIBLE = 2
        size = ctypes.c_ulong(0)
        try:
            windll.secur32.GetUserNameExW(NAME_SAM_COMPATIBLE, None, ctypes.byref(size))
            buffer = ctypes.create_unicode_buffer(max(size.value, 256))
            size.value = len(buffer)
            if windll.secur32.GetUserNameExW(NAME_SAM_COMPATIBLE, buffer,
                                             ctypes.byref(size)) and buffer.value:
                return buffer.value
        except (AttributeError, OSError):
            pass

    domain = os.environ.get("USERDOMAIN", "").strip()
    user = os.environ.get("USERNAME", "").strip()
    return f"{domain}\\{user}" if domain and user else user


def _iso_duration(seconds: int) -> str:
    """Seconds as an ISO-8601 duration, which is what the task schema wants."""
    seconds = max(0, int(seconds))
    if seconds and seconds % 60 == 0:
        return f"PT{seconds // 60}M"
    return f"PT{seconds}S"


def render_task_xml(spec: AutostartSpec, *, user: str, task_path: str) -> str:
    """The task definition, as a pure function so the suite can assert on it.

    Element order mirrors what Task Scheduler itself emits on a round trip
    rather than what the published schema implies — the two disagree, and the
    scheduler is the one that has to accept the file.
    """
    from xml.sax.saxutils import escape

    trigger_delay = (f"\n      <Delay>{_iso_duration(spec.delay_seconds)}</Delay>"
                     if spec.delay_seconds else "")
    working_dir = (f"\n      <WorkingDirectory>{escape(str(spec.working_dir))}"
                   "</WorkingDirectory>" if spec.working_dir else "")
    arguments = (f"\n      <Arguments>{escape(_command_line(spec.argv[1:]))}</Arguments>"
                 if len(spec.argv) > 1 else "")
    description = (f"\n    <Description>{escape(spec.description)}</Description>"
                   if spec.description else "")

    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>{description}
    <URI>{escape(task_path)}</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <RestartOnFailure>
      <Interval>{RESTART_INTERVAL}</Interval>
      <Count>{RESTART_COUNT}</Count>
    </RestartOnFailure>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
  </Settings>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>{trigger_delay}
    </LogonTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(spec.argv[0])}</Command>{arguments}{working_dir}
    </Exec>
  </Actions>
</Task>
"""


def _task_target(query_output: str) -> str:
    """The registered command, from ``schtasks /query /fo LIST /v`` output.

    Load-bearing, and it has to actually work. `selfcheck.check_autostart_targets`
    is the check that catches "the machine reverts to the previous build at the
    next logon", and it decides entirely on this value.

    The previous implementation could not return one. It scanned for a line
    containing both a backslash and ``.exe`` — against the output of
    ``schtasks /query /tn <task>`` with no format flags, which does not contain
    the command at all. Measured::

        Folder: \\RAGTools
        TaskName                Next Run Time          Status
        ======================= ====================== ==============
        Service                 N/A                    Ready

    Zero lines with ``.exe``; the TaskName column carries the leaf name with no
    backslash either, so both halves of the predicate failed. It returned ``""``
    on every real Windows machine, and `selfcheck` read that as "no autostart
    registered" — a SKIP. The check could not pass, and, far worse, could not
    FAIL: a task pointing at the previous install directory produced the same
    empty string and the same reassuring skip.

    ``Task To Run:`` is what carries it, and only the verbose list format emits
    it. Localised Windows uses a translated label, so the fallback recognises the
    value by shape rather than by its label.
    """
    lines = query_output.splitlines()
    for index, line in enumerate(lines):
        label, sep, value = line.partition(":")
        if not sep or "task to run" not in label.strip().lower():
            continue
        # schtasks wraps long commands onto continuation lines with no label.
        parts = [value.strip()]
        for following in lines[index + 1:]:
            if not following.strip() or ":" in following.partition(":")[1]:
                break
            parts.append(following.strip())
        target = " ".join(p for p in parts if p).strip()
        if target:
            return target

    # Localised label, or an unexpected layout: fall back to the value's shape.
    # An executable path is the thing we need, and it looks like one.
    for line in lines:
        candidate = line.partition(":")[2].strip() or line.strip()
        lowered = candidate.lower()
        if ("\\" in candidate and (".exe" in lowered or ".vbs" in lowered)
                and not lowered.startswith("taskname")):
            return candidate
    return ""
