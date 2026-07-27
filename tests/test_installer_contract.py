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
