"""The installer told a user with a healthy install to reboot and reinstall.

`VerifyInstallation` reduced eleven checks to one bit and printed a fixed
sentence for every non-zero exit: *"a process from the previous version was
still running... some files were skipped... restart Windows, then run this
installer again."*

Five of those eleven checks fail for RUNTIME reasons on a machine whose files
are byte-perfect — an unreachable storage engine, a config still at the previous
schema, a service that has not come up, a rebuild that stopped. For all of them
the advice was wrong, and the remedy — reinstall over a machine mid-migration —
actively harmful.

The CLI already knew the difference (`cli.py` branches the remedy by check
name). The installer could not see it. So the category is now the exit code, and
the installer only chooses words.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragtools.selfcheck import (
    CATEGORY_INTEGRITY,
    CATEGORY_MIGRATING,
    CATEGORY_RUNTIME,
    CATEGORY_WARNING,
    Check,
    EXIT_CLEAN,
    EXIT_INTEGRITY,
    EXIT_MIGRATING,
    EXIT_RUNTIME,
    as_dict,
    classify,
    exit_code,
)


def failing(name, category):
    return Check(name, ok=False, detail="x", category=category)


def passing(name):
    return Check(name, ok=True, detail="fine")


# --- classification ---------------------------------------------------------


def test_a_clean_run_is_clean():
    assert classify([passing("a"), passing("b")]) == ""
    assert exit_code([passing("a")]) == EXIT_CLEAN


def test_a_skipped_check_is_not_a_failure():
    skipped = Check("x", ok=True, detail="", skipped=True)

    assert classify([skipped]) == ""


def test_integrity_outranks_everything():
    """Ordered by what the operator must DO. A broken install is the only case
    where re-running the installer is the right move, so it must not be masked
    by a rebuild that happens to be in flight."""
    checks = [failing("reindex state", CATEGORY_MIGRATING),
              failing("storage contract", CATEGORY_RUNTIME),
              failing("installed version", CATEGORY_INTEGRITY)]

    assert classify(checks) == CATEGORY_INTEGRITY
    assert exit_code(checks) == EXIT_INTEGRITY


def test_runtime_outranks_migrating():
    checks = [failing("reindex state", CATEGORY_MIGRATING),
              failing("storage contract", CATEGORY_RUNTIME)]

    assert exit_code(checks) == EXIT_RUNTIME


def test_a_rebuild_in_flight_has_its_own_code():
    assert exit_code([failing("reindex state", CATEGORY_MIGRATING)]) == EXIT_MIGRATING


def test_every_category_maps_to_a_distinct_code():
    codes = {exit_code([failing("x", c)]) for c in
             (CATEGORY_INTEGRITY, CATEGORY_RUNTIME, CATEGORY_MIGRATING,
              CATEGORY_WARNING)}

    assert len(codes) == 4, "two causes share an exit code; the installer cannot tell them apart"
    assert EXIT_CLEAN not in codes, "a failure exits 0"


# --- which check belongs to which category ----------------------------------


def test_installation_checks_default_to_integrity():
    """A check that says nothing about its category must not be mistaken for a
    runtime condition — the default has to be the conservative one."""
    assert Check("installed version", ok=False).category == CATEGORY_INTEGRITY


def _check_categories() -> dict:
    """Every `Check(...)` construction in selfcheck.py, by name -> categories.

    Parsed, not grepped: a textual scan matches the module docstring that
    explains this very fix, and the obvious repair (skip comments) still matches
    a docstring. The syntax tree only sees code.
    """
    import ast

    source = (Path(__file__).resolve().parents[1] / "src" / "ragtools" /
              "selfcheck.py").read_text(encoding="utf-8")
    found: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Check" and node.args):
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        category = "CATEGORY_INTEGRITY"      # the dataclass default
        for kw in node.keywords:
            if kw.arg == "category" and isinstance(kw.value, ast.Name):
                category = kw.value.id
        found.setdefault(first.value, set()).add(category)
    return found


@pytest.mark.parametrize("name", [
    "config schema", "migration state", "storage contract",
    "service health version", "reindex state",
])
def test_runtime_conditions_are_declared_as_runtime(name):
    """A new check must not quietly inherit the integrity default and start
    telling people to reboot for a storage outage."""
    categories = _check_categories()

    assert name in categories, f"{name!r} is no longer constructed in selfcheck.py"
    assert categories[name] - {"CATEGORY_INTEGRITY"}, (
        f"every branch of {name!r} declares the integrity default, so the "
        f"installer will blame file replacement for it")


def test_installation_checks_stay_on_the_integrity_default():
    """The converse. These genuinely ARE file-replacement failures."""
    categories = _check_categories()

    for name in ("installed version", "windowed executable",
                 "recorded install version", "running processes",
                 "autostart targets"):
        assert categories.get(name) == {"CATEGORY_INTEGRITY"}, (
            f"{name!r} is no longer classified as an installation-integrity "
            f"failure, but re-running the installer is still its remedy")


# --- the machine-readable form ---------------------------------------------


def test_json_output_names_the_category_and_the_code():
    body = as_dict([failing("storage contract", CATEGORY_RUNTIME)],
                   expected="3.2.0")

    assert body["category"] == CATEGORY_RUNTIME
    assert body["exit_code"] == EXIT_RUNTIME
    assert body["ok"] is False
    assert body["expected_version"] == "3.2.0"
    assert body["checks"][0]["name"] == "storage contract"


def test_json_output_of_a_clean_machine_says_clean():
    body = as_dict([passing("a")], expected="3.2.0")

    assert body["category"] == "clean" and body["ok"] is True


# --- the installer's side of the contract -----------------------------------


@pytest.fixture(scope="module")
def installer() -> str:
    return (Path(__file__).resolve().parents[1] / "installer.iss").read_text(
        encoding="utf-8")


def test_the_installer_branches_on_the_exit_code(installer):
    verify = installer.split("procedure VerifyInstallation()")[-1]

    assert "case ResultCode of" in verify, (
        "the installer still collapses every failure into one message")


@pytest.mark.parametrize("code", ["2:", "3:", "4:"])
def test_the_installer_handles_every_category(code, installer):
    verify = installer.split("procedure VerifyInstallation()")[-1]

    assert code in verify, f"no branch for exit code {code}"


def test_a_rebuild_in_progress_is_not_reported_as_a_failure(installer):
    verify = installer.split("procedure VerifyInstallation()")[-1]
    migrating = verify.split("3:")[1].split("2:")[0]

    assert "installed successfully" in migrating
    assert "mbInformation" in migrating, "a healthy rebuild shows an error dialog"
    assert "restart Windows" not in migrating


def test_a_runtime_failure_never_prescribes_a_reboot(installer):
    """The specific harm: reinstalling over a machine mid-migration restarts the
    rebuild from zero and fixes nothing."""
    verify = installer.split("procedure VerifyInstallation()")[-1]
    runtime = verify.split("2:")[1].split("4:")[0]

    assert "Reinstalling will NOT help" in runtime
    assert "restart Windows" not in runtime
    assert "rag selfcheck" in runtime


def test_no_verifier_script_assumes_the_old_pass_fail_bit():
    """The exit code is a CATEGORY now, and scripts that predate that break.

    `verify_posix_upgrade.py` asserted `returncode in (0, 1)` — correct when a
    failure could only mean "the installation is wrong", and wrong the moment a
    runtime condition got its own code. A packaged artifact with no service
    running reports 2, so the check failed on a machine that was behaving
    exactly as designed. Caught by reading the callers, not by a test, which is
    why there is now a test.
    """
    import re

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    offenders = []
    for path in sorted(scripts.glob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()

        # Only the variables actually assigned from a selfcheck invocation.
        # Scanning every `returncode in (...)` in a file that merely mentions
        # selfcheck flagged an unrelated `service stop` check — the same
        # over-broad-pattern mistake this suite exists to avoid.
        subjects = {
            m.group(1)
            for line in lines
            if "selfcheck" in line and not line.lstrip().startswith("#")
            for m in [re.match(r"\s*(\w+)\s*=.*selfcheck", line)] if m
        }
        if not subjects:
            continue

        for line in lines:
            if line.lstrip().startswith("#"):
                continue
            for name in subjects:
                match = re.search(rf"\b{re.escape(name)}\.returncode\s+in\s+\(([^)]*)\)",
                                  line)
                if not match:
                    continue
                accepted = {p.strip() for p in match.group(1).split(",") if p.strip()}
                missing = {"2", "3"} - accepted
                if missing:
                    offenders.append(f"{path.name}: {name}.returncode accepts "
                                     f"{sorted(accepted)}, missing {sorted(missing)}")
    assert not offenders, (
        "a verifier script rejects exit codes this release defines:\n  "
        + "\n  ".join(offenders))


def test_the_file_replacement_message_survives_for_the_case_it_describes(installer):
    """It was never wrong — only wrongly applied to everything else."""
    verify = installer.split("procedure VerifyInstallation()")[-1]

    assert "some files were skipped" in verify
    assert "restart Windows" in verify
