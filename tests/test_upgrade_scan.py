"""Upgrade detection and repair, modelled on the machine this was written on.

The fixtures here are not invented. The development machine carries, right now:

* the install directory repeated **sixteen times** on ``PATH``,
* two spellings of that directory (``Programs\\RAGTools`` and
  ``Programs\\ragtools``) that resolve to one folder and decide which ``rag``
  runs,
* a ``RAGTools Watchdog`` scheduled task,
* ``RAGTools.vbs`` and ``RAGTools-Tray.vbs`` in the Startup folder,
* stale ``service.pid`` / ``supervisor.pid`` / ``tray.pid`` files and a
  ``RAGTools-Watchdog.vbs`` inside the data directory,
* and an isolated development environment that an installer must never touch.

Every test below is one of those, turned into something the code can see.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from ragtools.platform import KIND_SERVICE, KIND_TRAY, Registration
from ragtools.upgrade.migrate import CONFIG_VERSION, migrate_config, repair_path
from ragtools.upgrade.scan import (
    L_DATA,
    L_INSTALL_USER,
    L_LEGACY_ARTIFACT,
    is_development_path,
    scan,
    summarize,
)

SEP = os.pathsep

#: PATH fixtures have to be shaped like the HOST's PATH, not like Windows'.
#: These tests joined `C:\...` entries with `os.pathsep` — which is `:` on POSIX,
#: so every drive letter became its own entry and `C:\Windows` arrived as
#: ['C', '\Windows']. They passed on Windows and failed on Linux and macOS, and
#: nobody saw it because the suite only ever ran on Windows.
WINDOWS_PATHS = os.name == "nt"
SYSTEM_DIR = r"C:\Windows\system32" if WINDOWS_PATHS else "/usr/bin"
OTHER_DIR = r"C:\Git\cmd" if WINDOWS_PATHS else "/usr/local/bin"
PLAIN_DIR = r"C:\Windows" if WINDOWS_PATHS else "/usr/bin"
INSTALL_DIR = (r"C:\Users\testuser\AppData\Local\Programs\RAGTools"
               if WINDOWS_PATHS else "/home/testuser/.local/opt/RAGTools")

def _detect_case_insensitive_fs() -> bool:
    """Ask this filesystem whether it folds case. Do not infer it.

    `os.name == "nt" or sys.platform == "darwin"` was the first version, and it
    is the same mistake the product had: case-sensitivity belongs to the
    FILESYSTEM, not the OS. A case-sensitive APFS volume — supported, and what
    some developers deliberately choose — would have run the case-insensitive
    assertions and failed them, on a Mac, for being right.
    """
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "CaseProbe").mkdir()
        return (Path(td) / "caseprobe").exists()


#: Two spellings differing only in case name ONE directory where the filesystem
#: folds and TWO where it does not, so collapsing them is correct on the first
#: and destructive on the second. `directory_identity()` asks the filesystem;
#: so does this.
CASE_INSENSITIVE_FS = _detect_case_insensitive_fs()


class FakeAdapter:
    def __init__(self, app_dir: Path, registrations=()):
        self._app = app_dir
        self._regs = list(registrations)

    name = "fake"

    def app_dir(self):
        return self._app

    def dev_dir(self):
        return self._app.parent / "RAGTools-dev"

    def find_autostart(self, kind=KIND_SERVICE):
        return [r for r in self._regs if r.kind == kind]


# --- PATH repair ----------------------------------------------------------


def test_sixteen_duplicate_entries_collapse_to_one():
    """The measured state. `NeedsAddPath` returned true on every upgrade and
    appended again, sixteen times, for one directory."""
    value = SEP.join([SYSTEM_DIR] + [INSTALL_DIR] * 16 + [OTHER_DIR])

    repair = repair_path(value)

    assert repair.changed
    assert len(repair.removed) == 15
    assert repair.entries.count(INSTALL_DIR) == 1
    # Everything else keeps its position — a PATH cleanup that reorders
    # unrelated entries is a far worse bug than the one being fixed.
    assert repair.entries[0] == SYSTEM_DIR
    assert repair.entries[-1] == OTHER_DIR


def test_product_entries_are_recognised_whatever_their_casing():
    """The bug CI caught on every POSIX runner.

    `is_product` used `os.path.normcase`, which lowercases on Windows and is the
    IDENTITY on POSIX — so on Linux and macOS the check `"ragtools" in ...`
    never matched the product's own `RAGTools` directory, and PATH cleanup
    silently found nothing to clean on two of three platforms.
    """
    upper = f"{SYSTEM_DIR}{SEP}{INSTALL_DIR}{SEP}{INSTALL_DIR}"
    repair = repair_path(upper)

    assert repair.changed, "a capitalised RAGTools entry must still be ours"
    assert len(repair.removed) == 1


@pytest.mark.skipif(not CASE_INSENSITIVE_FS,
                    reason="two casings are two directories on a case-sensitive "
                           "filesystem; collapsing them there would drop a real entry")
def test_two_casings_of_one_directory_are_one_entry(tmp_path):
    """`where rag` resolved a DIFFERENT casing than the other fifteen entries,
    so the two spellings decided which executable ran."""
    lower = tmp_path / "programs" / "ragtools"
    lower.mkdir(parents=True)
    value = SEP.join([str(lower), str(lower).upper()])

    repair = repair_path(value)

    assert len(repair.entries) == 1


@pytest.mark.skipif(not CASE_INSENSITIVE_FS,
                    reason="the two spellings are distinct directories here")
def test_a_preferred_spelling_wins_so_the_installer_and_path_agree(tmp_path):
    canonical = tmp_path / "Programs" / "RAGTools"
    canonical.mkdir(parents=True)
    value = SEP.join([str(canonical).lower(), str(canonical)])

    repair = repair_path(value, keep=str(canonical))

    assert repair.entries == [str(canonical)]


def test_a_case_sensitive_filesystem_keeps_both_spellings(tmp_path):
    """The other half of the same rule, asserted where it actually applies.

    On Linux `/opt/RAGTools` and `/opt/ragtools` are two directories. Folding
    them into one would silently delete a working PATH entry — which is why
    `_key()` follows the filesystem while name-matching does not.
    """
    if CASE_INSENSITIVE_FS:
        pytest.skip("this filesystem treats the two spellings as one directory")
    lower = tmp_path / "programs" / "ragtools"
    upper = tmp_path / "programs" / "RAGTools"
    lower.mkdir(parents=True)
    upper.mkdir(parents=True)

    repair = repair_path(SEP.join([str(lower), str(upper)]))

    assert len(repair.entries) == 2, "two real directories must both survive"
    assert not repair.removed


def test_non_product_entries_are_never_touched():
    other = OTHER_DIR if OTHER_DIR != PLAIN_DIR else SYSTEM_DIR
    value = SEP.join([PLAIN_DIR, other, PLAIN_DIR])
    repair = repair_path(value)
    assert not repair.changed
    assert repair.entries == value.split(SEP)


def test_empty_segments_survive():
    """A trailing separator is normal on Windows; removing it is a visible,
    pointless diff in the user's environment."""
    value = SEP.join([PLAIN_DIR, ""])
    assert repair_path(value).entries == [PLAIN_DIR, ""]


def test_a_development_path_is_not_deduplicated_as_a_product_entry(tmp_path):
    dev = tmp_path / "rag-v3-dev" / "Scripts"
    dev.mkdir(parents=True)
    repair = repair_path(SEP.join([str(dev), str(dev)]))
    # It is a duplicate, but of a dev path — the installer has no business
    # rewriting a developer's environment either way.
    assert repair.entries.count(str(dev)) >= 1


# --- development-path protection -----------------------------------------


@pytest.mark.parametrize("path", [
    r"C:\Users\testuser\AppData\Local\RAGTools-dev",
    r"C:\Users\testuser\AppData\Local\rag-v3-dev\src",
    r"C:\Users\testuser\AppData\Local\rag-v3-e2e\gui",
    # The POSIX rows are the regression. DEV_MARKERS are lowercase and the match
    # went through os.path.normcase, which is the IDENTITY on POSIX — so these
    # returned False on Linux and macOS, exactly where the platform adapters
    # create `RAGTools-dev`. The guard against deleting a developer's isolated
    # store was inert on two of three platforms.
    "/home/testuser/.local/share/RAGTools-dev",
    "/Users/testuser/Library/Application Support/RAGTools-dev",
])
def test_development_environments_are_recognised(path):
    assert is_development_path(path) is True


@pytest.mark.parametrize("path", [
    r"C:\Users\testuser\AppData\Local\RAGTools",
    r"C:\Users\testuser\AppData\Local\Programs\RAGTools",
    "/home/testuser/.local/share/RAGTools",
    "/Users/testuser/Library/Application Support/RAGTools",
])
def test_installed_locations_are_not_mistaken_for_development(path):
    assert is_development_path(path) is False


def test_a_development_data_directory_is_found_but_protected(tmp_path):
    """Deleting a developer's isolated environment because the name looked
    close enough is worse than leaving a stale file behind."""
    dev = tmp_path / "RAGTools-dev"
    (dev / "data").mkdir(parents=True)

    result = scan(adapter=FakeAdapter(dev), path_value="")

    findings = result.by_layout(L_DATA)
    assert findings and findings[0].protected
    assert result.removable == []


# --- installation discovery ----------------------------------------------


def test_the_data_directory_is_kept_not_removed(tmp_path):
    """It is RENAMED by apply, never deleted — that is what keeps rollback real
    past the migration boundary."""
    app = tmp_path / "RAGTools"
    (app / "data").mkdir(parents=True)

    result = scan(adapter=FakeAdapter(app), path_value="")

    data = result.by_layout(L_DATA)[0]
    assert data.removable is False


def test_program_files_are_marked_for_removal(tmp_path):
    app = tmp_path / "RAGTools"
    app.mkdir()
    programs = tmp_path / "Programs" / "RAGTools"
    programs.mkdir(parents=True)

    result = scan(adapter=FakeAdapter(app), path_value="")

    install = result.by_layout(L_INSTALL_USER)
    assert install and install[0].removable is True


def test_stale_runtime_artifacts_are_found(tmp_path):
    """PID files and the watchdog's VBS outlive the processes that wrote them,
    and a stale `service.pid` makes the next start think it is already running."""
    app = tmp_path / "RAGTools"
    data = app / "data"
    data.mkdir(parents=True)
    for name in ("service.pid", "supervisor.pid", "tray.pid", "RAGTools-Watchdog.vbs"):
        (data / name).write_text("x", encoding="utf-8")

    result = scan(adapter=FakeAdapter(app), path_value="")

    stale = {f.path.name for f in result.by_layout(L_LEGACY_ARTIFACT)}
    assert stale == {"service.pid", "supervisor.pid", "tray.pid", "RAGTools-Watchdog.vbs"}
    assert all(f.removable for f in result.by_layout(L_LEGACY_ARTIFACT))


def test_superseded_registrations_are_collected_for_both_concerns(tmp_path):
    app = tmp_path / "RAGTools"
    app.mkdir()
    regs = [
        Registration(r"\RAGTools\Service", KIND_SERVICE, "task-scheduler", "rag.exe"),
        Registration("RAGTools Watchdog", KIND_SERVICE, "task-scheduler", "x", legacy=True),
        Registration("RAGTools-Tray.vbs", KIND_TRAY, "startup-folder", "y", legacy=True),
    ]

    result = scan(adapter=FakeAdapter(app, regs), path_value="")

    legacy = {r.name for r in result.registrations if r.legacy}
    assert legacy == {"RAGTools Watchdog", "RAGTools-Tray.vbs"}


def test_the_summary_states_the_plan_before_anything_stops(tmp_path):
    """A scan produces something a human reads first. An upgrade that stops
    services before showing its plan cannot be declined."""
    app = tmp_path / "RAGTools"
    (app / "data").mkdir(parents=True)
    install = tmp_path / "Programs" / "RAGTools"
    install.mkdir(parents=True)

    text = summarize(scan(
        adapter=FakeAdapter(app, [
            Registration("RAGTools Watchdog", KIND_SERVICE, "task-scheduler", "x", legacy=True)]),
        path_value=SEP.join([str(install)] * 3),
    ))

    assert "remove" in text and "keep" in text
    assert "duplicate" in text
    assert "RAGTools Watchdog" in text


def test_scanning_changes_nothing(tmp_path):
    app = tmp_path / "RAGTools"
    (app / "data").mkdir(parents=True)
    (app / "data" / "service.pid").write_text("123", encoding="utf-8")
    before = sorted(p.name for p in (app / "data").iterdir())

    scan(adapter=FakeAdapter(app), path_value="")

    assert sorted(p.name for p in (app / "data").iterdir()) == before


# --- config migration -----------------------------------------------------


def _v2_document(tmp_path) -> dict:
    """The shape actually installed on this machine: fifteen projects, each
    with a mode and a dependency_paths list, and none of the v3 keys."""
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return {
        "version": 2,
        "projects": [
            {"id": f"p{i}", "name": f"P{i}", "path": str(proj),
             "enabled": True, "ignore_patterns": [], "dependency_paths": [],
             "mode": "docs"}
            for i in range(15)
        ],
    }


def test_v2_gains_the_v3_keys_it_lacks(tmp_path):
    result = migrate_config(_v2_document(tmp_path))

    assert result.changed
    assert result.from_version == 2
    assert result.document["version"] == CONFIG_VERSION
    assert set(result.added_keys) == {"storage_backend", "collection_strategy"}


def test_the_default_storage_backend_is_the_one_that_needs_nothing(tmp_path):
    """Choosing `managed` for the user would download a binary they did not ask
    for during an upgrade. Embedded works everywhere and says so when the index
    outgrows it."""
    result = migrate_config(_v2_document(tmp_path))
    assert result.document["storage_backend"] == "embedded"


def test_migration_preserves_every_project(tmp_path):
    result = migrate_config(_v2_document(tmp_path))
    assert len(result.document["projects"]) == 15
    assert result.project_count == 15


def test_legacy_dependency_paths_become_catalog_entries(tmp_path):
    dep = tmp_path / "shared" / "odoo"
    dep.mkdir(parents=True)
    doc = _v2_document(tmp_path)
    doc["projects"][0]["dependency_paths"] = [str(dep)]
    doc["projects"][1]["dependency_paths"] = [str(dep)]     # same folder, two projects

    result = migrate_config(doc)

    assert len(result.document["dependencies"]) == 1, "one folder became two entries"
    assert result.adopted_dependencies == ["odoo"]
    assert result.document["projects"][0]["dependencies"] == ["odoo"]
    assert result.document["projects"][1]["dependencies"] == ["odoo"]


def test_migration_is_idempotent(tmp_path):
    """An interrupted upgrade re-runs the whole step."""
    once = migrate_config(_v2_document(tmp_path))
    twice = migrate_config(once.document)

    assert twice.changed is False
    assert twice.document == once.document


def test_migration_never_writes(tmp_path):
    """It returns the document it WOULD write, so a dry run and a real run
    cannot disagree about what happens."""
    config = tmp_path / "config.toml"
    config.write_text("version = 2\n", encoding="utf-8")

    migrate_config(_v2_document(tmp_path))

    assert config.read_text(encoding="utf-8") == "version = 2\n"


def test_directory_identity_asks_the_filesystem_not_the_platform(tmp_path):
    """Why `os.path.normcase` cannot answer "are these one directory?".

    normcase lowercases on Windows and is the IDENTITY on both Linux AND macOS.
    That reads as "POSIX is case-sensitive", which is false: the default APFS is
    case-insensitive, so on macOS two casings of one directory each kept their
    own PATH entry — the duplicate-entry bug repair_path() exists to fix, going
    unfixed on the platform whose CI reported it.

    `(st_dev, st_ino)` is the filesystem's own answer and needs no platform
    test. Asserted by mechanism here so the result holds on filesystems this
    suite never runs on — including a case-sensitive APFS volume, where a
    sys.platform check would be wrong in the other direction.
    """
    from ragtools.upgrade.migrate import directory_identity

    real = tmp_path / "Programs" / "RAGTools"
    real.mkdir(parents=True)

    assert directory_identity(str(real)).startswith("inode:"), (
        "an existing directory must be identified by inode, not by its spelling")

    # Whatever this filesystem says about case, the key must agree with it.
    upper = str(real).upper()
    folds = Path(upper).exists()
    assert (directory_identity(upper) == directory_identity(str(real))) is folds

    # A path that does not exist has no inode and cannot be proven identical to
    # anything — textual fallback, never a false match.
    absent = directory_identity(str(real / "nope"))
    assert not absent.startswith("inode:")
    assert absent != directory_identity(str(real))
