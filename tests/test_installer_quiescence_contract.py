"""WP-R02 — the installer must be able to REFUSE, and must refuse in one place.

`installer.iss` and `installer/quiesce.ps1` are not covered by any Python test
that imports them, and the 3.0.0 and 3.3.0 incidents were both entirely
installer incidents. So the properties are asserted structurally, over the
scripts themselves.

Two things are being held here.

**One: Setup has to have a way to say no.** `[InstallDelete]` removes
`{app}\\_internal` wholesale and that is the first destructive write of the whole
installation. Until v3.5.1 the pre-install protocol ran in
`CurStepChanged(ssInstall)`, which cannot abort cleanly — so a still-locked file
did not become a refusal, it became Inno's file-in-use Abort/Retry/Ignore box,
answered **Abort** by `/SUPPRESSMSGBOXES`, exiting **5** with the payload half
deleted. `PrepareToInstall()` runs before `[InstallDelete]` and `[Files]`, and a
non-empty return makes Setup exit **7** having written nothing:

    exit 7   refused safely; the old installation is intact and runnable
    exit 5   aborted mid-write; the installation is now mixed

**Two: the decision lives in ONE place.** The OS work is PowerShell and the
policy is Python, so the phase list, the exit-code mapping and the verdict shape
are declared exactly once on each side and compared here — *parsed*, not
grepped. A hand-maintained list that can silently disagree is two sources of
truth wearing one name.

Everything in this module is static analysis of two text files. It runs on any
platform; nothing here needs Windows.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "installer.iss"
QUIESCE = ROOT / "installer" / "quiesce.ps1"

#: A task path no machine can have registered. Used to ask the REAL
#: `Get-TaskState` the question that broke the 3.5.1 upgrade.
UNREGISTERED_TASK = r"\RAGToolsQuiescenceProbe\NoSuchTask"

#: Sections whose entries Inno executes as file operations. Both run AFTER
#: `PrepareToInstall`, which is the ordering guarantee this module exists to
#: keep true.
DESTRUCTIVE_SECTIONS = ("InstallDelete", "Files")

#: Pascal calls that modify the machine's files.
DESTRUCTIVE_CALLS = ("DelTree(", "DeleteFile(", "FileCopy(", "RenameFile(",
                     "robocopy")


# --------------------------------------------------------------------------
# Loading. Every accessor asserts, so a missing implementation fails as a
# statement about the product rather than as a collection error.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def iss() -> str:
    assert INSTALLER.is_file(), f"{INSTALLER} is missing"
    return INSTALLER.read_text(encoding="utf-8")


def quiesce_source() -> str:
    assert QUIESCE.is_file(), (
        f"{QUIESCE.relative_to(ROOT)} does not exist. The upgrade has no single "
        "authoritative quiescence implementation: the pre-install protocol is "
        "spread across Pascal in `CurStepChanged(ssInstall)` and PowerShell "
        "one-liners embedded in it, none of which can prove a file is "
        "replaceable and none of which can refuse the installation."
    )
    return QUIESCE.read_text(encoding="utf-8")


def quiesce_code() -> str:
    """`quiesce.ps1` with its comments blanked out, line count preserved.

    The sibling suite learned this the hard way: `[Code]` is Pascal and uses
    `//`, which its section helper never stripped, so two structural assertions
    passed on a comment that quoted the very line they were written to forbid.
    This script explains `/end` at length in the doc block of the function that
    replaced it — exactly the shape of that trap.
    """
    source = quiesce_source()
    source = re.sub(r"<#.*?#>", lambda m: "\n" * m.group(0).count("\n"), source,
                    flags=re.DOTALL)
    return "\n".join("" if line.strip().startswith("#") else line
                     for line in source.splitlines())


def code_section(script: str) -> str:
    """The `[Code]` body with whole-line Pascal comments stripped.

    Comments are removed deliberately: every assertion below must hold because
    of what the installer DOES, never because the desired string appears in the
    prose explaining the bug. (Two tests in the sibling suite once passed on a
    comment quoting the very line they were written to forbid.)
    """
    match = re.search(r"^\[Code\]$(.*)\Z", script, re.MULTILINE | re.DOTALL)
    assert match, "installer.iss has no [Code] section"
    return "\n".join(line for line in match.group(1).splitlines()
                     if not line.strip().startswith("//"))


def ini_section(script: str, name: str) -> str:
    match = re.search(rf"^\[{name}\]$(.*?)(?=^\[|\Z)", script,
                      re.MULTILINE | re.DOTALL)
    if match is None:
        return ""
    return "\n".join(line for line in match.group(1).splitlines()
                     if not line.strip().startswith(";"))


def pascal_body(code: str, name: str) -> str:
    """One routine's source, definition only — never a `forward;` declaration."""
    bodies = [m.group(0) for m in
              re.finditer(rf"(?:function|procedure)\s+{name}\b.*?^end;", code,
                          re.DOTALL | re.MULTILINE)
              if "forward;" not in m.group(0).splitlines()[0]]
    return bodies[0] if bodies else ""


# --- structural extraction from the PowerShell -----------------------------


def ps_array(script: str, name: str) -> list[str]:
    """A declared array: `$Script:<name> = @( 'a', 'b' )` -> ['a', 'b'].

    Extracted from the DECLARATION, so the test reads what the script says
    rather than checking whether the script contains what the test expects.
    """
    match = re.search(rf"\$Script:{name}\s*=\s*@\((.*?)\)", script, re.DOTALL)
    assert match, f"quiesce.ps1 does not declare $Script:{name}"
    return re.findall(r"'([^']*)'", match.group(1))


def ps_hashtable(script: str, name: str) -> dict:
    match = re.search(rf"\$Script:{name}\s*=\s*@\{{(.*?)\}}", script, re.DOTALL)
    assert match, f"quiesce.ps1 does not declare $Script:{name}"
    return {key: int(value)
            for key, value in re.findall(r"(\w+)\s*=\s*(-?\d+)", match.group(1))}


def ps_scalar(script: str, name: str) -> str:
    match = re.search(rf"\$Script:{name}\s*=\s*'([^']*)'", script)
    assert match, f"quiesce.ps1 does not declare $Script:{name}"
    return match.group(1)


def ps_braced_block(script: str, opener: str) -> str:
    """The text between a literal opener ending in `{` and its matching `}`."""
    assert opener in script, f"quiesce.ps1 has no `{opener}`"
    start = script.index(opener) + len(opener)
    depth, i = 1, start
    while i < len(script) and depth:
        if script[i] == "{":
            depth += 1
        elif script[i] == "}":
            depth -= 1
        i += 1
    return script[start:i - 1]


def declarations_and_functions(script: str) -> str:
    """`quiesce.ps1` with its `param()` block and its main body removed.

    What is left is the error preference, the `$Script:` declarations and every
    function — definitions only, so loading it EXECUTES NOTHING. The `param()`
    block goes because its parameters are `Mandatory`, and a mandatory parameter
    with no value makes PowerShell prompt, which in a test run means hang. The
    main body goes because it stops processes.

    Deliberately the real file rather than a paraphrase of it: a copy of
    `Get-TaskState` maintained in this test could be fixed here and left broken
    there, which is the failure mode the whole module exists to prevent.
    """
    start = script.index("$ErrorActionPreference")
    end = script.index("if ($Mode -eq 'Restore')")
    return script[start:end]


def run_powershell(body: str, tmp_path: Path,
                   interpreter: str = "powershell.exe") -> subprocess.CompletedProcess:
    probe = tmp_path / f"probe-{abs(hash(interpreter)) % 10**8}.ps1"
    probe.write_text(body, encoding="utf-8")
    return subprocess.run(
        [interpreter, "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-File", str(probe)],
        capture_output=True, text=True, timeout=180)


def powershell_hosts() -> list[tuple[str, str]]:
    """Every Windows PowerShell this script can actually be launched by.

    Not a detail: ``installer.iss`` declares no 64-bit architecture, so Inno's
    Setup binary is 32-bit and its ``Exec('powershell.exe', ...)`` resolves
    through WOW64 to ``SysWOW64\\WindowsPowerShell`` — the 32-bit host. Any
    property of this script that only holds under the 64-bit host is a property
    the shipped installer does not have.
    """
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    found = []
    for label, folder in (("64-bit", "System32"), ("32-bit", "SysWOW64")):
        exe = root / folder / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if exe.is_file():
            found.append((label, str(exe)))
    return found


def invoked_phases(script: str) -> list[str]:
    """Phases the script actually DISPATCHES, in order.

    One dispatcher (`Invoke-Phase -Name '<x>'`) is what makes the declared list
    and the executed list comparable at all — without it, `$Script:Phases` would
    be documentation that nothing enforces.
    """
    return re.findall(r"Invoke-Phase\s+-Name\s+'([^']+)'", script)


# ==========================================================================
# 1. The protocol runs from PrepareToInstall, before anything is written
# ==========================================================================


def test_the_quiescence_protocol_runs_from_prepare_to_install(iss):
    """The whole package, in one assertion.

    `CurStepChanged(ssInstall)` also runs before `[InstallDelete]` — that was
    never the problem. The problem is that it cannot REFUSE: there is no return
    value Setup acts on, so a pre-install phase that discovers a locked file has
    no way to stop the installation cleanly, and the first thing that reacts to
    the lock is the delete itself, mid-write, exiting 5 over a half-removed
    `_internal`.

    `PrepareToInstall` returns a String. A non-empty one makes Setup fail with
    exit 7 — "Preparing to Install determined Setup cannot proceed" — WITHOUT
    modifying a single file.
    """
    code = code_section(iss)
    body = pascal_body(code, "PrepareToInstall")
    assert body, (
        "installer.iss has no PrepareToInstall(). The pre-install protocol runs "
        "from CurStepChanged(ssInstall), which has no way to refuse: every "
        "blocker it can see becomes a failure discovered mid-write (exit 5, "
        "`_internal` partially deleted) instead of a refusal (exit 7, nothing "
        "touched)."
    )
    assert re.match(r"function\s+PrepareToInstall\s*\(\s*var\s+NeedsRestart\s*:\s*Boolean\s*\)\s*:\s*String",
                    body), (
        "PrepareToInstall does not have the signature Inno calls, so it is never "
        "invoked and the gate does not exist"
    )
    assert "RunQuiescence(" in body, (
        "PrepareToInstall does not run the quiescence protocol"
    )


def test_the_destructive_sections_exist_and_are_what_the_gate_precedes(iss):
    """Names the steps whose ordering the gate is about.

    If `[InstallDelete]` stopped removing `{app}\\_internal`, the gate would
    still be correct but this suite would be asserting something about nothing —
    so the destructive step is pinned here too.
    """
    delete = ini_section(iss, "InstallDelete")
    files = ini_section(iss, "Files")
    assert re.search(r"\{app\}\\_internal", delete), (
        "[InstallDelete] no longer removes {app}\\_internal — the first "
        "destructive write this gate exists to precede"
    )
    assert re.search(r"DestDir:\s*\"\{app\}\"", files), (
        "[Files] no longer extracts into {app}"
    )


def test_no_destructive_pascal_runs_before_the_gate_can_refuse(iss):
    """`PrepareToInstall` must be able to say "nothing has been changed" and be
    telling the truth.

    Its refusal branch therefore has to come before anything that writes. The
    only write on this path is the rollback COPY, which is deliberately
    sequenced after the refusal — and which writes to a sibling directory, never
    into `{app}`.
    """
    code = code_section(iss)
    body = pascal_body(code, "PrepareToInstall")
    assert body, "PrepareToInstall is gone"

    refusal = body.find("Exit;")
    assert refusal != -1, "PrepareToInstall has no early exit, so it cannot refuse"

    prefix = body[:refusal]
    offenders = [call for call in DESTRUCTIVE_CALLS if call in prefix]
    assert not offenders, (
        f"PrepareToInstall performs {offenders} before it has decided whether to "
        "refuse, so a refusal would no longer mean 'nothing has been changed'"
    )


def test_the_pre_install_work_happens_in_exactly_one_place(iss):
    """One protocol, not two.

    `CurStepChanged(ssInstall)` used to run its own stop-and-kill sequence:
    a bounded `service stop`, `StopOwnedTasks()`, `ForceKillRagProcesses()`.
    Keeping that alongside the gate would mean two pre-install phases doing the
    same job, and the one that CANNOT refuse would still be the one deciding
    whether the install proceeds — which is the defect, not a backstop against
    it.

    Those two procedures still exist and are still called: by the UNINSTALLER,
    where the same ordering lesson applies and where refusing is not an option.
    """
    code = code_section(iss)
    body = pascal_body(code, "CurStepChanged")
    assert body, "CurStepChanged is gone"

    offenders = [call for call in DESTRUCTIVE_CALLS if call in body]
    assert not offenders, (
        f"CurStepChanged performs {offenders} — a destructive step at a point "
        "where Setup can no longer refuse cleanly"
    )
    for procedure in ("StopOwnedTasks()", "ForceKillRagProcesses()"):
        assert procedure not in body, (
            f"CurStepChanged still calls {procedure}, so the pre-install protocol "
            "runs a SECOND time from ssInstall — where there is no return value "
            "Setup acts on, and therefore no way to refuse. That second copy is "
            "the one that turns a locked file into an Abort mid-delete (exit 5) "
            "instead of a clean refusal (exit 7)."
        )
    assert "VerifyInstallation()" in body, (
        "CurStepChanged no longer verifies the installation at ssDone"
    )


# ==========================================================================
# 2. The refusal path returns a non-empty string, so nothing is written
# ==========================================================================


def test_a_refusal_returns_a_non_empty_message_naming_what_to_close(iss):
    """Exit 7 is only reachable through a non-empty return.

    And the message matters as much as the code: "restart Windows" is what an
    installer says when it does not know which process is holding the file. The
    protocol knows — it reports blockers by PID and by path — so the message
    names them.
    """
    code = code_section(iss)
    body = pascal_body(code, "PrepareToInstall")
    assert body, "PrepareToInstall is gone"

    failure = re.search(r"if\s+Code\s*<>\s*0\s+then(.*?)\bExit;", body, re.DOTALL)
    assert failure, (
        "nothing in PrepareToInstall branches on the protocol's exit code, so a "
        "refusal cannot reach Setup"
    )
    branch = failure.group(1)
    assert re.search(r"Result\s*:=", branch), (
        "the failure branch does not set a return value; an empty Result tells "
        "Setup to PROCEED, which is the opposite of refusing"
    )
    assert "QuiesceBlockerText" in branch, (
        "the refusal message does not include the blockers the protocol found, so "
        "the user is told that something is wrong but not what to close"
    )
    assert "NOTHING HAS BEEN CHANGED" in branch, (
        "the refusal does not tell the user their existing installation survived "
        "— which is the entire difference between exit 7 and exit 5"
    )


def test_a_failed_rollback_copy_also_refuses_rather_than_proceeding(iss):
    """The copy is verified while refusing is still free.

    A rollback copy discovered to be incomplete AFTER `_internal` has been
    deleted is not a rollback copy; it is a second way to lose the installation.
    """
    code = code_section(iss)
    body = pascal_body(code, "PrepareToInstall")
    assert re.search(r"if\s+not\s+BackupInstallation\(\)\s+then", body), (
        "PrepareToInstall does not refuse when the rollback copy cannot be made"
    )

    backup = pascal_body(code, "BackupInstallation")
    assert backup, "BackupInstallation is gone"
    assert re.search(r"Result\s*:=\s*FileExists\(", backup), (
        "the rollback copy is never verified, so an incomplete one is indis"
        "tinguishable from a good one until it is needed"
    )


def test_the_rollback_copy_lands_beside_the_installation_not_inside_it(iss):
    """`[InstallDelete]` removes `{app}\\_internal` and `[UninstallDelete]`
    removes `{app}` entirely, so a copy kept underneath is deleted by the very
    operations it exists to survive — the mistake `BackupConfig` already avoids
    for `config.toml`."""
    code = code_section(iss)
    backup = pascal_body(code, "BackupInstallation")
    # Not the first assignment: the routine clears `RollbackDir := '';` before
    # it decides there is anything to copy. Taking assignment number one would
    # have tested the reset, not the path.
    assignments = [m.group(1).strip() for m in
                   re.finditer(r"RollbackDir\s*:=\s*(.+?);", backup, re.DOTALL)]
    built = [a for a in assignments if a.strip("'\"")]
    assert built, "BackupInstallation no longer names a rollback directory"
    expression = built[0]

    assert "AddBackslash(Source)" not in expression, (
        f"the rollback copy is placed INSIDE the installation: {expression.strip()}"
    )
    assert "Source +" in expression and "-rollback-" in expression, (
        f"the rollback directory is not a sibling of {{app}}: {expression.strip()}"
    )


# ==========================================================================
# 3. quiesce.ps1 and the Python policy declare the SAME protocol
# ==========================================================================


def test_the_two_implementations_declare_the_same_phases():
    """Parsed from both declarations, never listed here.

    A third hand-maintained copy in this file would be exactly the drift the
    test is written to prevent.
    """
    script = quiesce_source()
    from ragtools.upgrade import quiescence as policy

    declared = ps_array(script, "Phases")
    assert declared == list(policy.PHASES), (
        "quiesce.ps1 and ragtools.upgrade.quiescence disagree about the protocol:\n"
        f"  PowerShell: {declared}\n"
        f"  Python:     {list(policy.PHASES)}"
    )


def test_the_powershell_executes_exactly_the_phases_it_declares():
    """Otherwise `$Script:Phases` is documentation, and documentation cannot
    drift into being wrong slowly enough to notice."""
    script = quiesce_code()
    declared = ps_array(script, "Phases")
    executed = invoked_phases(script)

    assert executed == declared, (
        "the phases quiesce.ps1 runs are not the phases it declares:\n"
        f"  declared: {declared}\n"
        f"  executed: {executed}"
    )


def test_the_two_implementations_agree_on_the_exit_codes():
    script = quiesce_source()
    from ragtools.upgrade import quiescence as policy

    declared = ps_hashtable(script, "ExitCodes")
    assert declared == dict(policy.EXIT_CODES), (
        f"exit-code mapping disagrees:\n  PowerShell: {declared}\n"
        f"  Python:     {dict(policy.EXIT_CODES)}"
    )
    assert declared["quiescent"] == 0
    assert declared["not_quiescent"] == 2
    assert 1 not in declared.values(), (
        "1 is what an unhandled PowerShell error exits with; a considered verdict "
        "must never be mistakable for a crash"
    )


def test_the_two_implementations_agree_on_what_may_be_retried():
    script = quiesce_source()
    from ragtools.upgrade import quiescence as policy

    declared = ps_array(script, "RetryablePhases")
    assert declared == list(policy.RETRYABLE_PHASES)
    assert "disable_tasks" not in declared, (
        "re-running disable_tasks would read the prior state as Disabled — "
        "because the first pass disabled it — and overwrite the only evidence of "
        "what to restore"
    )


def test_the_two_implementations_agree_on_the_restore_phase_and_schema():
    script = quiesce_source()
    from ragtools.upgrade import quiescence as policy

    assert ps_scalar(script, "RestorePhase") == policy.RESTORE_PHASE
    assert ps_scalar(script, "Schema") == policy.SCHEMA


def test_the_two_implementations_emit_the_same_verdict_document():
    script = quiesce_source()
    from ragtools.upgrade import quiescence as policy

    block = ps_braced_block(script, "$verdict = [ordered]@{")
    keys = re.findall(r"^\s*(\w+)\s*=", block, re.MULTILINE)
    assert keys == list(policy.VERDICT_KEYS), (
        "the JSON verdict shapes differ:\n"
        f"  PowerShell: {keys}\n  Python:     {list(policy.VERDICT_KEYS)}"
    )


def test_the_two_implementations_agree_on_which_names_are_owned():
    script = quiesce_source()
    from ragtools.upgrade import quiescence as policy

    assert ps_array(script, "OwnedImageNames") == list(policy.OWNED_IMAGE_NAMES)
    assert ps_array(script, "PathScopedImageNames") == list(policy.PATH_SCOPED_IMAGE_NAMES)
    assert ps_array(script, "ReplaceableSuffixes") == list(policy.REPLACEABLE_SUFFIXES)
    assert ps_array(script, "OwnedTasks") == list(policy.OWNED_TASKS)


# ==========================================================================
# The behaviours the PowerShell adds over the previous implementation
# ==========================================================================


def test_the_owned_tasks_are_disabled_not_merely_ended():
    """`/end` stops the running instance and leaves the TRIGGER ARMED.

    The registration carries RestartOnFailure and a force-kill is precisely the
    failure it reacts to, so the scheduler is free to start a replacement
    between the kill and the copy. Ending a task does not close that race;
    disabling it does.
    """
    script = quiesce_code()
    body = re.search(r"function Invoke-DisableTasks\b.*?\n\}", script, re.DOTALL)
    assert body, "quiesce.ps1 has no task-disabling phase"
    text = body.group(0)

    assert "/disable" in text, (
        "the owned scheduled tasks are not DISABLED for the upgrade window, so "
        "the scheduler can restart what the upgrade just killed"
    )
    assert "/end" not in text, (
        "the tasks are merely ended; `/end` leaves the trigger armed"
    )
    assert "prior_state" in text, (
        "the prior task state is not recorded, so the upgrade cannot restore it "
        "and a user's autostart would silently stay off"
    )


def test_holders_are_found_by_loaded_module_not_only_by_image_name():
    """The reproduced failure, as a property of the enumerator.

    `Get-Process -Name rag,ragw,qdrant` — the only filter the previous
    implementation had — is blind to a process called anything else. One that
    has MAPPED `_internal\\python312.dll` holds the file exactly as hard, and
    `[InstallDelete]` is the first thing that finds out.
    """
    script = quiesce_code()
    body = re.search(r"function Get-OwnedProcess\b.*?\n\}", script, re.DOTALL)
    assert body, "quiesce.ps1 has no process-ownership enumerator"
    text = body.group(0)

    assert re.search(r"\$proc\.Modules", text), (
        "processes are not inspected for LOADED MODULES, so a holder with an "
        "unrelated image name is invisible — which is exactly how "
        "`[PYI-2072] Failed to load Python DLL` happened"
    )
    assert "loaded_module_under_installation" in text
    assert re.search(r"catch\s*\{", text), (
        "a module scan that is denied is not handled, so an access denial would "
        "abort the enumeration instead of being recorded"
    )
    assert "modulesReadable" in text.replace("ModulesReadable", "modulesReadable"), (
        "an access-denied module scan is dropped rather than recorded; a process "
        "we could not inspect is not evidence of quiescence"
    )


def test_a_pre_write_replaceability_probe_proves_the_files_can_be_replaced():
    """Process enumeration can come back clean while a handle survives.

    So the proof is not "nothing looks like it is holding this" but "this file
    opens exclusively" — the same access the delete and the overwrite need. The
    first thing that establishes it today is `[InstallDelete]`, mid-write.
    """
    script = quiesce_code()
    body = re.search(r"function Test-Replaceable\b.*?\n\}", script, re.DOTALL)
    assert body, (
        "quiesce.ps1 has no pre-write replaceability probe, so quiescence is "
        "inferred from process enumeration alone"
    )
    text = body.group(0)

    assert "FileShare]::None" in text, (
        "the probe does not open the file EXCLUSIVELY, so it would succeed "
        "against a file another process still holds"
    )
    assert "Dispose()" in text, "the probe leaks the handle it opens"

    probe = re.search(r"function Invoke-ProbeReplaceable\b.*?\n\}", script, re.DOTALL)
    assert probe, "nothing calls the replaceability probe"
    assert "Kind 'file'" in probe.group(0), (
        "an unreplaceable file is not reported as a blocker by path"
    )


def test_the_script_starts_no_unbounded_child_process():
    """It runs the PREVIOUS release's `rag.exe` — any version ever published,
    whose behaviour when its own service is wedged is not knowable from here.
    Observed once: a silent upgrade over a running v2.7.0 produced no output and
    did not finish in forty minutes."""
    script = quiesce_code()

    helper = re.search(r"function Invoke-Bounded\b.*?\n\}", script, re.DOTALL)
    assert helper, "quiesce.ps1 has no bounded process helper"
    assert "WaitForExit(" in helper.group(0), "the helper does not wait with a timeout"
    assert ".Kill()" in helper.group(0), "the helper never kills a process that overruns"

    starts = re.findall(r"Start-Process[^\n]*", script)
    assert starts, "nothing starts a child process at all"
    for line in starts:
        assert "-Wait" not in line, f"an unbounded child process is started: {line.strip()}"


def test_the_protocol_cannot_modify_the_installation_it_is_judging():
    """The acceptance criterion, as a property of the source.

    "The old installation must remain byte-consistent and runnable after a
    refusal" is only guaranteed if the refusing code has no way to break it. The
    only files this script writes are its own log, its blocker summary and its
    task record, all under the log directory.
    """
    script = quiesce_code()
    writes = re.findall(
        r"(?:Remove-Item|Set-Content|Out-File|Move-Item|New-Item|WriteAllText|"
        r"WriteAllBytes|WriteAllLines)[^\n]*", script)
    assert writes, (
        "no write of any kind was found; either the verdict is no longer "
        "recorded, or this test has stopped recognising how it is written"
    )
    for line in writes:
        assert "$AppDir" not in line, (
            f"quiesce.ps1 writes to the installation directory: {line.strip()}"
        )
        assert "Remove-Item" not in line, (
            f"quiesce.ps1 deletes something: {line.strip()}"
        )


# ==========================================================================
# The 3.5.1 defect: a task that is merely not registered aborted the protocol
# ==========================================================================


@pytest.mark.skipif(sys.platform != "win32",
                    reason="Windows PowerShell is the interpreter under test")
def test_a_scheduled_task_that_is_not_registered_is_reported_not_thrown(tmp_path):
    """THE defect that made every 3.5.1 upgrade refuse, asked of the real script.

    In Windows PowerShell, what a native command writes to stderr becomes an
    ErrorRecord the moment a redirection operator is applied to stream 2 — and
    under ``$ErrorActionPreference = 'Stop'``, which this script sets on its
    first line, that ErrorRecord is TERMINATING. ``schtasks /query`` announces an
    absent task on stderr, and an absent task is the NORMAL case for at least one
    of ``$Script:OwnedTasks`` on every release the matrix upgrades from:
    ``\\RAGTools Watchdog`` is v2's and v3 removes it, while a v2 machine has
    neither ``\\RAGTools\\Service`` nor ``\\RAGTools\\Tray``.

    So ``Get-TaskState`` could never return ``'Missing'``: it threw, the phase
    dispatcher rethrew, and the protocol aborted at ``disable_tasks`` — before
    ``stop_tray``, before the supervisor stop, before the proven
    ``taskkill /F /IM ... /T``, before the engine stop. Setup then refused with
    exit 7 having correctly written nothing, which is why the safety half looked
    healthy while the previous version was still serving with every one of its
    processes alive.

    Executed, not grepped. The interpreter's behaviour is the thing that was
    wrong, and no amount of reading the source reveals it.
    """
    body = declarations_and_functions(quiesce_source()) + f"""
try {{
    $state = Get-TaskState -Name '{UNREGISTERED_TASK}'
    Write-Output "STATE=$state"
}}
catch {{
    Write-Output "THREW=$($_.Exception.GetType().Name): $($_.Exception.Message)"
}}
"""
    result = run_powershell(body, tmp_path)
    output = (result.stdout or "") + (result.stderr or "")

    assert "THREW=" not in output, (
        "Get-TaskState THROWS for a scheduled task that is simply not "
        f"registered:\n  {output.strip()}\n"
        "Under $ErrorActionPreference = 'Stop', a redirection applied to a "
        "native command's stderr makes that stderr terminating. disable_tasks "
        "therefore aborts the whole protocol before a single process is stopped, "
        "and the upgrade refuses with the previous version still running."
    )
    assert "STATE=Missing" in output, (
        f"an unregistered task is not reported as Missing: {output.strip()!r}"
    )


def test_no_native_command_can_turn_its_own_stderr_into_a_terminating_error():
    """The general form of the defect above, as a property of the source.

    ``$ErrorActionPreference = 'Stop'`` is deliberate and stays. What must not
    happen again is a raw native invocation that redirects stream 2 while it is
    in force — so every native command that needs its output read goes through
    ``Invoke-Native``, which neutralises the preference around that one call.
    """
    script = quiesce_code()

    helper = re.search(r"function Invoke-Native\b.*?\n\}", script, re.DOTALL)
    assert helper, (
        "quiesce.ps1 has no guarded native-command helper, so a native command's "
        "stderr is once again free to abort the protocol"
    )
    assert "$ErrorActionPreference = 'Continue'" in helper.group(0), (
        "Invoke-Native does not neutralise the error preference, which is the "
        "only thing that stops native stderr being terminating"
    )

    offenders = [line.strip() for line in script.splitlines()
                 if re.search(r"^\s*(?:\$\w+\s*=\s*)?&\s*[\"'$]", line)
                 and re.search(r"\b2>(?:&1|\$null)", line)]
    assert not offenders, (
        "a native command is invoked with its stderr redirected while "
        "$ErrorActionPreference = 'Stop' is in force, which makes that stderr a "
        f"TERMINATING error: {offenders}. Use Invoke-Native."
    )


def test_a_preparatory_phase_that_fails_does_not_skip_every_stop():
    """One dispatcher, two kinds of phase, and the distinction is declared.

    A phase that only HELPS reach quiescence failing proves nothing — and must
    not be able to skip the phases that do the stopping and the proving. Only a
    phase whose failure could make the script wrongly conclude quiescence aborts
    it: `detect_installation` (an error read as "nothing installed" would let
    Setup delete a live installation) and the two that decide.
    """
    script = quiesce_code()

    decisive = ps_array(script, "DecisivePhases")
    declared = ps_array(script, "Phases")
    assert decisive, "quiesce.ps1 no longer distinguishes decisive from advisory phases"
    assert set(decisive) <= set(declared), (
        f"a decisive phase is not in the protocol at all: {decisive}"
    )
    for required in ("detect_installation", "verify_clear", "probe_replaceable"):
        assert required in decisive, (
            f"{required} is advisory, so an error in it would be recorded and the "
            "protocol would carry on — for detect_installation that means "
            "concluding a live installation is a fresh one and returning exit 0"
        )
    for advisory in ("disable_tasks", "stop_tray", "stop_service_children"):
        assert advisory not in decisive, (
            f"{advisory} aborts the whole protocol when it fails, which is how "
            "one unregistered scheduled task stopped 3.5.1 from ever running its "
            "kill"
        )

    dispatcher = re.search(r"function Invoke-Phase\b.*?\n\}", script, re.DOTALL)
    assert dispatcher, "quiesce.ps1 has no phase dispatcher"
    assert "$Script:DecisivePhases -contains $Name" in dispatcher.group(0), (
        "the dispatcher rethrows for every phase, so an advisory failure is "
        "still an abort"
    )


def test_the_harness_reads_the_verdict_where_the_installer_writes_it(iss):
    """A refusal nobody can read is a refusal nobody can act on.

    `installer.iss` decides where `quiesce.ps1 -LogPath` points and
    `scripts/verify_upgrade_install.py` is what turns that file into CI output.
    If those two ever name different directories, an exit-7 leg reports a
    refusal with no cause attached and the next person has to reproduce the
    whole 48-minute matrix to find out why.
    """
    code = code_section(iss)
    directory = pascal_body(code, "QuiesceLogDirectory")
    assert directory, "installer.iss no longer decides where the verdict is written"
    match = re.search(r"ExpandConstant\('([^']+)'\)", directory)
    assert match, f"the log directory is not a constant path: {directory}"
    installer_path = match.group(1)          # {localappdata}\RAGTools\logs

    harness = (ROOT / "scripts" / "verify_upgrade_install.py").read_text(encoding="utf-8")
    declared = re.search(r"QUIESCE_LOG_DIR\s*=\s*(.+)", harness)
    assert declared, (
        "the harness no longer names the quiescence log directory, so it cannot "
        "report what a refusal found"
    )
    tail = installer_path.replace("{localappdata}", "").strip("\\/")
    for segment in tail.split("\\"):
        assert f'"{segment}"' in declared.group(1), (
            f"the harness reads {declared.group(1).strip()} but the installer "
            f"writes to {installer_path}; the segment {segment!r} is missing"
        )

    # And it reads the STRUCTURED verdict, not only the summary. The summary is
    # written for Inno's dialog and carries the blockers alone, so a protocol
    # that aborted before it looked for any produces an empty list — which is
    # exactly how a refusal came to print `Blockers:` and nothing else. The
    # phases are the part that says where it stopped.
    assert "QUIESCE_JSON" in harness and '"phases"' in harness, (
        "the harness reports only the blocker summary; a run that aborted before "
        "identifying any holder would print an empty list and no cause"
    )


def test_the_two_implementations_agree_on_which_phases_are_decisive():
    script = quiesce_source()
    from ragtools.upgrade import quiescence as policy

    declared = ps_array(script, "DecisivePhases")
    assert declared == list(policy.DECISIVE_PHASES), (
        "the two implementations disagree about which failures abort:\n"
        f"  PowerShell: {declared}\n  Python:     {list(policy.DECISIVE_PHASES)}"
    )


def test_a_fresh_install_short_circuits_the_whole_protocol():
    script = quiesce_code()
    detect = re.search(r"function Invoke-DetectInstallation\b.*?\n\}", script, re.DOTALL)
    assert detect, "quiesce.ps1 does not detect an existing installation"
    assert "no previous installation" in detect.group(0)
    assert re.search(r"if\s*\(-not\s+\$Script:Installation\['present'\]\)", script), (
        "the protocol does not short-circuit when there is nothing to replace; a "
        "fresh install must not pay for any of this"
    )


# ==========================================================================
# The reasoning this project treats as load-bearing
# ==========================================================================


def test_the_image_name_versus_path_scoping_reasoning_survives():
    """Deliberately asserted on the COMMENTS.

    This is the one place where prose is the artifact. The asymmetry — image
    names for `rag.exe`/`ragw.exe`, path scoping for `qdrant.exe` — looks
    arbitrary and inconsistent, and has twice been "tidied up" into a uniform
    rule that broke one half or the other. Both halves cost a CI cycle and one
    of them cost a field incident. The explanation is what stops it happening a
    third time.

    A preservation test: its job is to fail the day someone deletes the
    reasoning, not to prove anything was added. (Against the pre-change tree it
    fails only because `quiesce.ps1` does not exist yet — the `installer.iss`
    half of the assertion already held there, which is exactly the reasoning
    being carried across.)
    """
    quiesce = quiesce_source()
    installer = INSTALLER.read_text(encoding="utf-8")

    for name, text in (("quiesce.ps1", quiesce), ("installer.iss", installer)):
        assert "PROVEN" in text or "proven" in text, (
            f"{name} no longer records that image-name matching for rag.exe was "
            "measured to work where a path-scoped equivalent failed"
        )
        assert "external" in text, (
            f'{name} no longer explains why qdrant.exe is path-scoped: '
            'storage_backend = "external" means a server the user runs'
        )
        assert "data loss" in text, (
            f"{name} no longer states the consequence of killing a Qdrant this "
            "installation does not own"
        )

    assert "DeleteFile failed; code 5" in quiesce, (
        "quiesce.ps1 does not carry the measurement that decided the rag.exe "
        "kill mechanism — the path-scoped PowerShell equivalent that was tried "
        "and failed"
    )


# ==========================================================================
# The 3.5.1 ENGINE defect: the managed engine was never stopped, because the
# enumerator could not see it
# ==========================================================================


def test_the_engines_ownership_does_not_rest_on_one_unreadable_property():
    """`qdrant.exe` earns no image-name evidence BY DESIGN, so for the engine the
    executable PATH is the entire ownership proof — and until 3.5.1 that path had
    exactly one source, `Process.Path`, which shares a primitive with
    `Process.Modules`. Both go through `EnumProcessModules`, so they fail
    TOGETHER. Measured under the 32-bit PowerShell that Inno's 32-bit Setup
    actually launches, against the live product:

        pid 46132 qdrant: Path=<NULL> (no exception) Modules=[]

    `$null` and an empty collection, with nothing raised. So the engine gathered
    no evidence at all, `stop_managed_engine` iterated an empty set,
    `verify_clear` reported "no owned process is running", and the only phase
    that saw the truth was `probe_replaceable` — which can only refuse:
    `files_not_replaceable`, four locked files, ZERO process blockers.
    """
    script = quiesce_code()
    body = re.search(r"function Get-OwnedProcess\b.*?\n\}", script, re.DOTALL)
    assert body, "quiesce.ps1 has no process-ownership enumerator"
    text = body.group(0)

    assert re.search(r"Get-ProcessTable", text), (
        "the enumerator has a single source for the executable path. For "
        "qdrant.exe the path IS the ownership proof, so one unreadable property "
        "makes the managed engine invisible to every phase that stops or proves "
        "anything"
    )

    table = re.search(r"function Get-ProcessTable\b.*?\n\}", script, re.DOTALL)
    assert table, "quiesce.ps1 has no bitness-independent process table"
    assert "Win32_Process" in table.group(0), (
        "the fallback does not go through the WMI service, which is the only "
        "source of an executable path that a 32-bit host can read for a 64-bit "
        "process"
    )
    assert "ExecutablePath" in table.group(0), (
        "the fallback does not read the executable path, so path scoping still "
        "has nothing to scope by"
    )
    assert re.search(r"catch\s*\{", table.group(0)), (
        "a failed process-table query would throw out of a DECISIVE phase and "
        "abort the protocol rather than degrade its evidence"
    )


def test_an_empty_module_list_is_not_counted_as_a_successful_scan():
    """The silent half. A live process has at least its own image mapped, so
    zero modules means the scan did not happen — and reporting it as a clean scan
    is what let the 3.5.1 verdict say "0 process(es) refused a module scan" from
    a host that could read none of them."""
    script = quiesce_code()
    for name in ("Get-OwnedProcess", "Invoke-IdentifyHolders"):
        body = re.search(rf"function {name}\b.*?\n\}}", script, re.DOTALL)
        assert body, f"quiesce.ps1 has no {name}"
        assert "Modules" in body.group(0), f"{name} no longer looks at modules at all"
        assert re.search(r"Count\s*-eq\s*0", body.group(0)), (
            f"{name} treats an empty module list as a scan that succeeded, so a "
            f"host that can read no modules at all reports a clean sweep"
        )


def test_the_engine_stop_awaits_actual_exit_rather_than_a_delivered_signal():
    """`Stop-Process` returns when the request is issued; the kernel releases the
    image's file handle afterwards, and `[InstallDelete]` fails in exactly that
    gap. The engine is the one role with no CLI stop and no `taskkill` behind it,
    so if this phase does not confirm the exit, nothing before the probe does."""
    script = quiesce_code()

    stop = re.search(r"function Invoke-RoleStop\b.*?\n\}", script, re.DOTALL)
    assert stop, "quiesce.ps1 has no role-stop implementation"
    assert "Stop-EngineProcess" in stop.group(0), (
        "the engine is stopped by a bare Stop-Process, which proves only that a "
        "request was issued"
    )

    engine = re.search(r"function Stop-EngineProcess\b.*?\n\}", script, re.DOTALL)
    assert engine, "quiesce.ps1 has no engine stop"
    text = engine.group(0)
    graceful = text.index("CloseMainWindow")
    forceful = text.index("Stop-Process")
    assert graceful < forceful, (
        "the engine is terminated before it is asked, so it never gets the "
        "chance to close its storage cleanly"
    )
    assert text.count("Wait-ProcessGone") >= 2, (
        "the engine stop does not wait after BOTH the graceful request and the "
        "terminate"
    )

    waiter = re.search(r"function Wait-ProcessGone\b.*?\n\}", script, re.DOTALL)
    assert waiter, "quiesce.ps1 has no waiter"
    assert "Get-Process" in waiter.group(0), (
        "the wait believes a return value instead of looking"
    )
    assert "Get-DeadlineExceeded" in waiter.group(0), (
        "the wait is not bounded by the protocol deadline, so one process that "
        "will never die can consume the whole upgrade window"
    )


@pytest.mark.skipif(sys.platform != "win32",
                    reason="Windows PowerShell is the interpreter under test")
def test_the_real_enumerator_finds_an_engine_under_the_installation_and_spares_one_outside(
        tmp_path):
    """The 3.5.1 engine defect, asked of the REAL script, with REAL processes.

    Two 64-bit processes named ``qdrant.exe``: one inside a temporary ``{app}``,
    one outside it. The one inside must be found as ``engine`` with
    ``executable_under_installation``; the one outside must not appear at all,
    because ``storage_backend = "external"`` means a server the user runs and
    stopping it would be data loss in someone else's application.

    Run under EVERY PowerShell host on the machine, and that is the point.
    ``installer.iss`` declares no 64-bit architecture, so Inno's Setup is 32-bit
    and its ``Exec('powershell.exe', ...)`` gets the WOW64-redirected 32-bit
    host — where ``Process.Path`` is ``$null`` and ``Process.Modules`` is empty
    for every 64-bit process, silently. Measured against the pre-fix script:

        PRE-CHANGE, 32-bit: FOUND=0        <- the shipped installer's host
        PRE-CHANGE, 64-bit: FOUND=1

    A test that only ran under the 64-bit host would have been green throughout
    the incident.

    Executed, not grepped: the interpreter's behaviour is the thing that was
    wrong, and no amount of reading the source reveals it.
    """
    hosts = powershell_hosts()
    assert hosts, "no Windows PowerShell was found to run the protocol under"

    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    stand_in = system32 / "ping.exe"          # 64-bit, self-contained, idles on demand
    assert stand_in.is_file(), f"{stand_in} is missing; nothing to stand in for the engine"

    app = tmp_path / "app"
    (app / "bin").mkdir(parents=True)
    outside = tmp_path / "someone-elses-qdrant"
    outside.mkdir()
    ours = app / "bin" / "qdrant.exe"
    theirs = outside / "qdrant.exe"
    shutil.copy(stand_in, ours)
    shutil.copy(stand_in, theirs)

    idle = ["-n", "60", "127.0.0.1"]
    mine = subprocess.Popen([str(ours), *idle],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    yours = subprocess.Popen([str(theirs), *idle],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.5)          # let both processes actually exist
        body = declarations_and_functions(quiesce_source()) + f"""
$AppDir = '{app}'
$DataDir = '{tmp_path / "data"}'
$TimeoutSeconds = 120
foreach ($p in (Get-OwnedProcess)) {{
    Write-Output "OWNED pid=$($p.Pid) role=$($p.Role) evidence=$([string]::Join(',', $p.Evidence))"
}}
Write-Output 'ENUMERATED'
"""
        for label, interpreter in hosts:
            result = run_powershell(body, tmp_path, interpreter=interpreter)
            output = (result.stdout or "") + (result.stderr or "")
            assert "ENUMERATED" in output, (
                f"the enumeration did not finish under the {label} host:\n{output}"
            )

            mine_rows = [line for line in output.splitlines()
                         if line.startswith(f"OWNED pid={mine.pid} ")]
            assert mine_rows, (
                f"under the {label} PowerShell host the managed engine at "
                f"{ours} was NOT found. This is the 3.5.1 defect exactly: the "
                f"engine is invisible to stop_managed_engine, to wait_for_exit "
                f"and to verify_clear, which reports 'no owned process is "
                f"running' while the engine holds the files [InstallDelete] is "
                f"about to remove.\n{output}"
            )
            assert "role=engine" in mine_rows[0], mine_rows[0]
            assert "executable_under_installation" in mine_rows[0], (
                f"the engine was found by something other than its PATH, which "
                f"is the only evidence qdrant.exe is allowed to earn: "
                f"{mine_rows[0]}"
            )
            assert not [line for line in output.splitlines()
                        if line.startswith(f"OWNED pid={yours.pid} ")], (
                f"under the {label} host a Qdrant OUTSIDE this installation "
                f"({theirs}) was claimed as ours. Stopping it would be data loss "
                f"in another application caused by installing this one.\n{output}"
            )
    finally:
        for proc in (mine, yours):
            proc.kill()
            proc.wait(timeout=30)


def test_the_proven_kill_mechanism_was_carried_across_not_reinvented():
    """`taskkill /F /IM ... /T` for the names we own outright.

    v3.0.1 replaced a running `rag.exe` in place on a real machine using exactly
    that call. A path-scoped PowerShell equivalent was tried in its place and
    the upgrade failed with `DeleteFile failed; code 5`. A mechanism that works
    is not replaced by a tidier one that does not.
    """
    script = quiesce_code()
    body = re.search(r"function Invoke-RoleStop\b.*?\n\}", script, re.DOTALL)
    assert body, "quiesce.ps1 has no role-stop implementation"
    text = body.group(0)

    assert "taskkill.exe" in text, (
        "the proven image-name kill was dropped in favour of stopping by PID only"
    )
    assert "'/F', '/IM'" in text.replace('"', "'"), (
        "taskkill is not invoked with the flags that were measured to work"
    )
    assert re.search(r"Test-UnderRoot\s+\$proc\.Executable\s+\$AppDir", text), (
        "the engine is no longer path-scoped, so an external Qdrant the user "
        "runs themselves would be stopped by this installer"
    )


# ==========================================================================
# Post-install: step 16/17
# ==========================================================================


def test_the_installer_restores_the_startup_lifecycle_it_suspended(iss):
    """The tasks were DISABLED, not ended. Leaving them disabled would silently
    switch off a user's autostart — a behaviour change delivered by an
    upgrade."""
    code = code_section(iss)
    verify = pascal_body(code, "VerifyInstallation")
    assert verify, "VerifyInstallation is gone"
    assert "RunQuiescence('Restore')" in verify, (
        "nothing re-enables the scheduled tasks the upgrade disabled"
    )

    script = quiesce_code()
    restore = re.search(r"function Invoke-RestoreTaskState\b.*?\n\}", script, re.DOTALL)
    assert restore, "quiesce.ps1 has no restore mode"
    assert "/enable" in restore.group(0)


def test_a_mixed_installation_is_rolled_back_rather_than_left_behind(iss):
    """`rag.exe` survives a half-completed `[InstallDelete]` while
    `_internal\\python312.dll` does not, and all the user ever sees is
    `[PYI-2072]`. Never leave a mixed PyInstaller directory silently."""
    code = code_section(iss)
    verify = pascal_body(code, "VerifyInstallation")

    assert "InstallationBinariesArePresent()" in verify, (
        "the installer does not check that the payload survived extraction"
    )
    assert "RestoreInstallation()" in verify, (
        "a mixed installation is reported but not repaired"
    )

    consistency = pascal_body(code, "InstallationBinariesArePresent")
    assert "python312.dll" in consistency, (
        "the consistency check does not look for the DLL whose absence produced "
        "[PYI-2072]"
    )

    rollback = pascal_body(code, "RestoreInstallation")
    payload = rollback.find("_internal")
    executables = rollback.find("rag.exe ragw.exe")
    assert payload != -1 and executables != -1
    assert payload < executables, (
        "the executables are restored before their payload; rag.exe without "
        "_internal IS the mixed state being undone"
    )


def test_a_runtime_failure_does_not_trigger_a_rollback(iss):
    """The files are correct. Restoring an older bundle would leave the actual
    cause — a storage outage, a held port, a rebuild that stopped early —
    exactly where it was, on an older version. This installer has already
    learned the general form of that lesson once."""
    code = code_section(iss)
    verify = pascal_body(code, "VerifyInstallation")

    runtime = re.search(r"\n    2:\s*\n\s*begin(.*?)\n\s*end;", verify, re.DOTALL)
    assert runtime, "the runtime-failure branch is gone"
    assert "RestoreInstallation()" not in runtime.group(1), (
        "a runtime failure rolls the installation back, which cannot fix it and "
        "leaves the machine on an older version"
    )
    assert "DiscardRollback()" in runtime.group(1)


def test_the_quiescence_script_is_shipped_and_extracted(iss):
    """`dontcopy` so it never lands in the installation — it is Setup's tool,
    not the product's — and `ExtractTemporaryFile` so it exists before anything
    is installed."""
    files = ini_section(iss, "Files")
    entry = [line for line in files.splitlines() if "quiesce.ps1" in line]
    assert entry, "installer.iss does not ship installer\\quiesce.ps1"
    assert all("dontcopy" in line for line in entry), (
        "quiesce.ps1 is installed into {app}; it is Setup's tool, not part of "
        "the product"
    )

    code = code_section(iss)
    assert "ExtractTemporaryFile('quiesce.ps1')" in code, (
        "the protocol is never unpacked, so PrepareToInstall would run nothing"
    )
