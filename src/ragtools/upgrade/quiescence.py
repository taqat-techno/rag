"""Prove an installation can be replaced — BEFORE the installer writes anything.

The failure this exists to make impossible, reproduced on a GitHub-hosted
``windows-latest`` runner (job 91273392455), genuine packaged v3.3.0 → candidate::

    >>> installing v3.3.0 ... exit=0
      [PASS] previous release is serving - managed engine pid 1264
      [PASS] previous release has live processes holding its binaries - 3
    >>> installing the candidate over it ... exit=5
    --- Inno log ... (last 120 of 1 lines) ---        <- Setup died before its log
      [FAIL] rag.exe reports the new version - [PYI-2072] Failed to load Python
             DLL '...\\Programs\\RAGTools\\_internal\\python312.dll'
      [FAIL] uninstall registry entry updated - registry reads 3.3.0
      [FAIL] the service answers after the upgrade - nothing within 300s

The mechanism, pinned by reading ``installer.iss``: ``CloseApplications=no``, so
Restart Manager is not in play; ``CurStepChanged(ssInstall)`` stops and kills;
``[InstallDelete]`` then removes ``{app}\\_internal`` wholesale, and *that* is
the first destructive write. If anything still holds ``_internal\\python312.dll``
the delete raises Inno's file-in-use Abort/Retry/Ignore box, ``/SUPPRESSMSGBOXES``
answers **Abort**, and Setup exits **5** with ``_internal`` half deleted — an
unloadable Python DLL, a registry still naming the old version, a mixed
directory.

So the defect is not the kill. It is that **the installer starts deleting before
it has proven the files are replaceable, and has no way to refuse.**

The invariant this module encodes:

    If any installation-owning process cannot be stopped, the upgrade aborts
    BEFORE its first destructive write and the old installation remains
    runnable.

Two implementations, one decision
---------------------------------
``installer/quiesce.ps1`` does the OS work, because PowerShell is present on
every Windows and needs nothing extracted from a bundle that is not yet
installed. This module holds the **policy** — which processes count as ours,
what counts as proof, when to retry, when to refuse — behind injectable ports so
it can be tested without a Windows host, a real installation, or a real lock.

The two must not drift, so the phase list and the exit-code mapping are declared
exactly once on each side and compared structurally by
``tests/test_installer_quiescence_contract.py``. A hand-maintained list that can
silently disagree is not a single source of truth; it is two.

What this module never does
---------------------------
It never deletes or overwrites anything under the installation directory. That
is not a convention, it is the acceptance criterion: a refusal has to leave the
old installation byte-consistent and runnable, and the cheapest way to guarantee
that is for the refusing code to have no destructive operation in it at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Protocol, Sequence

# --------------------------------------------------------------------------
# The protocol, named once
# --------------------------------------------------------------------------

PHASE_DETECT = "detect_installation"
PHASE_MAINTENANCE = "enter_maintenance"
PHASE_DISABLE_TASKS = "disable_tasks"
PHASE_STOP_TRAY = "stop_tray"
PHASE_STOP_SUPERVISOR = "stop_supervisor"
PHASE_STOP_SERVICE_CHILDREN = "stop_service_children"
PHASE_STOP_ENGINE = "stop_managed_engine"
PHASE_IDENTIFY_HOLDERS = "identify_holders"
PHASE_WAIT = "wait_for_exit"
PHASE_RETRY = "retry_safe_shutdown"
PHASE_VERIFY_CLEAR = "verify_clear"
PHASE_PROBE_REPLACEABLE = "probe_replaceable"

#: Every phase, in the order the protocol runs them. ``quiesce.ps1`` declares
#: the same tuple and executes it through one dispatcher, so neither side can
#: gain, lose or reorder a phase without the contract test noticing.
PHASES: tuple[str, ...] = (
    PHASE_DETECT,
    PHASE_MAINTENANCE,
    PHASE_DISABLE_TASKS,
    PHASE_STOP_TRAY,
    PHASE_STOP_SUPERVISOR,
    PHASE_STOP_SERVICE_CHILDREN,
    PHASE_STOP_ENGINE,
    PHASE_IDENTIFY_HOLDERS,
    PHASE_WAIT,
    PHASE_RETRY,
    PHASE_VERIFY_CLEAR,
    PHASE_PROBE_REPLACEABLE,
)

#: The one phase that runs AFTER the install, not before it. Disabling the
#: scheduled tasks is what closes the restart race; leaving them disabled would
#: be a silent regression of the user's autostart, so the same script restores
#: exactly what it disabled.
RESTORE_PHASE = "restore_task_state"

#: Phases whose shutdown request may be issued again during the retry loop.
#:
#: ``disable_tasks`` is DELIBERATELY absent. Re-running it is not dangerous to
#: the machine — it is dangerous to the *record*: the second pass would read the
#: prior state as ``Disabled`` (because the first pass disabled it) and overwrite
#: the only evidence of what to restore. A retry that destroys the rollback
#: information is a destructive step wearing a safe name.
RETRYABLE_PHASES: tuple[str, ...] = (
    PHASE_STOP_TRAY,
    PHASE_STOP_SUPERVISOR,
    PHASE_STOP_SERVICE_CHILDREN,
    PHASE_STOP_ENGINE,
)

#: Phases whose FAILURE could let the protocol wrongly conclude quiescence, and
#: which therefore abort it. Everything else is ADVISORY: it is recorded as an
#: error and the protocol carries on, because the decisive phases still have to
#: pass before anything proceeds.
#:
#: * :data:`PHASE_DETECT` decides whether there is anything to protect at all —
#:   an error read as "nothing installed" returns ``EXIT_QUIESCENT`` and lets
#:   Setup delete a *live* installation.
#: * :data:`PHASE_VERIFY_CLEAR` and :data:`PHASE_PROBE_REPLACEABLE` are the two
#:   that actually prove the answer.
#:
#: This is not a refinement, it is the 3.5.1 defect. ``disable_tasks`` raised on
#: a scheduled task that was merely not registered, and the whole protocol
#: aborted *before* ``stop_tray`` — no tray stop, no supervisor stop, no
#: ``taskkill``, no engine stop. Every upgrade leg refused with the previous
#: version still serving and all of its processes alive, and the refusal named
#: an exception where a blocker belonged. A preparatory step failing must never
#: be able to skip every step that does the work.
DECISIVE_PHASES: tuple[str, ...] = (
    PHASE_DETECT,
    PHASE_VERIFY_CLEAR,
    PHASE_PROBE_REPLACEABLE,
)

#: The complement, stated rather than derived at each site.
ADVISORY_PHASES: tuple[str, ...] = tuple(p for p in PHASES if p not in DECISIVE_PHASES)

#: Exit codes, keyed by the verdict they carry.
#:
#: ``0`` and ``2`` are the whole point of moving the protocol into
#: ``PrepareToInstall()``: Inno turns a non-empty return from that function into
#: exit **7** — "Setup cannot proceed" — WITHOUT touching a file, which finally
#: makes "refused safely, your old install is intact" distinguishable from
#: "aborted mid-write, your install is now mixed" (exit 5). ``1`` is left unused
#: on purpose: an unhandled PowerShell error exits 1, and that must not be
#: mistakable for a considered verdict.
EXIT_CODES: Mapping[str, int] = {
    "quiescent": 0,
    "not_quiescent": 2,
    "internal_error": 3,
}

EXIT_QUIESCENT = EXIT_CODES["quiescent"]
EXIT_NOT_QUIESCENT = EXIT_CODES["not_quiescent"]
EXIT_INTERNAL_ERROR = EXIT_CODES["internal_error"]

OUTCOME_OK = "ok"
OUTCOME_SKIPPED = "skipped"
OUTCOME_BLOCKED = "blocked"
OUTCOME_ERROR = "error"

ROLE_TRAY = "tray"
ROLE_SUPERVISOR = "supervisor"
ROLE_SERVICE = "service"
ROLE_ENGINE = "engine"
#: A process that is none of the above and would be invisible to an image-name
#: filter — an MCP client, an editor plugin, a stray ``python.exe`` — but has
#: mapped a DLL out of the installation and holds it exactly as hard.
ROLE_MODULE_HOLDER = "module_holder"

#: Which phase is responsible for stopping which role.
ROLE_PHASE: Mapping[str, str] = {
    ROLE_TRAY: PHASE_STOP_TRAY,
    ROLE_SUPERVISOR: PHASE_STOP_SUPERVISOR,
    ROLE_SERVICE: PHASE_STOP_SERVICE_CHILDREN,
    ROLE_ENGINE: PHASE_STOP_ENGINE,
}
PHASE_ROLE: Mapping[str, str] = {phase: role for role, phase in ROLE_PHASE.items()}

EVIDENCE_IMAGE_NAME = "owned_image_name"
EVIDENCE_EXECUTABLE_PATH = "executable_under_installation"
EVIDENCE_LOADED_MODULE = "loaded_module_under_installation"

#: Names this product owns outright — nothing else on a Windows machine ships
#: them — so matching them by image name is safe AND proven: v3.0.1 replaced a
#: running ``rag.exe`` in place on a real machine using exactly that. A
#: path-scoped equivalent was tried in its place and the upgrade failed with
#: ``DeleteFile failed; code 5``, with owned processes still alive afterwards.
OWNED_IMAGE_NAMES: tuple[str, ...] = ("rag.exe", "ragw.exe")

#: The opposite case, and the reason ownership is never decided by name alone.
#: ``qdrant.exe`` is a common name and ``storage_backend = "external"``
#: explicitly means "a server you run yourself"; stopping every Qdrant on the
#: machine would be data loss in someone else's application caused by installing
#: this one. It counts as ours only when its executable lives under the
#: installation or its data directory.
PATH_SCOPED_IMAGE_NAMES: tuple[str, ...] = ("qdrant.exe",)

#: File kinds Windows keeps mapped and therefore refuses to replace while a
#: handle survives. These are what ``[InstallDelete]`` and ``[Files]`` are about
#: to touch, so these are what must be proven replaceable first.
REPLACEABLE_SUFFIXES: tuple[str, ...] = (".exe", ".dll", ".pyd")

#: Scheduled tasks an installation owns. ``\RAGTools\Service`` and
#: ``\RAGTools\Tray`` are this design's registrations; the third is v2's, still
#: present on every machine that ever ran one.
OWNED_TASKS: tuple[str, ...] = (
    r"\RAGTools\Service",
    r"\RAGTools\Tray",
    r"\RAGTools Watchdog",
)

BLOCKER_PROCESS = "process"
BLOCKER_FILE = "file"

REASON_QUIESCENT = "quiescent"
REASON_NO_PREVIOUS_INSTALLATION = "no_previous_installation"
REASON_PROCESSES_SURVIVED = "processes_survived"
REASON_FILES_NOT_REPLACEABLE = "files_not_replaceable"
REASON_TIMEOUT = "timeout"
REASON_INTERNAL_ERROR = "internal_error"


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Installation:
    """A previous installation, as found on disk and in the registry."""

    present: bool
    version: str = ""
    executable: str = ""
    app_dir: str = ""

    def describe(self) -> str:
        if not self.present:
            return "no previous installation"
        return f"v{self.version or 'unknown'} at {self.app_dir or self.executable}"


@dataclass(frozen=True)
class ProcessInfo:
    """One process, as an enumerator can describe it.

    ``modules`` is the part an image-name filter cannot supply and the part the
    reproduced failure turned on: a process with an unrelated name that has
    mapped ``_internal\\python312.dll`` holds the lock exactly as hard as
    ``rag.exe`` does.

    ``modules_readable`` records an access denial rather than dropping the
    process. A process we could not inspect is not evidence of quiescence — but
    neither is it, on its own, a blocker, or every upgrade would refuse on
    ``System``. The replaceability probe is what decides; this is what explains.
    """

    pid: int
    name: str
    executable: Optional[str] = None
    command_line: str = ""
    modules: tuple[str, ...] = ()
    modules_readable: bool = True
    #: Enumerator's hint. When absent the role is derived from the command line.
    role: Optional[str] = None


@dataclass(frozen=True)
class Blocker:
    """Something that would make the next write fail, named so it can be closed."""

    kind: str
    identity: str
    detail: str
    pid: Optional[int] = None
    path: Optional[str] = None

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "identity": self.identity,
            "detail": self.detail,
            "pid": self.pid,
            "path": self.path,
        }


@dataclass(frozen=True)
class TaskRecord:
    """A scheduled task's state before the upgrade touched it."""

    name: str
    prior_state: str
    disabled_by_us: bool

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "prior_state": self.prior_state,
            "disabled_by_us": self.disabled_by_us,
        }


@dataclass
class PhaseRecord:
    phase: str
    outcome: str
    detail: str = ""
    duration_ms: int = 0
    blockers: tuple[Blocker, ...] = ()

    def to_json(self) -> dict:
        return {
            "phase": self.phase,
            "outcome": self.outcome,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            "blockers": [b.to_json() for b in self.blockers],
        }


@dataclass
class Verdict:
    """The whole answer: what happened, what is in the way, and what to return."""

    exit_code: int
    quiescent: bool
    reason: str
    app_dir: str = ""
    data_dir: str = ""
    installation: Optional[Installation] = None
    phases: list[PhaseRecord] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    tasks: list[TaskRecord] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def disabled_tasks(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.tasks if t.disabled_by_us)

    def phase(self, name: str) -> Optional[PhaseRecord]:
        for record in self.phases:
            if record.phase == name:
                return record
        return None

    def to_json(self) -> dict:
        """The verdict shape ``quiesce.ps1`` also emits, key for key."""
        return {
            "schema": SCHEMA,
            "mode": "quiesce",
            "exit_code": self.exit_code,
            "quiescent": self.quiescent,
            "reason": self.reason,
            "app_dir": self.app_dir,
            "data_dir": self.data_dir,
            "installation": {
                "present": bool(self.installation and self.installation.present),
                "version": self.installation.version if self.installation else "",
                "executable": self.installation.executable if self.installation else "",
            },
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "phases": [p.to_json() for p in self.phases],
            "blockers": [b.to_json() for b in self.blockers],
            "tasks": [t.to_json() for t in self.tasks],
        }

    def summary_lines(self) -> list[str]:
        """What the installer shows the user — blockers by path and PID.

        "Close the application and try again" is actionable. "Restart Windows"
        is what an installer says when it does not know, and it is what this
        product used to say for every cause including the ones a reboot cannot
        fix.
        """
        if self.quiescent:
            return ["RAG Tools can be upgraded: nothing is holding its files."]
        lines = [
            "RAG Tools cannot be upgraded safely right now, so nothing has been "
            "changed and your current installation is still intact.",
            "",
            "Still holding the files that must be replaced:",
        ]
        for blocker in self.blockers:
            lines.append(f"  - {blocker.identity}: {blocker.detail}")
        if not self.blockers:
            lines.append(f"  - (no individual blocker recorded; reason: {self.reason})")
        return lines


SCHEMA = "ragtools.upgrade.quiescence/1"

#: Top-level keys of the emitted verdict. Declared so the contract test can
#: compare against the PowerShell document without re-listing them by hand.
VERDICT_KEYS: tuple[str, ...] = (
    "schema", "mode", "exit_code", "quiescent", "reason", "app_dir", "data_dir",
    "installation", "elapsed_seconds", "phases", "blockers", "tasks",
)


@dataclass(frozen=True)
class QuiescenceConfig:
    app_dir: str
    data_dir: str
    timeout_seconds: float = 120.0
    #: Bounded, and short. Every interval is time an upgrade spends doing
    #: nothing, and the failure it guards against — the scheduler restarting
    #: what we just killed — is already closed by disabling the task.
    retry_backoff: tuple[float, ...] = (1.0, 3.0, 9.0)
    #: `taskkill` returns once the signal is delivered, not once the kernel has
    #: released the image's file handle. The copy that follows fails on exactly
    #: that gap.
    settle_seconds: float = 2.5
    #: How many settle rounds the wait phase spends before handing over to the
    #: retry loop. Bounded independently of `timeout_seconds`, so "this process
    #: is refusing to die" and "the whole protocol ran out of time" stay
    #: distinguishable — they have different remedies and the user is told which
    #: one happened.
    settle_rounds: int = 3


# --------------------------------------------------------------------------
# Ports — everything that touches the operating system
# --------------------------------------------------------------------------


class InstallationPort(Protocol):
    """The previous installation, asked politely before it is asked forcefully."""

    def detect(self) -> Optional[Installation]:
        ...

    def enter_maintenance(self) -> bool:
        """Best effort. A release that predates the state returns False, and
        that is not a failure — most of the versions this runs against were
        published before this protocol existed."""

    def request_stop(self, role: str) -> bool:
        """Ask a role to shut down gracefully. Returns whether the REQUEST was
        accepted, never whether the process died — that is decided by looking."""


class ProcessPort(Protocol):
    def list_processes(self) -> Sequence[ProcessInfo]:
        ...

    def stop(self, pid: int, *, force: bool = False) -> bool:
        """Issue a stop. The return value is the request's fate, not the
        process's; nothing here believes a process is gone until it is absent
        from :meth:`list_processes`."""


class TaskPort(Protocol):
    def states(self) -> Mapping[str, str]:
        """Task name -> state (``Ready`` / ``Running`` / ``Disabled`` / ``Missing``)."""

    def disable(self, name: str) -> bool:
        ...

    def enable(self, name: str) -> bool:
        ...


class LockPort(Protocol):
    def replaceable_candidates(self) -> Sequence[str]:
        """Every file the installer is about to delete or overwrite."""

    def can_replace(self, path: str) -> tuple[bool, str]:
        """``(ok, reason)``. Proven by opening the file exclusively, never by
        having seen no process holding it — enumeration can come back clean
        while a handle survives, and that is the failure this exists for."""


class ClockPort(Protocol):
    def now(self) -> float:
        ...

    def sleep(self, seconds: float) -> None:
        ...


@dataclass
class Ports:
    installation: InstallationPort
    processes: ProcessPort
    tasks: TaskPort
    locks: LockPort
    clock: ClockPort


# --------------------------------------------------------------------------
# Ownership — the decision that must never be made by image name alone
# --------------------------------------------------------------------------


def _norm(path: Optional[str]) -> str:
    """Comparable form of a path.

    Case-insensitive and separator-agnostic, because this policy describes a
    Windows installer while its tests run everywhere. Being lenient on POSIX
    costs nothing: no POSIX machine has an ``{app}`` to protect.
    """
    if not path:
        return ""
    return str(path).replace("\\", "/").rstrip("/").lower()


def _base(name: Optional[str]) -> str:
    return _norm(name).rsplit("/", 1)[-1]


def is_under(path: Optional[str], root: Optional[str]) -> bool:
    """True when ``path`` lies at or below ``root``.

    The boundary is a separator, so ``C:/Programs/RAGTools-old`` is NOT under
    ``C:/Programs/RAGTools``. A prefix comparison without that check is how a
    neighbouring directory gets swept into someone else's upgrade.
    """
    child, parent = _norm(path), _norm(root)
    if not child or not parent:
        return False
    return child == parent or child.startswith(parent + "/")


def ownership_evidence(proc: ProcessInfo, app_dir: str, data_dir: str) -> tuple[str, ...]:
    """Why this process counts as this installation's — or the empty tuple.

    Three kinds of evidence, deliberately asymmetric:

    * its executable lives under the installation or its data directory;
    * it has MAPPED a module out of the installation — the case an image-name
      filter cannot see and the case that produced ``[PYI-2072] Failed to load
      Python DLL``;
    * its image name is one this product owns outright.

    ``qdrant.exe`` earns no name evidence. That is the whole point.
    """
    evidence: list[str] = []
    if is_under(proc.executable, app_dir) or is_under(proc.executable, data_dir):
        evidence.append(EVIDENCE_EXECUTABLE_PATH)
    if any(is_under(module, app_dir) for module in proc.modules):
        evidence.append(EVIDENCE_LOADED_MODULE)
    if _base(proc.name) in OWNED_IMAGE_NAMES:
        evidence.append(EVIDENCE_IMAGE_NAME)
    return tuple(evidence)


def owns(proc: ProcessInfo, app_dir: str, data_dir: str) -> bool:
    return bool(ownership_evidence(proc, app_dir, data_dir))


_TRAY = re.compile(r"(?<![a-z])tray(?![a-z])")
_SUPERVISOR = re.compile(r"supervis")


def classify_role(proc: ProcessInfo) -> str:
    """What this process is to us, so the right phase owns stopping it."""
    if proc.role:
        return proc.role
    name = _base(proc.name)
    command = (proc.command_line or "").lower()
    if name in PATH_SCOPED_IMAGE_NAMES or name.startswith("qdrant"):
        return ROLE_ENGINE
    if _SUPERVISOR.search(command):
        return ROLE_SUPERVISOR
    if _TRAY.search(command):
        return ROLE_TRAY
    if name in OWNED_IMAGE_NAMES:
        return ROLE_SERVICE
    return ROLE_MODULE_HOLDER


def describe_process(proc: ProcessInfo, evidence: Sequence[str]) -> Blocker:
    return Blocker(
        kind=BLOCKER_PROCESS,
        identity=f"pid {proc.pid} {proc.name}",
        detail=(f"{classify_role(proc)}; {', '.join(evidence) or 'no evidence'}"
                + (f"; {proc.executable}" if proc.executable else "")
                + ("" if proc.modules_readable else "; loaded modules could not be read")),
        pid=proc.pid,
        path=proc.executable,
    )


# --------------------------------------------------------------------------
# The protocol
# --------------------------------------------------------------------------


class _Run:
    """One execution of the protocol. Holds nothing the caller should reuse."""

    def __init__(self, config: QuiescenceConfig, ports: Ports) -> None:
        self.config = config
        self.ports = ports
        self.phases: list[PhaseRecord] = []
        self.tasks: list[TaskRecord] = []
        self.started = ports.clock.now()
        self.deadline = self.started + max(config.timeout_seconds, 0.0)
        self.timed_out = False

    # -- bookkeeping -------------------------------------------------------

    def _record(self, phase: str, outcome: str, detail: str = "",
                started: Optional[float] = None,
                blockers: Sequence[Blocker] = ()) -> PhaseRecord:
        now = self.ports.clock.now()
        record = PhaseRecord(
            phase=phase,
            outcome=outcome,
            detail=detail,
            duration_ms=int(max(now - (started if started is not None else now), 0.0) * 1000),
            blockers=tuple(blockers),
        )
        self.phases.append(record)
        return record

    def _skip_remaining(self, after: str, reason: str) -> None:
        index = PHASES.index(after)
        for phase in PHASES[index + 1:]:
            self._record(phase, OUTCOME_SKIPPED, reason)

    def _advisory(self, phase: str, step) -> None:
        """Run a phase that only HELPS reach quiescence.

        Its failure is recorded and the protocol continues. Nothing is concluded
        from it: :data:`DECISIVE_PHASES` still have to pass before anything
        proceeds, so carrying on can only ever lead to a refusal that names a
        real blocker instead of an abort that names an exception.

        Exactly one record is produced either way — ``step`` records its own on
        success, and this records the error otherwise.
        """
        started = self.ports.clock.now()
        try:
            step()
        except Exception as exc:  # noqa: BLE001 — advisory is what that means
            self._record(phase, OUTCOME_ERROR, f"{type(exc).__name__}: {exc}", started)

    def _expired(self) -> bool:
        return self.ports.clock.now() >= self.deadline

    # -- process views -----------------------------------------------------

    def _owned(self) -> list[tuple[ProcessInfo, tuple[str, ...]]]:
        found = []
        for proc in self.ports.processes.list_processes():
            evidence = ownership_evidence(proc, self.config.app_dir, self.config.data_dir)
            if evidence:
                found.append((proc, evidence))
        return found

    def _issue_stops(self, role: str) -> tuple[bool, list[int], int]:
        """Ask, then insist. Never conclude — that is :meth:`_owned`'s job.

        Returns ``(graceful request accepted, pids stopped, pids spared)``. The
        engine is the only role with anything to spare, and sparing it is the
        rule that keeps this installer out of someone else's database.
        """
        asked = self.ports.installation.request_stop(role)
        stopped: list[int] = []
        spared = 0
        for proc in self.ports.processes.list_processes():
            if classify_role(proc) != role:
                continue
            if role == ROLE_ENGINE:
                if not (is_under(proc.executable, self.config.app_dir)
                        or is_under(proc.executable, self.config.data_dir)):
                    spared += 1
                    continue
            elif not owns(proc, self.config.app_dir, self.config.data_dir):
                continue
            if self.ports.processes.stop(proc.pid, force=True):
                stopped.append(proc.pid)
        return asked, stopped, spared

    def _stop_role(self, phase: str, role: str) -> PhaseRecord:
        started = self.ports.clock.now()
        asked, stopped, spared = self._issue_stops(role)
        detail = (f"graceful request {'accepted' if asked else 'unavailable'}; "
                  f"stop issued to {len(stopped)} process(es)")
        if spared:
            detail += (f"; left {spared} Qdrant process(es) alone — outside this "
                       "installation, so not ours to stop")
        return self._record(phase, OUTCOME_OK, detail, started)

    # -- phases ------------------------------------------------------------

    def detect(self) -> Optional[Installation]:
        started = self.ports.clock.now()
        installation = self.ports.installation.detect()
        if installation is None or not installation.present:
            self._record(PHASE_DETECT, OUTCOME_SKIPPED,
                         "no previous installation; a fresh install pays for none "
                         "of this", started)
            return None
        self._record(PHASE_DETECT, OUTCOME_OK, installation.describe(), started)
        return installation

    def enter_maintenance(self) -> None:
        started = self.ports.clock.now()
        entered = self.ports.installation.enter_maintenance()
        if entered:
            self._record(PHASE_MAINTENANCE, OUTCOME_OK,
                         "installed version entered upgrade maintenance", started)
        else:
            self._record(PHASE_MAINTENANCE, OUTCOME_SKIPPED,
                         "installed version does not support an upgrade-maintenance "
                         "state; absence is not a failure", started)

    def disable_tasks(self) -> None:
        """Disable, not ``/end``.

        ``/end`` stops the running instance and leaves the trigger armed, so the
        scheduler is free to start a replacement between the kill and the copy —
        and the registration carries ``RestartOnFailure``, which a force-kill is
        precisely the failure it reacts to. The prior state is recorded because
        the upgrade has to put it back, and a task the user had already disabled
        must stay disabled.
        """
        started = self.ports.clock.now()
        states = dict(self.ports.tasks.states())
        for name in OWNED_TASKS:
            prior = states.get(name, "Missing")
            if prior in ("Missing", ""):
                continue
            if prior.lower() == "disabled":
                self.tasks.append(TaskRecord(name, prior, disabled_by_us=False))
                continue
            ok = self.ports.tasks.disable(name)
            self.tasks.append(TaskRecord(name, prior, disabled_by_us=bool(ok)))
        disabled = [t.name for t in self.tasks if t.disabled_by_us]
        if not self.tasks:
            self._record(PHASE_DISABLE_TASKS, OUTCOME_SKIPPED,
                         "no owned scheduled task is registered", started)
        else:
            self._record(PHASE_DISABLE_TASKS, OUTCOME_OK,
                         f"disabled {len(disabled)} of {len(self.tasks)} owned task(s)"
                         + (f": {', '.join(disabled)}" if disabled else ""), started)

    def stop_roles(self) -> None:
        """One phase per role, in the order that makes each one cheap.

        Tray first (a UI process that exits on request), then the supervisor
        (which would otherwise restart its child), then the service children,
        then the engine — path-scoped, always, because ``storage_backend =
        "external"`` means a server the user runs and stopping every
        ``qdrant.exe`` on the machine would be data loss in someone else's
        application caused by installing this one.
        """
        for phase, role in ((PHASE_STOP_TRAY, ROLE_TRAY),
                            (PHASE_STOP_SUPERVISOR, ROLE_SUPERVISOR),
                            (PHASE_STOP_SERVICE_CHILDREN, ROLE_SERVICE),
                            (PHASE_STOP_ENGINE, ROLE_ENGINE)):
            # Advisory INDIVIDUALLY: one role failing to stop must not cost the
            # three that would have.
            self._advisory(phase, lambda p=phase, r=role: self._stop_role(p, r))

    def identify_holders(self) -> list[Blocker]:
        started = self.ports.clock.now()
        blockers = [describe_process(proc, evidence) for proc, evidence in self._owned()]
        unscannable = [p.pid for p in self.ports.processes.list_processes()
                       if not p.modules_readable]
        detail = f"{len(blockers)} owned process(es) still running"
        if unscannable:
            detail += (f"; {len(unscannable)} process(es) refused a module scan "
                       f"(pids {', '.join(str(p) for p in unscannable)}) — recorded, "
                       "not dropped; the replaceability probe is what decides")
        self._record(PHASE_IDENTIFY_HOLDERS,
                     OUTCOME_BLOCKED if blockers else OUTCOME_OK,
                     detail, started, blockers)
        return blockers

    def wait_for_exit(self) -> None:
        """A signal delivered is not a handle released.

        Always sleeps at least once, even when nothing is left: the kernel
        releases the image's file handle after `taskkill` returns, not before,
        and the copy that follows fails on exactly that gap.
        """
        started = self.ports.clock.now()
        slept = 0.0
        for _ in range(max(self.config.settle_rounds, 1)):
            self.ports.clock.sleep(self.config.settle_seconds)
            slept += self.config.settle_seconds
            if not self._owned() or self._expired():
                break
        remaining = self._owned()
        if remaining and self._expired():
            self.timed_out = True
            self._record(PHASE_WAIT, OUTCOME_BLOCKED,
                         f"timeout after {self.config.timeout_seconds:g}s with "
                         f"{len(remaining)} owned process(es) still running — refusing "
                         "BEFORE the first write, so the installation is untouched",
                         started)
            return
        if remaining:
            self._record(PHASE_WAIT, OUTCOME_BLOCKED,
                         f"{len(remaining)} owned process(es) still running after "
                         f"{slept:g}s", started)
            return
        self._record(PHASE_WAIT, OUTCOME_OK, f"settled after {slept:g}s", started)

    def retry(self) -> None:
        """Only the safe half, and only while there is time.

        Every attempt re-issues shutdown requests and re-looks. Nothing here
        re-runs :func:`disable_tasks`, which is the one earlier phase whose
        second run would destroy information rather than merely repeat work.
        """
        started = self.ports.clock.now()
        if not self._owned():
            self._record(PHASE_RETRY, OUTCOME_SKIPPED, "nothing left to retry", started)
            return
        attempts = 0
        for backoff in self.config.retry_backoff:
            if self._expired():
                self.timed_out = True
                break
            attempts += 1
            self.ports.clock.sleep(backoff)
            for phase in RETRYABLE_PHASES:
                role = PHASE_ROLE[phase]
                self._issue_stops(role)
            if not self._owned():
                break
        remaining = self._owned()
        if remaining:
            self._record(PHASE_RETRY, OUTCOME_BLOCKED,
                         f"{attempts} retry attempt(s); {len(remaining)} process(es) "
                         "still holding installation files", started)
        else:
            self._record(PHASE_RETRY, OUTCOME_OK,
                         f"cleared after {attempts} retry attempt(s)", started)

    def verify_clear(self) -> list[Blocker]:
        started = self.ports.clock.now()
        blockers = [describe_process(proc, evidence) for proc, evidence in self._owned()]
        self._record(PHASE_VERIFY_CLEAR,
                     OUTCOME_BLOCKED if blockers else OUTCOME_OK,
                     ("no owned process is running" if not blockers
                      else f"{len(blockers)} owned process(es) survived every stop"),
                     started, blockers)
        return blockers

    def probe_replaceable(self) -> list[Blocker]:
        """The step that would have caught the real failure.

        Process enumeration can come back clean while a handle survives — a
        duplicated handle, a driver, an indexer, a scan. So every file the
        installer is about to delete or overwrite is opened exclusively first,
        and anything that refuses is a blocker reported BY PATH.
        """
        started = self.ports.clock.now()
        blockers: list[Blocker] = []
        checked = 0
        for path in self.ports.locks.replaceable_candidates():
            if not str(path).lower().endswith(REPLACEABLE_SUFFIXES):
                continue
            checked += 1
            ok, reason = self.ports.locks.can_replace(path)
            if not ok:
                blockers.append(Blocker(
                    kind=BLOCKER_FILE,
                    identity=str(path),
                    detail=reason or "cannot be opened exclusively, so it cannot be replaced",
                    path=str(path),
                ))
        self._record(PHASE_PROBE_REPLACEABLE,
                     OUTCOME_BLOCKED if blockers else OUTCOME_OK,
                     (f"{checked} file(s) proven replaceable" if not blockers
                      else f"{len(blockers)} of {checked} file(s) cannot be replaced"),
                     started, blockers)
        return blockers


def run(config: QuiescenceConfig, ports: Ports) -> Verdict:
    """Execute the protocol and return a verdict. Writes nothing, anywhere."""
    run_state = _Run(config, ports)
    try:
        installation = run_state.detect()
        if installation is None:
            run_state._skip_remaining(PHASE_DETECT, REASON_NO_PREVIOUS_INSTALLATION)
            return Verdict(
                exit_code=EXIT_QUIESCENT,
                quiescent=True,
                reason=REASON_NO_PREVIOUS_INSTALLATION,
                app_dir=config.app_dir,
                data_dir=config.data_dir,
                installation=Installation(present=False),
                phases=run_state.phases,
                elapsed_seconds=ports.clock.now() - run_state.started,
            )

        # Advisory: each of these only HELPS reach quiescence, so one of them
        # raising is recorded and the rest still run. `verify_clear` and
        # `probe_replaceable` below are DECISIVE and deliberately are not
        # wrapped — an error there means the answer is unknown, and unknown
        # refuses.
        run_state._advisory(PHASE_MAINTENANCE, run_state.enter_maintenance)
        run_state._advisory(PHASE_DISABLE_TASKS, run_state.disable_tasks)
        run_state.stop_roles()
        run_state._advisory(PHASE_IDENTIFY_HOLDERS, run_state.identify_holders)
        run_state._advisory(PHASE_WAIT, run_state.wait_for_exit)
        run_state._advisory(PHASE_RETRY, run_state.retry)
        process_blockers = run_state.verify_clear()
        # Run regardless of what the process sweep found: fixing one blocker at
        # a time across five failed attempts is a miserable way to upgrade.
        file_blockers = run_state.probe_replaceable()
    except Exception as exc:  # noqa: BLE001 — a port failing is an abort, not a verdict
        return Verdict(
            exit_code=EXIT_INTERNAL_ERROR,
            quiescent=False,
            reason=REASON_INTERNAL_ERROR,
            app_dir=config.app_dir,
            data_dir=config.data_dir,
            phases=run_state.phases,
            blockers=[Blocker(kind="internal", identity=type(exc).__name__, detail=str(exc))],
            tasks=run_state.tasks,
            elapsed_seconds=_safe_elapsed(ports, run_state.started),
        )

    blockers = process_blockers + file_blockers
    if blockers:
        if run_state.timed_out:
            reason = REASON_TIMEOUT
        elif process_blockers:
            reason = REASON_PROCESSES_SURVIVED
        else:
            reason = REASON_FILES_NOT_REPLACEABLE
    else:
        reason = REASON_QUIESCENT

    return Verdict(
        exit_code=EXIT_QUIESCENT if not blockers else EXIT_NOT_QUIESCENT,
        quiescent=not blockers,
        reason=reason,
        app_dir=config.app_dir,
        data_dir=config.data_dir,
        installation=installation,
        phases=run_state.phases,
        blockers=blockers,
        tasks=run_state.tasks,
        elapsed_seconds=ports.clock.now() - run_state.started,
    )


def _safe_elapsed(ports: Ports, started: float) -> float:
    try:
        return ports.clock.now() - started
    except Exception:  # noqa: BLE001 — the clock itself may be what failed
        return 0.0


def decide(config: QuiescenceConfig, ports: Ports) -> int:
    """The protocol reduced to the number Setup acts on."""
    return run(config, ports).exit_code


# --------------------------------------------------------------------------
# Restoring what the upgrade disabled
# --------------------------------------------------------------------------


def restore_tasks(records: Iterable[TaskRecord], tasks: TaskPort) -> list[str]:
    """Re-enable exactly what this upgrade disabled, and nothing else.

    A task the user had already disabled stays disabled. Re-enabling everything
    "to be safe" would silently switch autostart back on for someone who turned
    it off, which is a behaviour change delivered by an upgrade — the category
    of surprise this product has spent three releases removing.
    """
    restored: list[str] = []
    for record in records:
        if not record.disabled_by_us:
            continue
        if tasks.enable(record.name):
            restored.append(record.name)
    return restored


# --------------------------------------------------------------------------
# The other half: what must hold AFTER the write, and when to roll back
# --------------------------------------------------------------------------

CHECK_EXECUTABLE_LAUNCHES = "executable_launches"
CHECK_REPORTS_VERSION = "reports_candidate_version"
CHECK_UNINSTALL_REGISTRY = "uninstall_registry_updated"
CHECK_BINARY_CONSISTENCY = "binary_consistency"
CHECK_STARTUP_LIFECYCLE = "startup_lifecycle_restored"
CHECK_SERVICE_STARTED = "service_started"
CHECK_HEALTH_ANSWERS = "health_answers"
CHECK_ENGINE_OWNERSHIP = "engine_ownership_intact"
CHECK_CONFIG_INTACT = "config_intact"
CHECK_IDENTITY_INTACT = "identity_intact"
CHECK_INDEXED_DATA = "indexed_data_survived"

POST_INSTALL_CHECKS: tuple[str, ...] = (
    CHECK_EXECUTABLE_LAUNCHES,
    CHECK_REPORTS_VERSION,
    CHECK_UNINSTALL_REGISTRY,
    CHECK_BINARY_CONSISTENCY,
    CHECK_STARTUP_LIFECYCLE,
    CHECK_SERVICE_STARTED,
    CHECK_HEALTH_ANSWERS,
    CHECK_ENGINE_OWNERSHIP,
    CHECK_CONFIG_INTACT,
    CHECK_IDENTITY_INTACT,
    CHECK_INDEXED_DATA,
)

#: Failures that mean the DIRECTORY is wrong — a mixed PyInstaller tree, a
#: registry still naming the old version, an executable that will not load its
#: own Python DLL. These are exactly what the rollback can put back, so these
#: are what trigger it.
INTEGRITY_CHECKS: frozenset[str] = frozenset({
    CHECK_EXECUTABLE_LAUNCHES,
    CHECK_REPORTS_VERSION,
    CHECK_UNINSTALL_REGISTRY,
    CHECK_BINARY_CONSISTENCY,
})

#: Failures that mean the files are correct and something at RUN TIME is stuck.
#: Restoring the previous bundle would not fix a storage outage, a held port or
#: a rebuild that stopped early — it would replace a correct installation with
#: an older one and leave the actual cause untouched. This product already
#: learned that lesson once, in `installer.iss`: a single fixed "restart Windows
#: and reinstall" message for every failing check was a wrong diagnosis with a
#: wrong and disruptive remedy.
RUNTIME_CHECKS: frozenset[str] = frozenset(POST_INSTALL_CHECKS) - INTEGRITY_CHECKS


@dataclass(frozen=True)
class PostInstallCheck:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class PostInstallVerdict:
    ok: bool
    restore_backup: bool
    failed: tuple[str, ...]
    integrity_failures: tuple[str, ...]
    runtime_failures: tuple[str, ...]
    message: str


def decide_post_install(checks: Iterable[PostInstallCheck]) -> PostInstallVerdict:
    """What to do about the state the machine is actually in.

    Two outcomes, because there are two causes and they have opposite remedies:
    an integrity failure means the write went wrong and the previous bundle must
    be put back; a runtime failure means the write went right and something else
    is stuck, which reinstalling cannot fix.
    """
    checks = list(checks)
    known = {c.name for c in checks}
    missing = [name for name in POST_INSTALL_CHECKS if name not in known]
    failed = tuple(c.name for c in checks if not c.ok)
    integrity = tuple(n for n in failed if n in INTEGRITY_CHECKS)
    runtime = tuple(n for n in failed if n in RUNTIME_CHECKS)
    details = {c.name: c.detail for c in checks}

    if missing:
        # A check that did not run is not a check that passed. Treating silence
        # as success is how a half-verified upgrade reports a clean bill.
        return PostInstallVerdict(
            ok=False, restore_backup=False, failed=tuple(missing),
            integrity_failures=(), runtime_failures=tuple(missing),
            message=("Verification did not finish: no result for "
                     + ", ".join(missing)
                     + ". The installation was NOT rolled back, because an "
                       "unfinished check is not evidence that anything is wrong. "
                       "Run:  rag selfcheck"),
        )

    if integrity:
        lines = [
            "RAG Tools could not be installed cleanly, so the previous version "
            "has been restored from the rollback copy made before extraction.",
            "",
            "What failed:",
        ]
        lines += [f"  - {name}: {details.get(name, '')}".rstrip() for name in integrity]
        lines += [
            "",
            "Your projects, configuration and index were not touched. Close any "
            "application that may be using RAG Tools (an editor, an MCP client, a "
            "terminal) and run this installer again.",
        ]
        return PostInstallVerdict(
            ok=False, restore_backup=True, failed=failed,
            integrity_failures=integrity, runtime_failures=runtime,
            message="\n".join(lines),
        )

    if runtime:
        lines = [
            "RAG Tools was installed successfully, but it is not running "
            "properly yet.",
            "",
            "The files on this machine are correct, so the installation has NOT "
            "been rolled back and reinstalling will not help. What is not "
            "working:",
        ]
        lines += [f"  - {name}: {details.get(name, '')}".rstrip() for name in runtime]
        lines += ["", "To see the exact cause and its remedy, run:  rag selfcheck"]
        return PostInstallVerdict(
            ok=False, restore_backup=False, failed=failed,
            integrity_failures=(), runtime_failures=runtime,
            message="\n".join(lines),
        )

    return PostInstallVerdict(
        ok=True, restore_backup=False, failed=(),
        integrity_failures=(), runtime_failures=(),
        message="RAG Tools was installed and verified.",
    )


def check_uninstall_registry(recorded: Optional[str], expected: str) -> PostInstallCheck:
    """The uninstall entry is what Add/Remove Programs, winget and Inno's own
    upgrade detection read. An upgrade that replaced files without moving it
    leaves the rest of the system describing the previous version — which is
    precisely what the reproduced failure reported: ``registry reads 3.3.0``."""
    if not recorded:
        return PostInstallCheck(CHECK_UNINSTALL_REGISTRY, False,
                                "no uninstall registry entry was found")
    if recorded != expected:
        return PostInstallCheck(CHECK_UNINSTALL_REGISTRY, False,
                                f"registry reads {recorded}, expected {expected}")
    return PostInstallCheck(CHECK_UNINSTALL_REGISTRY, True, recorded)


def check_binary_consistency(present: Iterable[str], required: Iterable[str] = ()
                             ) -> PostInstallCheck:
    """Every executable and DLL the bundle names is on disk.

    A partially deleted ``_internal`` is the shape of the failure: ``rag.exe``
    survives, ``python312.dll`` does not, and the only symptom the user sees is
    ``[PYI-2072]``.
    """
    have = {_norm(p) for p in present}
    required = tuple(required) or ("rag.exe", "ragw.exe", "_internal/python312.dll")

    def _seen(name: str) -> bool:
        wanted = _norm(name)
        # Anchored on a separator so `myrag.exe` cannot satisfy `rag.exe`.
        return any(entry == wanted or entry.endswith("/" + wanted) for entry in have)

    missing = [name for name in required if not _seen(name)]
    if missing:
        return PostInstallCheck(CHECK_BINARY_CONSISTENCY, False,
                                "missing after extraction: " + ", ".join(missing))
    return PostInstallCheck(CHECK_BINARY_CONSISTENCY, True,
                            f"{len(have)} file(s) present")


# --------------------------------------------------------------------------
# The rollback copy
# --------------------------------------------------------------------------

#: What must survive a failed extraction. The payload directory and the two
#: executables — everything the machine needs to still run the OLD version.
BACKUP_ITEMS: tuple[str, ...] = ("_internal", "rag.exe", "ragw.exe")


@dataclass(frozen=True)
class BackupPlan:
    app_dir: str
    backup_dir: str
    items: tuple[str, ...] = BACKUP_ITEMS


def plan_backup(app_dir: str, stamp: str) -> BackupPlan:
    """Where the rollback copy goes: a SIBLING of the installation.

    Not inside it. ``[InstallDelete]`` removes ``{app}\\_internal`` wholesale and
    ``[UninstallDelete]`` removes ``{app}`` entirely, so a backup kept under the
    installation is deleted by the very operations it exists to survive — the
    same mistake ``BackupConfig`` already avoids for ``config.toml``.
    """
    original = str(app_dir).rstrip("\\/")
    cut = max(original.rfind("\\"), original.rfind("/"))
    if cut == -1:
        return BackupPlan(app_dir=str(app_dir), backup_dir=f"{original}-rollback-{stamp}")
    # Split on the ORIGINAL separator so the result is one path, not a mixture.
    parent, sep, leaf = original[:cut], original[cut], original[cut + 1:]
    return BackupPlan(app_dir=str(app_dir),
                      backup_dir=f"{parent}{sep}{leaf}-rollback-{stamp}")


def backup_is_outside_installation(plan: BackupPlan) -> bool:
    return not is_under(plan.backup_dir, plan.app_dir)


def verify_backup(plan: BackupPlan, present: Iterable[str]) -> tuple[bool, list[str]]:
    """``(complete, missing)`` for the copy that a rollback would restore.

    Checked BEFORE the first destructive write, because a rollback copy that
    turns out to be incomplete after ``_internal`` has been deleted is not a
    rollback copy — it is a second way to lose the installation.
    """
    have = {_base(p) for p in present}
    missing = [item for item in plan.items if _base(item) not in have]
    return (not missing), missing


def plan_rollback(plan: BackupPlan) -> tuple[str, ...]:
    """Restore order: the payload first, then the executables.

    ``rag.exe`` without its ``_internal`` is the exact mixed state being undone,
    so it must never be the thing that exists first.
    """
    ordered = [item for item in plan.items if item == "_internal"]
    ordered += [item for item in plan.items if item != "_internal"]
    return tuple(ordered)


__all__ = [
    "PHASE_DETECT", "PHASE_MAINTENANCE", "PHASE_DISABLE_TASKS", "PHASE_STOP_TRAY",
    "PHASE_STOP_SUPERVISOR", "PHASE_STOP_SERVICE_CHILDREN", "PHASE_STOP_ENGINE",
    "PHASE_IDENTIFY_HOLDERS", "PHASE_WAIT", "PHASE_RETRY", "PHASE_VERIFY_CLEAR",
    "PHASE_PROBE_REPLACEABLE", "PHASE_ROLE",
    "REASON_QUIESCENT", "REASON_NO_PREVIOUS_INSTALLATION",
    "REASON_PROCESSES_SURVIVED", "REASON_FILES_NOT_REPLACEABLE",
    "REASON_TIMEOUT", "REASON_INTERNAL_ERROR",
    "BLOCKER_PROCESS", "BLOCKER_FILE",
    "PHASES", "RESTORE_PHASE", "RETRYABLE_PHASES", "DECISIVE_PHASES",
    "ADVISORY_PHASES", "EXIT_CODES",
    "EXIT_QUIESCENT", "EXIT_NOT_QUIESCENT", "EXIT_INTERNAL_ERROR",
    "SCHEMA", "VERDICT_KEYS",
    "OUTCOME_OK", "OUTCOME_SKIPPED", "OUTCOME_BLOCKED", "OUTCOME_ERROR",
    "ROLE_TRAY", "ROLE_SUPERVISOR", "ROLE_SERVICE", "ROLE_ENGINE",
    "ROLE_MODULE_HOLDER", "ROLE_PHASE",
    "EVIDENCE_IMAGE_NAME", "EVIDENCE_EXECUTABLE_PATH", "EVIDENCE_LOADED_MODULE",
    "OWNED_IMAGE_NAMES", "PATH_SCOPED_IMAGE_NAMES", "OWNED_TASKS",
    "REPLACEABLE_SUFFIXES",
    "Installation", "ProcessInfo", "Blocker", "TaskRecord", "PhaseRecord",
    "Verdict", "QuiescenceConfig", "Ports",
    "InstallationPort", "ProcessPort", "TaskPort", "LockPort", "ClockPort",
    "is_under", "ownership_evidence", "owns", "classify_role",
    "run", "decide", "restore_tasks",
    "POST_INSTALL_CHECKS", "INTEGRITY_CHECKS", "RUNTIME_CHECKS",
    "PostInstallCheck", "PostInstallVerdict", "decide_post_install",
    "check_uninstall_registry", "check_binary_consistency",
    "BACKUP_ITEMS", "BackupPlan", "plan_backup", "backup_is_outside_installation",
    "verify_backup", "plan_rollback",
]
