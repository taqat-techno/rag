"""macOS adapter — launchd agents.

**LaunchAgent, not LaunchDaemon.** An agent runs in the user's session with
access to `~`, which is where this product's data and projects live. A daemon
runs as root before login and cannot see either.

``KeepAlive`` with ``SuccessfulExit: false`` is the macOS spelling of
"restart it if it crashes, leave it alone if it exits cleanly" — the same
capability that makes a bespoke watchdog unnecessary here as on Linux.

The tray agent additionally needs ``LSUIElement`` on its bundle so a tray-only
app does not put an icon in the Dock. That is a bundle property rather than a
plist key, so it is asserted at packaging time (§7) and noted here because the
two are easy to confuse.
"""

from __future__ import annotations

import os
import plistlib
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

#: Keys launchd actually understands, from launchd.plist(5). launchd IGNORES an
#: unrecognised key silently — exactly how `StartLimitIntervalSec` in the wrong
#: systemd section discarded the crash-loop protection without a word. Since no
#: Mac is available to run `plutil`/`launchctl` against, validating the key set
#: is what stops that class of defect shipping here.
LAUNCHD_KEYS = frozenset({
    "Label", "Program", "ProgramArguments", "RunAtLoad", "KeepAlive",
    "WorkingDirectory", "EnvironmentVariables", "StandardOutPath",
    "StandardErrorPath", "ProcessType", "ThrottleInterval", "StartInterval",
    "StartCalendarInterval", "WatchPaths", "QueueDirectories", "Disabled",
    "UserName", "GroupName", "LimitLoadToSessionType", "ExitTimeOut",
    "Nice", "LowPriorityIO", "AbandonProcessGroup", "SessionCreate",
})

#: `KeepAlive` accepts a bool or a dict; these are its documented sub-keys.
KEEPALIVE_KEYS = frozenset({
    "SuccessfulExit", "NetworkState", "PathState", "OtherJobEnabled", "Crashed",
})


class InvalidAgent(ValueError):
    """A generated plist launchd would not fully understand."""


def validate_plist(document: dict) -> None:
    """Refuse a plist launchd would silently mis-read.

    launchd does not report unknown keys; it ignores them. A typo therefore
    produces an agent that loads, runs, and quietly lacks whatever the misspelt
    key was meant to configure.
    """
    unknown = sorted(set(document) - LAUNCHD_KEYS)
    if unknown:
        raise InvalidAgent(
            f"launchd does not recognise {', '.join(unknown)} — it would ignore "
            "them silently. Check launchd.plist(5)."
        )
    if not document.get("Label"):
        raise InvalidAgent("Label is required; launchd cannot address the job without it")
    argv = document.get("ProgramArguments")
    if not (argv or document.get("Program")):
        raise InvalidAgent("ProgramArguments or Program is required")
    if argv is not None and (not isinstance(argv, list) or not all(
            isinstance(a, str) for a in argv)):
        raise InvalidAgent("ProgramArguments must be a list of strings")
    keep_alive = document.get("KeepAlive")
    if isinstance(keep_alive, dict):
        bad = sorted(set(keep_alive) - KEEPALIVE_KEYS)
        if bad:
            raise InvalidAgent(f"KeepAlive does not accept {', '.join(bad)}")
    elif keep_alive is not None and not isinstance(keep_alive, bool):
        raise InvalidAgent("KeepAlive must be a boolean or a dictionary")


LABEL_SERVICE = "com.ragtools.service"
LABEL_TRAY = "com.ragtools.tray"


class DarwinAdapter:
    """macOS implementation of :class:`~ragtools.platform.base.PlatformAdapter`."""

    name = "darwin"

    def __init__(
        self,
        runner: Optional[CommandRunner] = None,
        *,
        home: Optional[Path] = None,
    ):
        self._run = runner or default_runner
        self._home = Path(home) if home else Path.home()

    # --- paths ----------------------------------------------------------

    def app_dir(self) -> Path:
        return self._home / "Library" / "Application Support" / "RAGTools"

    def dev_dir(self) -> Path:
        return self._home / "Library" / "Application Support" / "RAGTools-dev"

    @property
    def agents_dir(self) -> Path:
        return self._home / "Library" / "LaunchAgents"

    # --- process --------------------------------------------------------

    def spawn_detached(self, argv: Sequence[str], **kwargs) -> int:
        import subprocess

        kwargs.setdefault("start_new_session", True)
        kwargs.setdefault("close_fds", True)
        return subprocess.Popen(list(argv), **kwargs).pid

    def pid_alive(self, pid: int) -> bool:
        """Whether ``pid`` is a RUNNING process.

        Same zombie problem as Linux — an exited-but-unreaped process answers
        ``os.kill(pid, 0)`` — but macOS has no ``/proc``, so the state letter
        comes from ``ps``.
        """
        if pid <= 0:
            return False
        state = self._run(["ps", "-o", "state=", "-p", str(pid)])
        if state.ok and state.stdout.strip():
            return not state.stdout.strip().startswith("Z")
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def terminate(self, pid: int, force: bool = False) -> bool:
        import signal

        if pid <= 0:
            return False
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    # --- service / tray autostart ---------------------------------------

    def supports_autostart(self) -> bool:
        return True

    def _label(self, kind: str) -> str:
        return LABEL_TRAY if kind == KIND_TRAY else LABEL_SERVICE

    @staticmethod
    def _uid() -> int:
        """Current uid, or 0 off-platform.

        `os.getuid` does not exist on Windows, and the plist renderer and the
        registration inspector must both stay callable from a Windows build or
        test host — otherwise macOS packaging can only ever be verified on a Mac.
        """
        getuid = getattr(os, "getuid", None)
        return getuid() if callable(getuid) else 0

    def install_autostart(self, spec: AutostartSpec) -> Registration:
        label = self._label(spec.kind)
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        path = self.agents_dir / f"{label}.plist"
        document = self.render_plist(spec)
        validate_plist(document)      # never write one launchd would mis-read
        path.write_bytes(plistlib.dumps(document))

        # bootout first: launchd keeps the OLD definition loaded otherwise, so
        # an upgrade would keep running the previous binary until next login.
        uid = self._uid()
        self._run(["launchctl", "bootout", f"gui/{uid}/{label}"])
        result = self._run(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
        if not result.ok:
            # Older macOS spells this `load -w`; fall back before failing.
            result = self._run(["launchctl", "load", "-w", str(path)])
        if not result.ok:
            raise RuntimeError(
                f"failed to load {label}: {result.stderr.strip() or result.stdout.strip()}"
            )
        return Registration(
            name=label, kind=spec.kind, mechanism="launchd",
            target=" ".join(spec.argv), enabled=True, path=path,
        )

    def render_plist(self, spec: AutostartSpec) -> dict:
        """The agent definition. Separated so tests assert it without launchd."""
        plist: dict = {
            "Label": self._label(spec.kind),
            "ProgramArguments": list(spec.argv),
            "RunAtLoad": True,
            # Restart on crash, but respect a clean exit — otherwise `rag
            # service stop` becomes a fight with launchd.
            "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Background",
        }
        if spec.delay_seconds:
            plist["ThrottleInterval"] = int(spec.delay_seconds)
        if spec.working_dir:
            plist["WorkingDirectory"] = Path(spec.working_dir).as_posix()
        if spec.environment:
            plist["EnvironmentVariables"] = dict(spec.environment)
        return plist

    def remove_autostart(self, name: str) -> list[Registration]:
        removed: list[Registration] = []
        uid = self._uid()
        for existing in self.find_autostart(KIND_SERVICE) + self.find_autostart(KIND_TRAY):
            if existing.name != name:
                continue
            self._run(["launchctl", "bootout", f"gui/{uid}/{existing.name}"])
            if existing.path is not None:
                try:
                    existing.path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    continue
            removed.append(existing)
        return removed

    def find_autostart(self, kind: str = KIND_SERVICE) -> list[Registration]:
        found: list[Registration] = []
        label = self._label(kind)
        path = self.agents_dir / f"{label}.plist"
        if path.exists():
            probe = self._run(["launchctl", "list", label])
            found.append(Registration(
                name=label, kind=kind, mechanism="launchd",
                target=_plist_program(path), enabled=probe.ok, path=path,
            ))
        return found

    # --- desktop --------------------------------------------------------

    def has_desktop_session(self) -> bool:
        """A GUI session has an Aqua security session. SSH into a Mac does not,
        and a tray there would fail at launch rather than simply not appear."""
        probe = self._run(["launchctl", "managername"])
        if probe.ok and probe.stdout.strip():
            return probe.stdout.strip() == "Aqua"
        # If we cannot ask, assume a session: a Mac desktop is the common case
        # and the tray degrades visibly rather than dangerously.
        return True

    def open_url(self, url: str) -> bool:
        return self._run(["open", url]).ok

    def open_path(self, path: Path) -> bool:
        return self._run(["open", str(path)]).ok

    def copy_text(self, text: str) -> bool:
        import subprocess

        try:
            proc = subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=5)
            return proc.returncode == 0
        except Exception:  # noqa: BLE001
            return False


def _plist_program(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
        return " ".join(data.get("ProgramArguments", []))
    except Exception:  # noqa: BLE001
        return ""
