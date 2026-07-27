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
