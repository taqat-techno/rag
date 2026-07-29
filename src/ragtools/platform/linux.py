"""Linux adapter — systemd user units and XDG autostart.

**User units, not system units.** The product indexes the logged-in user's own
projects into a per-user data directory; a system unit runs as another account
and cannot see `$HOME`. `systemd --user` is the mechanism that matches what this
product actually is.

**Lingering is the headless correctness detail.** Without
``loginctl enable-linger``, a user manager — and therefore the service — stops
when the last session ends. On a headless box reached by SSH that means the
index quietly stops updating the moment you log out. :meth:`linger_enabled`
exposes it so install and health can both state it rather than let the user find
out days later.

Restart-on-failure is ``Restart=on-failure`` in the unit. That is the whole
reason the 462-line Windows watchdog has no Linux counterpart: this is a
first-class capability of the init system, not something a product should
reimplement with a polling timer.
"""

from __future__ import annotations

import os
import shutil
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

UNIT_SERVICE = "ragtools.service"
UNIT_TRAY = "ragtools-tray.service"
DESKTOP_TRAY = "ragtools-tray.desktop"


class LinuxAdapter:
    """Linux implementation of :class:`~ragtools.platform.base.PlatformAdapter`."""

    name = "linux"

    def __init__(
        self,
        runner: Optional[CommandRunner] = None,
        *,
        home: Optional[Path] = None,
        xdg_data_home: Optional[Path] = None,
        xdg_config_home: Optional[Path] = None,
    ):
        self._run = runner or default_runner
        self._home = Path(home) if home else Path.home()
        self._data_home = Path(xdg_data_home) if xdg_data_home else self._resolve_xdg(
            "XDG_DATA_HOME", self._home / ".local" / "share")
        self._config_home = Path(xdg_config_home) if xdg_config_home else self._resolve_xdg(
            "XDG_CONFIG_HOME", self._home / ".config")

    def _resolve_xdg(self, var: str, fallback: Path) -> Path:
        raw = os.environ.get(var, "").strip()
        return Path(raw) if raw else fallback

    # --- paths ----------------------------------------------------------

    def app_dir(self) -> Path:
        # Name matches the pre-refactor path exactly. Renaming to the more
        # idiomatic lowercase XDG form is a deliberate product decision, not
        # something to slip into a mechanical move.
        return self._data_home / "RAGTools"

    def dev_dir(self) -> Path:
        return self._data_home / "RAGTools-dev"

    @property
    def unit_dir(self) -> Path:
        return self._config_home / "systemd" / "user"

    @property
    def autostart_dir(self) -> Path:
        return self._config_home / "autostart"

    # --- process --------------------------------------------------------

    def spawn_detached(self, argv: Sequence[str], **kwargs) -> int:
        import subprocess

        # New session: the child survives the parent's terminal closing, which
        # is what "detached" has to mean for a service launcher.
        kwargs.setdefault("start_new_session", True)
        kwargs.setdefault("close_fds", True)
        return subprocess.Popen(list(argv), **kwargs).pid


    #: No windowless variant exists; ELF has no console-vs-GUI subsystem split.
    windowed_executable_name = None

    #: No OS-wide package database this product installs itself into.
    records_installed_version = False

    def recorded_version(self):
        """systemd keeps no per-application version record this product
        writes, so there is nothing to disagree with. None means "no record",
        never "agrees"."""
        return None

    def owned_processes(self):
        """Not enumerated here. Returning None rather than an empty list is
        deliberate: an empty list would let a caller conclude "no stray
        processes" from a check that never ran."""
        return None

    def background_executable(self, executable: str) -> str:
        """Unchanged — ELF has no console-vs-GUI subsystem split.

        A systemd unit inherits the journal, not a terminal, so there is no
        window to suppress and nothing to choose between. Present so the
        decision has one shape on every platform and callers never branch.
        """
        return executable

    def child_process_flags(self) -> dict:
        """Nothing extra. POSIX has no console to suppress.

        `PlatformAdapter` is a Protocol, so the concrete adapters conform
        structurally rather than inheriting — a default on the Protocol is
        invisible at runtime, which is how the first version of this shipped an
        `AttributeError` on Linux that only a real Linux runner could catch.
        """
        return {}

    def pid_alive(self, pid: int) -> bool:
        """Whether ``pid`` is a RUNNING process.

        A zombie — exited but not yet reaped by its parent — still answers
        ``os.kill(pid, 0)``, so the obvious check reports a dead service as
        alive and the stale PID file is never cleaned. `/proc` carries the
        process state, and `Z` is the answer that matters.
        """
        if pid <= 0:
            return False
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            stat = ""
        if stat:
            # `comm` may contain spaces and parentheses; state is the field
            # immediately after the closing paren.
            tail = stat.rpartition(")")[2].strip()
            return bool(tail) and tail.split()[0] != "Z"
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True          # exists, not ours — alive for our purposes
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
        return shutil.which("systemctl") is not None

    def _unit_name(self, kind: str) -> str:
        return UNIT_TRAY if kind == KIND_TRAY else UNIT_SERVICE

    def install_autostart(self, spec: AutostartSpec) -> Registration:
        unit = self._unit_name(spec.kind)
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        path = self.unit_dir / unit
        path.write_text(self.render_unit(spec), encoding="utf-8")

        # daemon-reload before enable, or systemd enables the previous content.
        self._run(["systemctl", "--user", "daemon-reload"])
        result = self._run(["systemctl", "--user", "enable", unit])
        if not result.ok:
            raise RuntimeError(
                f"failed to enable {unit}: {result.stderr.strip() or result.stdout.strip()}"
            )
        return Registration(
            name=unit, kind=spec.kind, mechanism="systemd-user",
            target=" ".join(spec.argv), enabled=True, path=path,
        )

    def render_unit(self, spec: AutostartSpec) -> str:
        """The unit file. Separated so tests assert content without systemd."""
        exec_start = " ".join(_quote(a) for a in spec.argv)
        lines = [
            "[Unit]",
            f"Description={spec.description or spec.name}",
            # The service owns its own storage supervision; ordering after the
            # network is enough, and Wants= would fail installs on hosts with
            # no network target.
            "After=network.target",
            # StartLimit* live in [Unit], NOT [Service]. systemd-analyze on real
            # systemd reports "Unknown key name 'StartLimitIntervalSec' in
            # section 'Service', ignoring" — so placing them below silently
            # discards the crash-loop protection and the unit restarts forever.
            "StartLimitBurst=5",
            "StartLimitIntervalSec=300",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={exec_start}",
            "Restart=on-failure",
            "RestartSec=5",
        ]
        if spec.delay_seconds:
            lines.append(f"ExecStartPre=/bin/sleep {int(spec.delay_seconds)}")
        if spec.working_dir:
            # POSIX separators even when rendered from a Windows build host.
            lines.append(f"WorkingDirectory={Path(spec.working_dir).as_posix()}")
        for key, value in sorted(spec.environment.items()):
            lines.append(f'Environment="{key}={value}"')
        lines += ["", "[Install]", "WantedBy=default.target", ""]
        return "\n".join(lines)

    def remove_autostart(self, name: str) -> list[Registration]:
        removed: list[Registration] = []
        for existing in self.find_autostart(KIND_SERVICE) + self.find_autostart(KIND_TRAY):
            if existing.name != name:
                continue
            if existing.mechanism == "systemd-user":
                self._run(["systemctl", "--user", "disable", "--now", existing.name])
            if existing.path is not None:
                try:
                    existing.path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    continue
            removed.append(existing)
        if removed:
            self._run(["systemctl", "--user", "daemon-reload"])
        return removed

    def find_autostart(self, kind: str = KIND_SERVICE) -> list[Registration]:
        found: list[Registration] = []
        unit = self._unit_name(kind)
        unit_path = self.unit_dir / unit
        if unit_path.exists():
            probe = self._run(["systemctl", "--user", "is-enabled", unit])
            found.append(Registration(
                name=unit, kind=kind, mechanism="systemd-user",
                target=_unit_exec(unit_path), enabled=probe.ok, path=unit_path,
            ))
        if kind == KIND_TRAY:
            desktop = self.autostart_dir / DESKTOP_TRAY
            if desktop.exists():
                found.append(Registration(
                    name=DESKTOP_TRAY, kind=kind, mechanism="xdg-autostart",
                    target=str(desktop), enabled=True, path=desktop,
                ))
        return found

    # --- headless / lingering -------------------------------------------

    def linger_enabled(self, user: Optional[str] = None) -> bool:
        """Whether the user manager survives logout.

        False on a headless box means the service stops when the SSH session
        ends — a silent stop that looks like "indexing randomly broke".
        """
        who = user or os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        if not who:
            return False
        probe = self._run(["loginctl", "show-user", who, "--property=Linger"])
        return probe.ok and "Linger=yes" in probe.stdout

    def enable_linger(self, user: Optional[str] = None) -> bool:
        who = user or os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        if not who:
            return False
        return self._run(["loginctl", "enable-linger", who]).ok

    # --- desktop --------------------------------------------------------

    def has_desktop_session(self) -> bool:
        """A tray needs a display. Wayland and X11 both advertise themselves."""
        return bool(
            os.environ.get("DISPLAY", "").strip()
            or os.environ.get("WAYLAND_DISPLAY", "").strip()
        )

    def open_url(self, url: str) -> bool:
        return self._open_with_handler(url)

    def open_path(self, path: Path) -> bool:
        return self._open_with_handler(str(path))

    def _open_with_handler(self, target: str) -> bool:
        for tool in ("xdg-open", "gio", "gnome-open", "kde-open"):
            if shutil.which(tool) is None:
                continue
            args = [tool, "open", target] if tool == "gio" else [tool, target]
            if self._run(args).ok:
                return True
        return False

    def copy_text(self, text: str) -> bool:
        import subprocess

        # wl-copy first: on a Wayland session xclip may exist but do nothing.
        for argv in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]):
            if shutil.which(argv[0]) is None:
                continue
            try:
                proc = subprocess.run(argv, input=text.encode("utf-8"), timeout=5)
                if proc.returncode == 0:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


def _unit_exec(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecStart="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""
