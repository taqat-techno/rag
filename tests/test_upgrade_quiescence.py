"""WP-R02 — the upgrade must refuse before its first destructive write.

Every scenario the release requires, against the policy in
:mod:`ragtools.upgrade.quiescence` with fake ports. Nothing here needs Windows,
a real installation, or a real file lock: the OS work lives in
``installer/quiesce.ps1`` and the DECISION lives here, which is the whole reason
the two were separated.

The invariant under test:

    If any installation-owning process cannot be stopped, the upgrade aborts
    BEFORE its first destructive write and the old installation remains
    runnable.

The failure it comes from, reproduced on a GitHub-hosted ``windows-latest``
runner (job 91273392455), genuine packaged v3.3.0 -> candidate: Setup exited 5
with ``_internal`` half deleted, ``rag.exe`` unable to load
``_internal\\python312.dll``, and the uninstall registry still reading 3.3.0.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from ragtools.upgrade import quiescence as q


APP = r"C:\Users\r\AppData\Local\Programs\RAGTools"
DATA = r"C:\Users\r\AppData\Local\RAGTools"


# --------------------------------------------------------------------------
# Fakes. Each one models a real behaviour of its port, not a stub of it.
# --------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class FakeInstallation:
    def __init__(self, *, present: bool = True, version: str = "3.3.0",
                 maintenance: bool = False, accepts: tuple[str, ...] = ()) -> None:
        self._installation = (q.Installation(present=True, version=version,
                                             executable=APP + r"\rag.exe", app_dir=APP)
                              if present else None)
        self._maintenance = maintenance
        self._accepts = set(accepts)
        self.requests: list[str] = []

    def detect(self):
        return self._installation

    def enter_maintenance(self) -> bool:
        return self._maintenance

    def request_stop(self, role: str) -> bool:
        self.requests.append(role)
        return role in self._accepts


class FakeTasks:
    """Task Scheduler, with the distinction that matters.

    ``honour_disable=False`` models ``schtasks /end``: the running instance is
    stopped and the call reports success, but the TRIGGER STAYS ARMED — so the
    scheduler is free to start a replacement between the kill and the copy.
    """

    def __init__(self, states: dict | None = None, *, honour_disable: bool = True) -> None:
        self._states = dict(states if states is not None
                            else {name: "Ready" for name in q.OWNED_TASKS})
        self.honour_disable = honour_disable
        self.disable_calls: list[str] = []
        self.enable_calls: list[str] = []

    def states(self):
        return dict(self._states)

    def disable(self, name: str) -> bool:
        self.disable_calls.append(name)
        if not self.honour_disable:
            return True
        self._states[name] = "Disabled"
        return True

    def enable(self, name: str) -> bool:
        self.enable_calls.append(name)
        self._states[name] = "Ready"
        return True

    @property
    def armed(self) -> bool:
        return any(state.lower() != "disabled" for state in self._states.values())


class FakeProcesses:
    def __init__(self, procs=(), *, refuse=(), respawn=(), tasks: FakeTasks | None = None,
                 attempts_needed: dict | None = None, raises: bool = False) -> None:
        self.procs = {p.pid: p for p in procs}
        self.refuse = set(refuse)
        self.respawn = set(respawn)
        self.tasks = tasks
        self.attempts_needed = dict(attempts_needed or {})
        self.attempts: dict[int, int] = {}
        self.stop_calls: list[int] = []
        self.raises = raises
        self._next_pid = 9000

    def list_processes(self):
        if self.raises:
            raise OSError("the process table could not be read")
        return list(self.procs.values())

    def stop(self, pid: int, *, force: bool = False) -> bool:
        self.stop_calls.append(pid)
        proc = self.procs.get(pid)
        if proc is None:
            return False
        # The REQUEST is accepted either way. Whether the process died is
        # decided by looking, never by this return value.
        if pid in self.refuse:
            return True
        need = self.attempts_needed.get(pid)
        if need is not None:
            self.attempts[pid] = self.attempts.get(pid, 0) + 1
            if self.attempts[pid] < need:
                return True
        del self.procs[pid]
        if pid in self.respawn and self.tasks is not None and self.tasks.armed:
            self._next_pid += 1
            reborn = replace(proc, pid=self._next_pid)
            self.procs[reborn.pid] = reborn
            self.respawn.add(reborn.pid)
        return True


class FakeLocks:
    def __init__(self, candidates=(), unreplaceable: dict | None = None) -> None:
        self.candidates = list(candidates)
        self.unreplaceable = dict(unreplaceable or {})
        self.probed: list[str] = []

    def replaceable_candidates(self):
        return list(self.candidates)

    def can_replace(self, path: str):
        self.probed.append(path)
        reason = self.unreplaceable.get(path)
        if reason:
            return False, reason
        return True, ""


DEFAULT_FILES = (
    APP + r"\rag.exe",
    APP + r"\ragw.exe",
    APP + r"\_internal\python312.dll",
    APP + r"\_internal\base_library.zip",   # not a probe candidate; must be skipped
)


def tray(pid=101):
    return q.ProcessInfo(pid=pid, name="ragw.exe", executable=APP + r"\ragw.exe",
                         command_line=f'"{APP}\\ragw.exe" tray')


def supervisor(pid=102):
    return q.ProcessInfo(pid=pid, name="ragw.exe", executable=APP + r"\ragw.exe",
                         command_line=f'"{APP}\\ragw.exe" service run --supervisor')


def service(pid=103):
    return q.ProcessInfo(pid=pid, name="rag.exe", executable=APP + r"\rag.exe",
                         command_line=f'"{APP}\\rag.exe" service run')


def engine(pid=104, *, executable=DATA + r"\bin\qdrant.exe"):
    return q.ProcessInfo(pid=pid, name="qdrant.exe", executable=executable,
                         command_line=f'"{executable}" --config-path ...')


def mcp_client(pid=105):
    """An MCP-like process: an unrelated image name that mapped our DLL."""
    return q.ProcessInfo(
        pid=pid, name="python.exe", executable=r"C:\Python312\python.exe",
        command_line=r'"C:\Python312\python.exe" -m some_client',
        modules=(r"C:\Python312\python.exe", APP + r"\_internal\python312.dll"),
    )


def build(procs=(), *, refuse=(), respawn=(), attempts_needed=None,
          tasks=None, locks=None, present=True, timeout=120.0,
          accepts=("tray", "supervisor", "service"), raises=False):
    tasks = tasks if tasks is not None else FakeTasks()
    processes = FakeProcesses(procs, refuse=refuse, respawn=respawn, tasks=tasks,
                              attempts_needed=attempts_needed, raises=raises)
    ports = q.Ports(
        installation=FakeInstallation(present=present, accepts=accepts),
        processes=processes,
        tasks=tasks,
        locks=locks if locks is not None else FakeLocks(DEFAULT_FILES),
        clock=FakeClock(),
    )
    config = q.QuiescenceConfig(app_dir=APP, data_dir=DATA, timeout_seconds=timeout)
    return config, ports


def blocker_pids(verdict) -> set[int]:
    return {b.pid for b in verdict.blockers if b.pid is not None}


# --------------------------------------------------------------------------
# 1. graceful shutdown succeeds
# --------------------------------------------------------------------------


def test_a_graceful_shutdown_lets_the_upgrade_proceed():
    config, ports = build([tray(), supervisor(), service(), engine()])
    verdict = q.run(config, ports)

    assert verdict.exit_code == q.EXIT_QUIESCENT
    assert verdict.quiescent is True
    assert verdict.reason == q.REASON_QUIESCENT
    assert verdict.blockers == []
    assert [p.phase for p in verdict.phases] == list(q.PHASES), (
        "every phase must be recorded, in order, even on the happy path — a "
        "verdict that omits the phases it skipped cannot be audited"
    )


def test_a_fresh_install_pays_for_none_of_this():
    """No previous installation: report quiescent immediately, touch nothing."""
    config, ports = build([], present=False)
    verdict = q.run(config, ports)

    assert verdict.exit_code == q.EXIT_QUIESCENT
    assert verdict.reason == q.REASON_NO_PREVIOUS_INSTALLATION
    assert ports.tasks.disable_calls == [], "a fresh install disabled a scheduled task"
    assert ports.processes.stop_calls == [], "a fresh install stopped a process"
    assert ports.locks.probed == [], "a fresh install probed files that do not exist"
    # Still fully recorded: skipped is an outcome, not an absence.
    assert [p.phase for p in verdict.phases] == list(q.PHASES)
    assert all(p.outcome == q.OUTCOME_SKIPPED for p in verdict.phases[1:])


# --------------------------------------------------------------------------
# 2-5. one role at a time survives
# --------------------------------------------------------------------------


@pytest.mark.parametrize("factory,role", [
    (tray, q.ROLE_TRAY),
    (supervisor, q.ROLE_SUPERVISOR),
    (service, q.ROLE_SERVICE),
    (engine, q.ROLE_ENGINE),
])
def test_a_surviving_process_refuses_the_upgrade(factory, role):
    """Whatever the role, a process still holding installation files means the
    next write would fail — so the answer is 2, not "try anyway"."""
    proc = factory()
    config, ports = build([proc], refuse=[proc.pid])
    verdict = q.run(config, ports)

    assert verdict.exit_code == q.EXIT_NOT_QUIESCENT
    assert verdict.reason == q.REASON_PROCESSES_SURVIVED
    assert proc.pid in blocker_pids(verdict)
    named = [b for b in verdict.blockers if b.pid == proc.pid][0]
    assert role in named.detail, f"the blocker does not say what {proc.name} is"
    assert named.kind == q.BLOCKER_PROCESS


def test_the_managed_engine_is_stopped_but_a_foreign_qdrant_is_not():
    """`storage_backend = "external"` means a server the user runs.

    Stopping every `qdrant.exe` on the machine would be data loss in someone
    else's application caused by installing this one — so ownership of that name
    is decided by PATH, never by the name itself.
    """
    ours = engine(pid=201, executable=DATA + r"\bin\qdrant.exe")
    theirs = engine(pid=202, executable=r"D:\services\qdrant\qdrant.exe")
    config, ports = build([ours, theirs])
    verdict = q.run(config, ports)

    assert ours.pid in ports.processes.stop_calls, "the managed engine was not stopped"
    assert theirs.pid not in ports.processes.stop_calls, (
        "a Qdrant outside this installation was stopped — that is data loss in "
        "another application"
    )
    assert verdict.exit_code == q.EXIT_QUIESCENT
    assert theirs.pid not in blocker_pids(verdict), (
        "a foreign Qdrant was reported as blocking OUR upgrade"
    )


# --------------------------------------------------------------------------
# 6. the case an image-name filter cannot see
# --------------------------------------------------------------------------


def test_a_process_with_an_unrelated_name_that_loaded_our_dll_is_found():
    """The reproduced failure, in one process.

    `Get-Process -Name rag,ragw,qdrant` — the filter this installer used — is
    blind to a process called anything else. One that has MAPPED
    `_internal\\python312.dll` holds the file exactly as hard, and
    `[InstallDelete]` is the first thing that finds out.
    """
    holder = mcp_client()
    assert holder.name not in q.OWNED_IMAGE_NAMES + q.PATH_SCOPED_IMAGE_NAMES, (
        "this scenario is only meaningful if the image name is NOT one we match"
    )

    evidence = q.ownership_evidence(holder, APP, DATA)
    assert q.EVIDENCE_LOADED_MODULE in evidence
    assert q.EVIDENCE_EXECUTABLE_PATH not in evidence, (
        "the executable is outside the installation; only the loaded module ties "
        "this process to us"
    )

    config, ports = build([holder])
    verdict = q.run(config, ports)

    assert verdict.exit_code == q.EXIT_NOT_QUIESCENT
    assert holder.pid in blocker_pids(verdict)
    assert q.classify_role(holder) == q.ROLE_MODULE_HOLDER


def test_an_unrecognised_holder_is_reported_rather_than_killed():
    """We do not force-kill arbitrary user processes.

    A `module_holder` has no stop phase on purpose: it might be the user's
    editor with unsaved work. It is named, and the upgrade refuses — which is
    the honest outcome and the one the user can act on.
    """
    holder = mcp_client()
    config, ports = build([holder])
    verdict = q.run(config, ports)

    assert ports.processes.stop_calls == [], (
        "an unrecognised process was force-killed by the upgrade"
    )
    assert verdict.exit_code == q.EXIT_NOT_QUIESCENT
    assert holder.executable in (verdict.blockers[0].detail or "")


def test_a_process_whose_modules_cannot_be_read_is_recorded_not_dropped():
    """Access denied is not evidence of absence. It is also not, on its own, a
    blocker — or every upgrade would refuse on `System`. The replaceability
    probe decides; this explains."""
    opaque = q.ProcessInfo(pid=300, name="AvScanner.exe",
                           executable=r"C:\Program Files\Av\AvScanner.exe",
                           modules_readable=False)
    config, ports = build([opaque])
    verdict = q.run(config, ports)

    identify = verdict.phase(q.PHASE_IDENTIFY_HOLDERS)
    assert "refused a module scan" in identify.detail
    assert "300" in identify.detail
    assert verdict.exit_code == q.EXIT_QUIESCENT, (
        "an unscannable third-party process must not block every upgrade by itself"
    )


# --------------------------------------------------------------------------
# 7. a stop that is accepted and does nothing
# --------------------------------------------------------------------------


def test_an_accepted_stop_is_never_taken_as_proof_the_process_died():
    """`stop()` returns whether the REQUEST was accepted. Believing it is how a
    kill that silently did nothing looks exactly like one that worked."""
    stubborn = service(pid=401)
    config, ports = build([stubborn], refuse=[stubborn.pid])
    verdict = q.run(config, ports)

    assert stubborn.pid in ports.processes.stop_calls, "no stop was even issued"
    assert verdict.exit_code == q.EXIT_NOT_QUIESCENT
    assert verdict.phase(q.PHASE_VERIFY_CLEAR).outcome == q.OUTCOME_BLOCKED


# --------------------------------------------------------------------------
# 8. the scheduled-task restart race
# --------------------------------------------------------------------------


def test_disabling_the_task_closes_the_restart_race():
    """The registration carries RestartOnFailure, and a force-kill is precisely
    the failure it reacts to. Disabling the task disarms the trigger, so what we
    kill stays killed."""
    racer = service(pid=501)
    tasks = FakeTasks(honour_disable=True)
    config, ports = build([racer], respawn=[racer.pid], tasks=tasks)
    verdict = q.run(config, ports)

    assert tasks.disable_calls, "no owned task was disabled"
    assert not tasks.armed, "a trigger is still armed after the disable phase"
    assert verdict.exit_code == q.EXIT_QUIESCENT
    assert verdict.blockers == []


def test_ending_a_task_without_disabling_it_leaves_the_race_open():
    """The negative half, and the reason `/end` was not enough.

    `/end` stops the running instance and reports success while leaving the
    trigger armed. The scheduler then starts a replacement between the kill and
    the copy, and the upgrade — correctly — refuses.
    """
    racer = service(pid=502)
    tasks = FakeTasks(honour_disable=False)
    config, ports = build([racer], respawn=[racer.pid], tasks=tasks)
    verdict = q.run(config, ports)

    assert tasks.armed, "this scenario requires the trigger to stay armed"
    assert verdict.exit_code == q.EXIT_NOT_QUIESCENT
    assert verdict.reason == q.REASON_PROCESSES_SURVIVED
    assert blocker_pids(verdict), "a respawned process was not reported"
    assert racer.pid not in blocker_pids(verdict), (
        "the blocker should be the REPLACEMENT the scheduler started, not the "
        "process we already killed"
    )


def test_the_prior_task_state_is_recorded_so_the_upgrade_can_restore_it():
    already_off = {r"\RAGTools\Service": "Ready",
                   r"\RAGTools\Tray": "Disabled",
                   r"\RAGTools Watchdog": "Missing"}
    tasks = FakeTasks(already_off)
    config, ports = build([], tasks=tasks)
    verdict = q.run(config, ports)

    recorded = {t.name: t for t in verdict.tasks}
    assert recorded[r"\RAGTools\Service"].disabled_by_us is True
    assert recorded[r"\RAGTools\Tray"].disabled_by_us is False, (
        "a task the user had already disabled was recorded as ours to re-enable"
    )
    assert r"\RAGTools Watchdog" not in recorded, "a missing task was recorded"


def test_restore_re_enables_only_what_the_upgrade_disabled():
    """Re-enabling everything "to be safe" would silently switch autostart back
    on for someone who deliberately turned it off — a behaviour change delivered
    by an upgrade."""
    tasks = FakeTasks({r"\RAGTools\Service": "Ready", r"\RAGTools\Tray": "Disabled"})
    config, ports = build([], tasks=tasks)
    verdict = q.run(config, ports)

    restored = q.restore_tasks(verdict.tasks, tasks)
    assert restored == [r"\RAGTools\Service"]
    assert tasks.enable_calls == [r"\RAGTools\Service"]
    assert tasks.states()[r"\RAGTools\Tray"] == "Disabled"


# --------------------------------------------------------------------------
# 9. the timeout, and what it must NOT have done by then
# --------------------------------------------------------------------------


def test_the_timeout_refuses_and_says_so_distinctly():
    stubborn = service(pid=601)
    config, ports = build([stubborn], refuse=[stubborn.pid], timeout=1.0)
    verdict = q.run(config, ports)

    assert verdict.exit_code == q.EXIT_NOT_QUIESCENT
    assert verdict.reason == q.REASON_TIMEOUT, (
        "running out of time and a process refusing to die have different "
        "remedies; the verdict must say which happened"
    )
    wait = verdict.phase(q.PHASE_WAIT)
    assert wait.outcome == q.OUTCOME_BLOCKED
    assert "refusing BEFORE the first write" in wait.detail


def test_the_retry_loop_is_bounded_by_the_configured_backoff():
    stubborn = service(pid=602)
    config, ports = build([stubborn], refuse=[stubborn.pid])
    verdict = q.run(config, ports)

    assert verdict.exit_code == q.EXIT_NOT_QUIESCENT
    backoff = config.retry_backoff
    assert ports.clock.slept.count(backoff[0]) <= 1
    assert sum(1 for s in ports.clock.slept if s in backoff) <= len(backoff), (
        "the retry loop ran more attempts than its backoff schedule allows"
    )


# --------------------------------------------------------------------------
# 10. a retry that succeeds
# --------------------------------------------------------------------------


def test_a_retry_clears_a_process_that_did_not_die_first_time():
    slow = service(pid=701)
    config, ports = build([slow], attempts_needed={slow.pid: 2})
    verdict = q.run(config, ports)

    assert verdict.exit_code == q.EXIT_QUIESCENT
    retry = verdict.phase(q.PHASE_RETRY)
    assert retry.outcome == q.OUTCOME_OK
    assert "cleared after" in retry.detail
    assert ports.processes.stop_calls.count(slow.pid) >= 2


def test_a_retry_never_re_runs_the_step_that_records_the_prior_task_state():
    """`disable_tasks` is not in the retry set, and the reason is the record.

    A second pass would read the prior state as `Disabled` — because the first
    pass disabled it — and overwrite the only evidence of what to restore. A
    retry that destroys rollback information is a destructive step wearing a
    safe name.
    """
    assert q.PHASE_DISABLE_TASKS not in q.RETRYABLE_PHASES

    stubborn = service(pid=702)
    tasks = FakeTasks()
    config, ports = build([stubborn], refuse=[stubborn.pid], tasks=tasks)
    verdict = q.run(config, ports)

    assert verdict.exit_code == q.EXIT_NOT_QUIESCENT
    assert len(tasks.disable_calls) == len(set(tasks.disable_calls)), (
        f"a task was disabled more than once: {tasks.disable_calls}"
    )
    assert all(t.prior_state != "Disabled" for t in verdict.tasks
               if t.disabled_by_us), (
        "a retry overwrote the recorded prior state with the state it had just set"
    )


# --------------------------------------------------------------------------
# The replaceability probe — the step that would have caught the real failure
# --------------------------------------------------------------------------


def test_a_locked_file_refuses_the_upgrade_even_with_no_process_to_blame():
    """Process enumeration can come back clean while a handle survives.

    That is precisely the reproduced failure: nothing was visibly running, and
    `[InstallDelete]` was the first thing to discover otherwise — mid-write.
    """
    locked = APP + r"\_internal\python312.dll"
    locks = FakeLocks(DEFAULT_FILES,
                      {locked: "in use by another process - The process cannot access the file"})
    config, ports = build([], locks=locks)
    verdict = q.run(config, ports)

    assert verdict.exit_code == q.EXIT_NOT_QUIESCENT
    assert verdict.reason == q.REASON_FILES_NOT_REPLACEABLE
    assert [b.path for b in verdict.blockers] == [locked], (
        "the blocking file is not reported BY PATH"
    )
    assert verdict.blockers[0].kind == q.BLOCKER_FILE


def test_the_probe_only_covers_the_file_kinds_windows_keeps_mapped():
    locks = FakeLocks(DEFAULT_FILES)
    config, ports = build([], locks=locks)
    q.run(config, ports)

    assert locks.probed == [f for f in DEFAULT_FILES
                            if f.lower().endswith(q.REPLACEABLE_SUFFIXES)]
    assert APP + r"\_internal\base_library.zip" not in locks.probed


def test_process_blockers_and_file_blockers_are_reported_in_one_pass():
    """Fixing one blocker at a time across five failed attempts is a miserable
    way to upgrade — the same reason `run_preflight` reports rather than raises."""
    stubborn = service(pid=801)
    locked = APP + r"\ragw.exe"
    locks = FakeLocks(DEFAULT_FILES, {locked: "in use by another process"})
    config, ports = build([stubborn], refuse=[stubborn.pid], locks=locks)
    verdict = q.run(config, ports)

    kinds = {b.kind for b in verdict.blockers}
    assert kinds == {q.BLOCKER_PROCESS, q.BLOCKER_FILE}
    assert verdict.phase(q.PHASE_PROBE_REPLACEABLE).outcome == q.OUTCOME_BLOCKED


# --------------------------------------------------------------------------
# Exit-code contract, and the "changed nothing" half of the invariant
# --------------------------------------------------------------------------


def test_a_failing_port_aborts_rather_than_guessing():
    config, ports = build([], raises=True)
    verdict = q.run(config, ports)

    assert verdict.exit_code == q.EXIT_INTERNAL_ERROR
    assert verdict.reason == q.REASON_INTERNAL_ERROR
    assert verdict.quiescent is False


def test_the_three_exit_codes_are_distinct_and_1_is_left_alone():
    """1 is what an unhandled PowerShell error exits with. A considered verdict
    must never be mistakable for a crash."""
    assert sorted(q.EXIT_CODES.values()) == [0, 2, 3]
    assert 1 not in q.EXIT_CODES.values()


def test_decide_returns_exactly_the_verdicts_exit_code():
    config, ports = build([tray()])
    assert q.decide(config, ports) == q.EXIT_QUIESCENT


def test_the_policy_module_contains_no_destructive_filesystem_operation():
    """The acceptance criterion, asserted by construction.

    "The old installation must remain byte-consistent and runnable after a
    refusal" is only guaranteed if the refusing code has no way to break it. So
    this module must not be able to delete, rename, truncate or write anything —
    not as a convention, as a property of its source.
    """
    source = Path(q.__file__).read_text(encoding="utf-8")
    forbidden = [
        r"\bos\.remove\b", r"\bos\.unlink\b", r"\bos\.rename\b", r"\bos\.replace\b",
        r"\bshutil\.(rmtree|move|copy|copytree)\b",
        r"\bopen\s*\(", r"\.write_text\s*\(", r"\.write_bytes\s*\(",
        r"\.unlink\s*\(", r"\.rmdir\s*\(", r"\.mkdir\s*\(", r"\.touch\s*\(",
        r"\bsubprocess\b",
    ]
    hits = [pattern for pattern in forbidden if re.search(pattern, source)]
    assert not hits, (
        f"{Path(q.__file__).name} can modify the machine: {hits}. A protocol "
        "whose whole promise is 'nothing was changed' must not be able to change "
        "anything."
    )


def test_the_verdict_document_names_the_blockers_it_found():
    stubborn = service(pid=901)
    config, ports = build([stubborn], refuse=[stubborn.pid])
    verdict = q.run(config, ports)

    document = verdict.to_json()
    assert tuple(document) == q.VERDICT_KEYS
    assert document["exit_code"] == q.EXIT_NOT_QUIESCENT
    assert document["blockers"][0]["pid"] == stubborn.pid

    summary = "\n".join(verdict.summary_lines())
    assert "still intact" in summary
    assert str(stubborn.pid) in summary
    assert "restart" not in summary.lower(), (
        "the refusal message prescribes a reboot; it should name what to close — "
        "'restart Windows' is what an installer says when it does not know"
    )


# --------------------------------------------------------------------------
# 11. simulated post-write validation failure
# --------------------------------------------------------------------------


def _all_passing(**overrides) -> list[q.PostInstallCheck]:
    return [q.PostInstallCheck(name, overrides.get(name, True),
                               "" if overrides.get(name, True) else "simulated failure")
            for name in q.POST_INSTALL_CHECKS]


def test_a_clean_post_install_keeps_the_new_version():
    verdict = q.decide_post_install(_all_passing())
    assert verdict.ok is True
    assert verdict.restore_backup is False
    assert verdict.failed == ()


@pytest.mark.parametrize("failing", sorted(q.INTEGRITY_CHECKS))
def test_an_integrity_failure_restores_the_backup(failing):
    """A mixed PyInstaller directory is exactly what the rollback copy can undo,
    and leaving one silently is the failure this package exists to end."""
    verdict = q.decide_post_install(_all_passing(**{failing: False}))

    assert verdict.ok is False
    assert verdict.restore_backup is True
    assert failing in verdict.integrity_failures
    assert "RESTORED" in verdict.message.upper()
    assert failing in verdict.message


@pytest.mark.parametrize("failing", sorted(q.RUNTIME_CHECKS))
def test_a_runtime_failure_does_not_restore_the_backup(failing):
    """The files are correct. Rolling back would replace a correct installation
    with an older one and leave the actual cause — a storage outage, a held
    port, a rebuild that stopped early — exactly where it was."""
    verdict = q.decide_post_install(_all_passing(**{failing: False}))

    assert verdict.ok is False
    assert verdict.restore_backup is False
    assert failing in verdict.runtime_failures
    assert "rag selfcheck" in verdict.message


def test_a_check_that_did_not_run_is_not_a_check_that_passed():
    partial = [c for c in _all_passing() if c.name != q.CHECK_HEALTH_ANSWERS]
    verdict = q.decide_post_install(partial)

    assert verdict.ok is False
    assert q.CHECK_HEALTH_ANSWERS in verdict.failed
    assert verdict.restore_backup is False, (
        "an unfinished check is not evidence that anything is wrong, so it must "
        "not trigger a rollback"
    )


# --------------------------------------------------------------------------
# 12. rollback integrity
# --------------------------------------------------------------------------


def test_the_rollback_copy_lands_outside_the_directory_it_protects():
    """`[InstallDelete]` removes `{app}\\_internal` wholesale and
    `[UninstallDelete]` removes `{app}` entirely, so a copy kept underneath is
    deleted by the operations it exists to survive."""
    plan = q.plan_backup(APP, "20260801-101010")

    assert q.backup_is_outside_installation(plan) is True
    assert not q.is_under(plan.backup_dir, APP)
    assert plan.backup_dir.startswith(str(Path(APP).parent))
    assert "\\" in plan.backup_dir and "/" not in plan.backup_dir, (
        f"the plan mixes path separators: {plan.backup_dir}"
    )


def test_a_backup_kept_inside_the_installation_is_rejected():
    inside = q.BackupPlan(app_dir=APP, backup_dir=APP + r"\rollback")
    assert q.backup_is_outside_installation(inside) is False


def test_an_incomplete_rollback_copy_is_detected_before_it_is_needed():
    """A copy discovered to be incomplete AFTER `_internal` has been deleted is
    not a rollback copy — it is a second way to lose the installation."""
    plan = q.plan_backup(APP, "s")
    complete, missing = q.verify_backup(plan, ["_internal", "rag.exe", "ragw.exe"])
    assert complete and missing == []

    complete, missing = q.verify_backup(plan, ["rag.exe", "ragw.exe"])
    assert not complete
    assert missing == ["_internal"]


def test_the_payload_is_restored_before_the_executables():
    """`rag.exe` without its `_internal` IS the mixed state being undone, so it
    must never be the thing that exists first."""
    order = q.plan_rollback(q.plan_backup(APP, "s"))
    assert order[0] == "_internal"
    assert set(order) == set(q.BACKUP_ITEMS)


# --------------------------------------------------------------------------
# 13. the uninstall registry entry
# --------------------------------------------------------------------------


def test_the_uninstall_registry_entry_must_move_to_the_new_version():
    """`registry reads 3.3.0` is one of the three FAILs in the reproduced run.
    Add/Remove Programs, winget and Inno's own upgrade detection all read this
    key, so an upgrade that leaves it behind leaves the rest of the system
    describing the previous version."""
    assert q.check_uninstall_registry("3.5.1", "3.5.1").ok is True

    stale = q.check_uninstall_registry("3.3.0", "3.5.1")
    assert stale.ok is False
    assert "3.3.0" in stale.detail and "3.5.1" in stale.detail
    assert stale.name in q.INTEGRITY_CHECKS

    assert q.check_uninstall_registry(None, "3.5.1").ok is False


# --------------------------------------------------------------------------
# 14. final executable / DLL consistency
# --------------------------------------------------------------------------


def test_a_half_deleted_payload_is_detected_by_name():
    """The observed signature: `rag.exe` survives and `python312.dll` does not,
    and the only thing the user sees is `[PYI-2072]`."""
    intact = q.check_binary_consistency(
        [APP + r"\rag.exe", APP + r"\ragw.exe", APP + r"\_internal\python312.dll"])
    assert intact.ok is True

    mixed = q.check_binary_consistency([APP + r"\rag.exe", APP + r"\ragw.exe"])
    assert mixed.ok is False
    assert "python312.dll" in mixed.detail
    assert mixed.name in q.INTEGRITY_CHECKS


def test_a_similarly_named_file_does_not_satisfy_the_consistency_check():
    """`myrag.exe` is not `rag.exe`. A suffix match without a separator anchor
    would have said otherwise."""
    result = q.check_binary_consistency([APP + r"\myrag.exe"], required=["rag.exe"])
    assert result.ok is False


# --------------------------------------------------------------------------
# Ownership, stated directly
# --------------------------------------------------------------------------


def test_a_neighbouring_directory_is_not_part_of_this_installation():
    """The boundary is a separator. A bare prefix comparison is how
    `RAGTools-rollback-...` gets swept into the upgrade of `RAGTools`."""
    assert q.is_under(APP + r"\rag.exe", APP) is True
    assert q.is_under(APP + "-rollback-20260801\\rag.exe", APP) is False
    assert q.is_under(APP, APP) is True


def test_qdrant_is_never_owned_by_its_name_alone():
    foreign = engine(pid=1, executable=r"D:\services\qdrant\qdrant.exe")
    assert q.ownership_evidence(foreign, APP, DATA) == ()
    assert q.owns(foreign, APP, DATA) is False

    ours = engine(pid=2, executable=DATA + r"\bin\qdrant.exe")
    assert q.EVIDENCE_EXECUTABLE_PATH in q.ownership_evidence(ours, APP, DATA)


def test_our_own_image_names_are_owned_wherever_they_run():
    """`rag.exe` and `ragw.exe` are names nothing else on Windows ships, and
    matching them by name is PROVEN: v3.0.1 replaced a running `rag.exe` in
    place using exactly that. A stray copy running from somewhere else still
    holds our DLLs."""
    stray = q.ProcessInfo(pid=3, name="rag.exe", executable=r"C:\elsewhere\rag.exe")
    assert q.EVIDENCE_IMAGE_NAME in q.ownership_evidence(stray, APP, DATA)
