"""Platform-adapter contracts — the seam every OS difference lives behind.

Before this package, thirteen modules branched on ``sys.platform`` and three of
them (service autostart, tray autostart, the 462-line watchdog) had **no**
non-Windows path at all. That is not a product with adaptations pending; it is a
Windows product. The contracts here are what a second and third platform plug
into, so "Windows, Linux and macOS are first-class" becomes a structural fact
rather than an intention.

Two contracts carry most of the weight:

* :class:`Registration` — one autostart or schedule entry **found on the
  machine**, including ones created by superseded versions. An upgrade cannot
  remove what it cannot enumerate, and the machine this was written on carries a
  scheduled task, two Startup-folder scripts and sixteen duplicate PATH entries
  that no code could see.
* :meth:`PlatformAdapter.find_autostart` — deliberately returns a *list*. The
  invariant the product needs is "exactly one", and you cannot assert that with
  an API that can only answer "is there at least one".

Every adapter takes injectable roots and an injectable command runner, so the
suite exercises real logic against temp directories and never registers a task,
writes a Startup-folder entry, or touches a live service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence, runtime_checkable

#: A command runner: argv -> (returncode, stdout, stderr). Injected so tests
#: never shell out and production has exactly one place that does.
CommandRunner = Callable[[Sequence[str]], "CommandResult"]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


#: What an autostart entry is for. `service` and `tray` are separate concerns
#: with separate lifetimes: a headless machine has the first and not the second.
KIND_SERVICE = "service"
KIND_TRAY = "tray"
KIND_SCHEDULE = "schedule"


@dataclass(frozen=True)
class Registration:
    """One autostart/schedule entry that exists on this machine.

    ``legacy`` marks an entry created by a superseded version — a Startup-folder
    VBS, the retired watchdog task. The upgrade removes those; it must never
    remove an entry it does not recognise, because that is how a user's own
    automation gets deleted by a product upgrade.
    """

    name: str
    kind: str
    mechanism: str
    target: str
    enabled: bool = True
    legacy: bool = False
    #: Where it lives, when the mechanism is file-backed (Startup folder, unit
    #: file, plist, .desktop). Empty for registry/scheduler-backed entries.
    path: Optional[Path] = None

    def describe(self) -> str:
        state = "enabled" if self.enabled else "disabled"
        tag = " [legacy]" if self.legacy else ""
        return f"{self.name} ({self.mechanism}, {state}){tag}"


@dataclass(frozen=True)
class AutostartSpec:
    """What to register. Platform-neutral on purpose.

    ``argv`` is a full command line rather than a script path: the Windows
    implementation used to write a ``.vbs`` shim purely to avoid a console
    window, and an interpreted shim in the startup path is its own defect class.

    That much held. The reasoning printed here until v3.0.1 did not: it said
    hiding a console "is a launcher problem solved properly by process creation
    flags", meaning ``CREATE_NO_WINDOW``. That flag is a ``subprocess.Popen``
    argument, so it governs processes **ragtools spawns** — and the process at
    issue is spawned by the OS. Task Scheduler builds it from this spec's
    ``argv`` and never sees a Python creation flag; no task-XML setting
    suppresses a console either. v3.0.0 therefore shipped autostart that opened
    a terminal window at every login, on the strength of a comment.

    ``argv[0]`` is resolved through :meth:`PlatformAdapter.background_executable`
    for exactly this reason — the answer is "which image should the OS start",
    and only the platform knows.
    """

    name: str
    kind: str
    argv: Sequence[str]
    #: Human-readable, shown in the OS's own UI where it has one.
    description: str = ""
    working_dir: Optional[Path] = None
    environment: dict = field(default_factory=dict)
    #: Seconds to wait after login before starting. Lets the machine settle.
    delay_seconds: int = 0


class PlatformUnsupported(RuntimeError):
    """This platform has no adapter.

    Raised rather than falling back to a "probably fine" default. The managed
    Qdrant asset matrix already refuses unknown platform/arch pairs instead of
    guessing an asset name, and the same rule applies here: a wrong guess about
    how a machine starts services is discovered at reboot, in the field.
    """


@runtime_checkable
class PlatformAdapter(Protocol):
    """Everything the product needs to know about the OS it is running on."""

    #: ``windows`` | ``linux`` | ``darwin``
    name: str

    # --- paths ---------------------------------------------------------
    def app_dir(self) -> Path:
        """Per-user application data root for the installed profile."""
        ...

    def dev_dir(self) -> Path:
        """Per-user data root for the *development* profile. Never the same as
        :meth:`app_dir` — sharing them is what lets a dev run corrupt a live
        index."""
        ...

    # --- process -------------------------------------------------------
    def spawn_detached(self, argv: Sequence[str], **kwargs) -> int:
        """Start a process that outlives this one, with no console window."""
        ...

    #: Filename of the windowless sibling this platform ships, or ``None`` where
    #: the concept does not exist. Declarative rather than a method because it is
    #: a fact about the build, and a caller asking "should there be one?" must
    #: not have to branch on the OS to find out.
    windowed_executable_name: Optional[str]

    #: Whether this platform keeps a package database recording installed
    #: versions at all. Declarative for the same reason as
    #: :attr:`windowed_executable_name`: it is a fact about the OS, and a caller
    #: deciding whether "no version found" is a finding or a non-question must
    #: not have to branch on ``sys.platform`` to answer it.
    records_installed_version: bool

    def recorded_version(self) -> Optional[str]:
        """The version the OS's own package database says is installed.

        Distinct from ``ragtools.__version__``, which is what this *process* is.
        When they disagree, an upgrade replaced files without updating the
        record the rest of the system reads — Add/Remove Programs, winget and
        the installer's own upgrade detection all consult it.

        ``None`` means **no installation is recorded**. It does NOT mean "this
        platform has no registry": that question is answered by
        :attr:`records_installed_version`, and conflating the two is a real
        defect rather than a hypothetical one. A single ``Optional[str]`` made
        a clean Windows runner (registry present, nothing installed)
        indistinguishable from Linux (no registry at all), so a check written to
        prove the registry is genuinely read passed on the author's machine —
        where the product happened to be installed — and failed in CI. The
        three-state split is the same one :meth:`owned_processes` already draws
        between "looked, found none" and "could not look".
        """
        ...

    def owned_processes(self) -> Optional[list[tuple[int, str, str]]]:
        """``(pid, image, executable path)`` for every running process this
        product owns, or ``None`` when the platform cannot be enumerated.

        The distinction matters: an empty list means "looked, found none" and
        ``None`` means "could not look". Collapsing them would let a machine
        report a clean installation precisely because nothing could be checked.
        """
        ...

    def background_executable(self, executable: str) -> str:
        """Which image the **OS itself** should launch for a windowless run.

        Distinct from :meth:`spawn_detached`, and the distinction is the whole
        point: that method hides a window for a process *we* create, and can
        pass a creation flag to do it. Nothing we can pass reaches a process the
        OS creates from a scheduler entry, so on Windows the only lever left is
        which executable the entry names — a console-subsystem image gets a
        console, and a GUI-subsystem one does not.

        Unix has no such split; those adapters return ``executable`` unchanged.
        Callers must treat a passthrough as normal rather than as a failure.
        """
        ...

    def pid_alive(self, pid: int) -> bool:
        """Whether ``pid`` is a live process. Must not raise for a dead pid."""
        ...

    def terminate(self, pid: int, force: bool = False) -> bool:
        """Ask ``pid`` to exit. ``force`` escalates."""
        ...

    # --- service autostart (machine boot / user login) ------------------
    def supports_autostart(self) -> bool: ...

    def install_autostart(self, spec: AutostartSpec) -> Registration: ...

    def remove_autostart(self, name: str) -> list[Registration]:
        """Remove and return what was removed. Idempotent: removing nothing is
        success, so an interrupted upgrade can re-run."""
        ...

    def find_autostart(self, kind: str = KIND_SERVICE) -> list[Registration]:
        """EVERY registration for ``kind``, including legacy mechanisms.

        A list, not an Optional: "exactly one" is the invariant, and duplicates
        are the observed failure — this machine carries a scheduled task *and*
        two Startup-folder scripts for one product.
        """
        ...

    # --- desktop session ------------------------------------------------
    def has_desktop_session(self) -> bool:
        """Whether a tray icon could exist. False on a headless box, which must
        remain fully supported without one."""
        ...

    def open_url(self, url: str) -> bool: ...

    def open_path(self, path: Path) -> bool: ...

    def copy_text(self, text: str) -> bool: ...


def default_runner(argv: Sequence[str]) -> CommandResult:
    """Run a command, capturing output, never raising, never showing a window."""
    import subprocess
    import sys

    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "timeout": 30,
    }
    if sys.platform == "win32":
        # 0x08000000 = CREATE_NO_WINDOW. Without it every scheduler query
        # flashes a console — the defect that made the old watchdog visible
        # to users as a blinking black box.
        kwargs["creationflags"] = 0x08000000
    try:
        proc = subprocess.run(list(argv), **kwargs)
        return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")
    except Exception as exc:  # noqa: BLE001 — a missing tool is an answer, not a crash
        return CommandResult(127, "", str(exc))
