"""Invariants of `installer.iss` that no Python test would otherwise cover.

The 3.0.0 incident was entirely an installer incident. The suite was green on
three platforms, four artifacts validated, three clean installs verified — and
the product could not survive being installed over its own predecessor, because
the two defects lived in a file the suite did not read:

* the installer never removed the previous `_internal`, so stale package
  manifests accumulated until `importlib.metadata` reported a version the
  bundle had not shipped for years;
* the uninstaller's data wipe took a hand-maintained `config.toml` with the
  rebuildable index, with no backup and nothing in the Recycle Bin.

Neither is reachable from a clean install, which is what every gate tested.
These are properties of the script, asserted structurally, so a well-meaning
tidy-up cannot quietly restore either failure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "installer.iss"


@pytest.fixture(scope="module")
def script() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def section(script: str, name: str) -> str:
    """The body of one `[Section]`, without comment lines.

    Comments are stripped deliberately: every invariant here must hold because
    of what the installer *does*, never because the desired string appears in a
    comment explaining the bug.
    """
    match = re.search(rf"^\[{name}\]$(.*?)(?=^\[|\Z)", script,
                      re.MULTILINE | re.DOTALL)
    if match is None:
        return ""
    return "\n".join(line for line in match.group(1).splitlines()
                     if not line.strip().startswith(";"))


# --- F1: the upgrade must not inherit the previous payload ----------------


def test_the_previous_bundle_payload_is_removed_before_extraction(script):
    """The crash-loop defect, stated as a property of the script.

    Inno processes `[InstallDelete]` before copying `[Files]`, so the presence
    of this entry IS the ordering guarantee — there is no way to express it that
    runs afterwards.
    """
    entries = section(script, "InstallDelete")
    assert entries.strip(), "installer.iss has no [InstallDelete] section"

    internal = [line for line in entries.splitlines()
                if re.search(r"\{app\}\\_internal\b", line)]
    assert internal, "[InstallDelete] no longer removes {app}\\_internal"
    assert all("filesandordirs" in line.lower() for line in internal), (
        "_internal is a directory tree; Type: files would delete nothing"
    )


def test_stale_root_level_manifests_are_swept_too(script):
    """Pre-6.x PyInstaller put the payload at `{app}` rather than in
    `_internal`. An upgrade from one of those layouts is shadowed by exactly the
    same mechanism, one directory up."""
    entries = section(script, "InstallDelete")

    assert re.search(r"\{app\}\\\*\.dist-info", entries), (
        "root-level .dist-info directories are not swept"
    )


def test_the_payload_wipe_does_not_reach_the_whole_install_directory(script):
    """Targeted, not scorched-earth.

    Deleting `{app}` outright would also take anything a user or another tool
    put in the program directory. The defect was stale *payload*, so the fix
    removes payload.
    """
    for line in section(script, "InstallDelete").splitlines():
        name = re.search(r"Name:\s*\"([^\"]+)\"", line)
        if name:
            assert name.group(1).rstrip("\\") != "{app}", (
                "[InstallDelete] removes all of {app}, which is broader than "
                "the defect and takes unrelated files with it"
            )


# --- F2: an uninstall must not destroy what it could not preserve ---------


def uninstall_body(script: str) -> str:
    match = re.search(r"procedure CurUninstallStepChanged.*?\Z", script, re.DOTALL)
    assert match, "CurUninstallStepChanged is gone"
    return match.group(0)


def test_the_configuration_is_copied_out_before_anything_is_deleted(script):
    """`config.toml` is the one file in the data root that cannot be rebuilt.

    The index costs hours; the project list, ignore rules and per-project modes
    cost months and regenerate from nothing. In the incident the wipe took 25
    project definitions and only a manual copy made minutes earlier saved them.
    """
    body = uninstall_body(script)
    backup = body.find("BackupConfig(")
    wipe = body.find("DelTree(")

    assert backup != -1, "the uninstaller no longer backs up the configuration"
    assert wipe != -1, "expected a DelTree on the delete path"
    assert backup < wipe, "the backup must happen BEFORE the deletion"


def test_a_failed_backup_cancels_the_deletion(script):
    """The only unrecoverable outcome is deleting what you failed to copy.

    Keeping the data is always recoverable — the user can delete the folder by
    hand. So a backup failure has to abort the wipe, not proceed past it.
    """
    body = uninstall_body(script)
    guard = re.search(r"if\s+BackupFailed\s+then", body)
    assert guard, "no branch handles a failed backup"
    assert body.find("Exit;", guard.end()) < body.find("DelTree("), (
        "a failed backup does not stop the code reaching DelTree"
    )


def test_the_backup_lands_outside_the_directory_being_deleted(script):
    """A backup inside the data root is deleted by the wipe it exists to
    survive."""
    match = re.search(r"Target\s*:=\s*ExpandConstant\('([^']+)'\)", script)
    assert match, "the backup target is no longer a literal path"
    target = match.group(1)

    assert not target.startswith("{localappdata}\\RAGTools\\"), (
        f"backup target {target} is inside the data root"
    )
    assert "{localappdata}" in target


def test_deleting_user_data_still_defaults_to_no(script):
    """A destructive prompt that defaults to Yes is a trap. Unchanged from
    3.0.0 and asserted so it stays that way."""
    body = uninstall_body(script)

    assert "MB_DEFBUTTON2" in body
    assert "IDYES" in body, "deletion must require an explicit Yes"


# --- the post-install tray launch ----------------------------------------


def test_the_post_install_tray_launch_does_not_depend_on_a_legacy_file(script):
    """v2 wrote `RAGTools-Tray.vbs` into the Startup folder; v3 removes it and
    registers a scheduled task instead. The [Run] step still invoked the VBS, so
    on a fresh v3 install it launched a file that had never existed and the tray
    icon did not appear until the user's next login."""
    run = section(script, "Run")
    tray = [line for line in run.splitlines() if "tray" in line.lower()
            and "install" not in line.lower()]
    assert tray, "nothing launches the tray after installation"

    for line in tray:
        assert ".vbs" not in line.lower(), (
            "the post-install tray launch depends on a legacy Startup-folder "
            "script that v3 deletes"
        )


def test_windows_startup_entry_points_at_the_windowless_executable(script):
    """Anything the installer starts and leaves running should be the GUI
    subsystem binary — see `rag.spec`."""
    run = section(script, "Run")
    tray = [line for line in run.splitlines()
            if re.search(r"Parameters:\s*\"tray\"", line)]
    assert tray, "expected a [Run] entry launching the tray directly"

    for line in tray:
        assert "ragw.exe" in line, (
            "the tray is launched through the console-subsystem binary"
        )


# --- v3.0.2: stopping what actually holds the files ----------------------


def test_every_owned_image_is_killed_not_just_the_original_one(script):
    """The defect v3.0.1 introduced into its own upgrade path.

    v3.0.1 pointed both scheduled tasks at `ragw.exe`, so from that release on
    the service and tray at login run under that image. `ForceKillRagProcesses`
    still killed only `rag.exe` — leaving the very processes an upgrade must
    stop holding their own binaries, which is how a copy silently skips files
    and the installer reports success over a half-replaced tree.
    """
    body = re.search(r"procedure ForceKillRagProcesses.*?^end;", script,
                     re.DOTALL | re.MULTILINE)
    assert body, "ForceKillRagProcesses is gone"
    text = body.group(0)

    for image in ("rag.exe", "ragw.exe", "qdrant.exe"):
        assert image in text, f"{image} is never killed before files are replaced"


def test_scheduled_tasks_are_ended_before_the_processes_are_killed(script):
    """Order matters, and the registration is what makes it matter.

    The task carries RestartOnFailure, and a force-kill is exactly the failure
    it reacts to — so killing first lets the scheduler start a replacement
    between the kill and the copy.
    """
    install = re.search(r"if CurStep = ssInstall then.*?^  end;", script,
                        re.DOTALL | re.MULTILINE)
    assert install, "the pre-install step is gone"
    text = install.group(0)

    stop = text.find("StopOwnedTasks()")
    kill = text.find("ForceKillRagProcesses()")
    assert stop != -1, "scheduled tasks are never stopped before replacement"
    assert kill != -1
    assert stop < kill, "processes are killed before the tasks that restart them"


def test_the_tasks_are_ended_rather_than_deleted(script):
    """`/end`, not `/delete`: [Run] re-registers both tasks from the new
    binaries, and deleting here would strand a user who cancels the wizard
    in between with no autostart at all."""
    body = re.search(r"procedure StopOwnedTasks.*?^end;", script,
                     re.DOTALL | re.MULTILINE)
    assert body, "StopOwnedTasks is gone"
    assert "/end" in body.group(0)
    assert "/delete" not in body.group(0)


# --- v3.0.2: the installer must not claim success it has not verified ----


def test_the_installer_verifies_the_installation_afterwards(script):
    """An installer that copied files has proven it copied files.

    Whether the MACHINE now runs the new version is a different question, and
    every way it can fail — a locked file, a task still naming the old path, a
    surviving process — looks identical from inside the installer: no error.
    """
    assert "VerifyInstallation" in script, "no post-install verification"
    assert re.search(r"selfcheck[^']*--expect-version", script), (
        "verification does not pin the expected version, so it cannot detect "
        "that the machine is still on the old one"
    )


def test_verification_runs_after_the_tasks_are_registered(script):
    """At ssDone, not ssPostInstall.

    [Run] is what re-registers the scheduled tasks from the new binaries, and it
    executes AFTER ssPostInstall. Verifying before that would check targets that
    do not exist yet and pass for the wrong reason.
    """
    assert re.search(r"if CurStep = ssDone then\s*\n\s*VerifyInstallation\(\);", script), (
        "verification is not wired to ssDone"
    )


def test_a_failed_verification_is_reported_as_an_error(script):
    """Silence here is the whole defect class. A mixed installation must say so."""
    # The name appears twice — a `forward;` declaration and the definition. Take
    # the body, not the declaration; matching the first occurrence silently
    # tested CurStepChanged instead, which is its own small lesson about
    # structural assertions.
    bodies = [m.group(0) for m in
              re.finditer(r"procedure VerifyInstallation\(\);.*?^end;", script,
                          re.DOTALL | re.MULTILINE)
              if "forward;" not in m.group(0).splitlines()[0]]
    assert bodies, "VerifyInstallation has no definition"
    text = bodies[0]
    assert "mbCriticalError" in text, "a failed verification does not surface as an error"
    assert re.search(r"ResultCode\s*<>\s*0", text), (
        "the verifier's exit code is never checked"
    )


# --- F4: nothing in the upgrade path may wait forever ---------------------


def _code_section(script: str) -> str:
    """The `[Code]` body with Pascal comments stripped.

    `section()` strips `;` comments, which is the comment syntax of the *ini*
    sections. `[Code]` is Pascal and uses `//`, so nothing was ever stripped
    there — and every structural assertion over this section was really being
    made against the prose explaining the defect as much as the code fixing it.
    Two tests here duly passed on a comment that quoted the very line they were
    written to forbid.

    Only whole-line comments are removed. `//` also appears inside string
    literals (`http://...`), and cutting at the first occurrence anywhere would
    silently truncate real code.
    """
    body = section(script, "Code")
    return "\n".join(line for line in body.splitlines()
                     if not line.strip().startswith("//"))


def test_the_previous_releases_binary_is_never_run_unbounded(script):
    """The upgrade path must not wait forever on a binary it did not ship.

    `Exec(..., ewWaitUntilTerminated, ...)` has no timeout. That is acceptable
    for this release's own executables and unacceptable for `{app}\rag.exe`
    during ssInstall, because at that moment `{app}` still holds the PREVIOUS
    release — any version ever published — and how it behaves when its own
    service is wedged is not knowable from here.

    Observed: a silent upgrade over a running v2.7.0 produced no output and did
    not finish in forty minutes, while the same installer completed a clean
    install in ninety seconds.
    """
    code = _code_section(script)
    # Built with re.escape rather than hand-escaped. Written by hand, the
    # literal `{app}\rag.exe` needs FOUR backslashes in a raw string: `\\rag`
    # reaches the regex engine as `\r` — a carriage return — so the pattern
    # matched nothing and the test passed against the very file it was written
    # to reject. A negative control caught it; re.escape removes the trap.
    target = re.escape(r"ExpandConstant('{app}\rag.exe')")
    unbounded = re.findall(rf"Exec\(\s*{target}.*?ewWaitUntilTerminated",
                           code, re.DOTALL)
    assert not unbounded, (
        "the previous release's rag.exe is still run with an unbounded Exec: "
        f"{unbounded}"
    )


def test_a_bounded_exec_helper_exists_and_enforces_a_timeout(script):
    code = _code_section(script)
    assert "function ExecBounded(" in code, "no bounded-exec helper"
    body = re.search(r"function ExecBounded\(.*?^end;", code,
                     re.DOTALL | re.MULTILINE)
    assert body, "ExecBounded has no body"
    text = body.group(0)
    assert "WaitForExit(" in text, "ExecBounded does not actually wait with a timeout"
    assert ".Kill()" in text, "ExecBounded never kills a process that overruns"


def test_the_graceful_stop_is_bounded(script):
    """Phase 1 of ssInstall is a courtesy, not a mechanism — it must not block."""
    code = _code_section(script)
    step = re.search(r"if CurStep = ssInstall then.*?^  end;", code,
                     re.DOTALL | re.MULTILINE)
    assert step, "ssInstall handling not found"
    text = step.group(0)
    assert "ExecBounded" in text, "the pre-install stop sequence is not bounded"


def test_verification_cannot_hang_a_completed_installation(script):
    """ssDone runs after every file is in place; a stalled verifier there would
    hang an installation that had already succeeded."""
    bodies = [m.group(0) for m in
              re.finditer(r"procedure VerifyInstallation\(\);.*?^end;", script,
                          re.DOTALL | re.MULTILINE)
              if "forward;" not in m.group(0).splitlines()[0]]
    assert bodies, "VerifyInstallation has no definition"
    assert "ExecBounded" in bodies[0], "the verifier is invoked unbounded"


# --- F5: the kill must not reach processes this installation does not own ---


def test_only_our_own_image_names_are_killed_by_name(script):
    """`rag.exe` and `ragw.exe` are names nothing else ships. `qdrant.exe` is not.

    Matching `qdrant.exe` by image name reaches every Qdrant on the machine —
    including one a user runs themselves under `storage_backend = "external"`,
    which the architecture explicitly says this product must not own. Killing it
    is data loss in someone else's application caused by installing this one.

    The two halves are deliberately asymmetric: image-name matching for what we
    own outright (and what is proven to work — v3.0.1 replaced a running
    `rag.exe` in place with exactly that call), path scoping for the name we
    share.
    """
    code = _code_section(script)
    body = re.search(r"procedure ForceKillRagProcesses\(\);.*?^end;", code,
                     re.DOTALL | re.MULTILINE)
    assert body, "ForceKillRagProcesses has no definition"
    text = body.group(0)

    by_name = re.findall(r"/IM\s+(\S+)", text)
    assert by_name, "nothing is stopped at all"
    assert "qdrant.exe" not in by_name, (
        "qdrant.exe is killed by image name, which reaches every Qdrant on the "
        "machine including an external one the user runs themselves"
    )
    assert set(by_name) == {"rag.exe", "ragw.exe"}, (
        f"unexpected image-name kills: {by_name}"
    )
    assert re.search(r"\$_\.Path", text), "qdrant is not scoped by executable path"
    assert "StartsWith" in text, "paths are not compared against a root"


def test_the_kill_reports_what_it_did(script):
    """Inno records nothing about Exec, so a kill that silently does nothing is
    indistinguishable in the log from one that worked. Two CI cycles went into
    fixing the wrong layer for exactly that reason."""
    code = _code_section(script)
    body = re.search(r"procedure ForceKillRagProcesses\(\);.*?^end;", code,
                     re.DOTALL | re.MULTILINE)
    assert body
    text = body.group(0)
    assert text.count("Log(") >= 3, "the kill does not log its results"
    assert "still running afterwards" in text, (
        "nothing checks — or reports — whether any owned process survived"
    )


def test_the_kill_does_not_depend_on_wmic(script):
    """`wmic` is deprecated and absent from recent Windows builds, where a
    kill built on it silently matches nothing."""
    code = _code_section(script)
    assert "wmic" not in code.lower(), "the installer depends on deprecated wmic"


def test_every_owned_image_is_still_covered(script):
    """Scoping must not quietly narrow WHICH images are stopped — that was the
    3.0.1 defect, and re-introducing it would restore the locked-file failure.
    """
    code = _code_section(script)
    body = re.search(r"procedure ForceKillRagProcesses\(\);.*?^end;", code,
                     re.DOTALL | re.MULTILINE)
    assert body
    text = body.group(0)
    # rag/ragw by image name (taskkill), qdrant by path-scoped Get-Process.
    # Both halves must be present; dropping either restores a real defect.
    assert "/IM rag.exe" in text, "rag.exe is no longer stopped"
    assert "/IM ragw.exe" in text, "ragw.exe is no longer stopped — the 3.0.1 defect"
    # Plain substring, not a regex. A hand-written word-boundary escape here
    # turned into a literal backspace byte, and the pattern silently stopped
    # matching the file it was written to check. Where a substring will do,
    # a regex is just more surface for that to happen on.
    assert "Get-Process -Name qdrant " in text, (
        "qdrant.exe is no longer stopped"
    )


def test_get_process_is_given_base_names_not_filenames(script):
    """`Get-Process -Name` matches ProcessName, which carries no extension.

    `-Name qdrant.exe` matches nothing AND reports no error, so the kill would
    silently spare the process it was aiming at. Measured on a live machine:
    `-Name rag.exe,ragw.exe,qdrant.exe` returned 0 processes; `-Name
    rag,ragw,qdrant` returned 7. (`taskkill /IM` is the opposite — it wants the
    filename — which is why the two halves look different.)
    """
    code = _code_section(script)
    body = re.search(r"procedure ForceKillRagProcesses\(\);.*?^end;", code,
                     re.DOTALL | re.MULTILINE)
    assert body, "ForceKillRagProcesses has no definition"

    for names in re.findall(r"Get-Process -Name ([A-Za-z0-9_,.]+)", body.group(0)):
        assert ".exe" not in names, (
            f"Get-Process was given filenames, which match nothing: {names}"
        )


def test_the_kill_needs_no_nested_double_quotes(script):
    """A quoted WQL filter has to survive Inno's Exec, CommandLineToArgvW and
    PowerShell's own parsing. When it does not, the query errors, the error is
    swallowed, and nothing is killed."""
    code = _code_section(script)
    body = re.search(r"procedure ForceKillRagProcesses\(\);.*?^end;", code,
                     re.DOTALL | re.MULTILINE)
    assert body
    assert '""' not in body.group(0), (
        "the kill embeds escaped double quotes, whose survival depends on every "
        "layer agreeing about escaping"
    )


# --- F6: the [Code] section must only call functions that exist -----------


#: Inno Setup Pascal Script support functions this installer relies on.
#:
#: Curated deliberately rather than generated: the point is that adding a NEW
#: name is a decision someone has to make consciously, having checked it against
#: Inno's "Support Functions Reference". Every entry here has compiled.
INNO_BUILTINS = {
    "AddBackslash", "ChangeFileExt", "DelTree", "DirExists", "Exec",
    "ExecAsOriginalUser", "ExpandConstant", "ExtractFileDir", "ExtractFileExt",
    "ExtractFileName", "ExtractFilePath", "FileCopy", "FileExists",
    "ForceDirectories", "GetDateTimeString", "IntToStr", "Length", "Log",
    "LowerCase",
    "MsgBox", "Pos", "RegQueryStringValue", "RegWriteStringValue",
    "RemoveBackslash", "SetLength", "Sleep", "StringChangeEx", "Trim",
    "UpperCase", "Copy", "IsAdminLoggedOn", "GetEnv", "SuppressibleMsgBox",
}

#: Pascal keywords that can be followed by a parenthesis.
_PASCAL_KEYWORDS = {
    "if", "then", "else", "begin", "end", "for", "while", "do", "and", "or",
    "not", "var", "to", "case", "of", "array", "function", "procedure", "div",
    "mod", "in", "repeat", "until", "with", "const", "type", "record",
}


def _pascal_code_only(script: str) -> str:
    """`[Code]`, with comments AND string literals removed.

    String literals matter: this installer builds PowerShell commands as Pascal
    strings, so `$p.Kill()` and `.StartsWith(...)` appear as text. Extracting
    identifiers without stripping them reports PowerShell methods as missing
    Pascal functions.
    """
    body = _code_section(script)
    return re.sub(r"'(?:[^']|'')*'", "''", body)


def test_the_code_section_calls_only_functions_that_exist(script):
    """Catch an unknown identifier here, not twenty-five minutes into CI.

    `RemoveFileExt` looked like the obvious way to turn `rag.exe` into `rag`.
    It does not exist in Pascal Script, and nothing said so until ISCC failed
    on a hosted runner after a full PyInstaller build — one wasted cycle for a
    fault that is visible in the source.
    """
    code = _pascal_code_only(script)
    defined = set(re.findall(r"(?:function|procedure)\s+(\w+)", code))
    called = {name for name in re.findall(r"\b([A-Za-z_]\w*)\s*\(", code)
              if name.lower() not in _PASCAL_KEYWORDS}

    unknown = called - defined - INNO_BUILTINS
    assert not unknown, (
        f"[Code] calls {sorted(unknown)}, which is neither defined in this "
        "script nor a known Inno Setup support function. If it IS one, add it "
        "to INNO_BUILTINS after checking Inno's Support Functions Reference."
    )


def test_restart_manager_is_disabled(script):
    """Two mechanisms closing the same processes, and the loser aborts the install.

    Inno's RestartManager enumerates BEFORE `CurStepChanged(ssInstall)` and
    shuts down AFTER it, so it acts on a list that is as stale as our pre-install
    phase is long — 86 seconds, measured. Anything it then fails to close raises
    an Abort/Retry/Ignore box, and `/SUPPRESSMSGBOXES` answers with the DEFAULT,
    which is Abort. Observed exactly that: "Some applications could not be shut
    down" -> "User canceled the installation process" -> exit 5, on an upgrade
    where nothing was actually wrong.

    This installer closes its own processes, scoped by path, and verifies the
    result. RM adds a second opinion and a way to lose.
    """
    setup = section(script, "Setup")
    match = re.search(r"^CloseApplications=(\w+)", setup, re.MULTILINE)
    assert match, "CloseApplications is not stated explicitly"
    assert match.group(1).lower() == "no", (
        "RestartManager is enabled; its suppressed Abort/Retry/Ignore defaults "
        "to Abort and cancels silent upgrades"
    )


def test_this_suite_contains_no_stray_control_characters():
    """Escapes that silently become control bytes are this suite's recurring bug.

    A hand-written `\b` inside an r-string reaches the regex engine as a word
    boundary; written one backslash short it becomes byte 0x08, the pattern
    stops matching, and the test passes against the file it was written to
    reject. That has now happened four times in this file — twice as a
    carriage-return from `\r`, once as a backspace — and each time the symptom
    was a green test rather than a red one.
    """
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        data = path.read_bytes()
        strays = {b for b in data if b < 9 or b in (11, 12) or 13 < b < 32}
        assert not strays, (
            f"{path.name} contains control bytes {sorted(strays)} — almost "
            "certainly a backslash escape that lost a backslash"
        )
