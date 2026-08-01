"""A release-blocking suite that reports ``skipped`` did not run. Fail the build.

WP-R10. The gates that would have caught the v3.4 / v3.5.0 defects were declared
and never executed, and the evidence was sitting in plain sight the whole time:

    2581 passed, 19 skipped

Eight of those nineteen were ``tests/test_panel_e2e.py`` — the entire admin-panel
browser suite, skipped because ``RAG_E2E_PANEL_URL`` was unset and Playwright was
not even a declared dependency. Two more were
``tests/test_storage_managed_e2e.py`` — the managed engine against a real
``qdrant`` binary, skipped because ``RAG_E2E_QDRANT`` was unset. There were ZERO
occurrences of ``RAG_E2E`` anywhere under ``.github/workflows``. So the two paths
that produced the field incidents were validated by nothing, and the workflow
reported success while proving it.

A skip is not a failure, which is exactly the problem: a green run and a green
run with the interesting half missing look identical from outside. This script
makes them look different.

WHY IT CANNOT BE SATISFIED BY ACCIDENT
--------------------------------------
Four independent conditions must hold, and no single edit satisfies all four:

1. **A skip must be declared.** Every skipped test is matched against this file
   by NODE ID *and* by the reason it recorded. Renaming the test breaks the
   match; changing why it skips breaks the match. Both fail closed.
2. **A required test must be PRESENT and PASSED.** ``must_pass`` is checked
   against the report, so deleting the test, renaming its file, mis-typing a
   ``-k`` filter, or failing to collect the module at all is a failure — not a
   suite that quietly shrank to zero. This is the condition a skip-detector that
   only looked for ``<skipped>`` would miss entirely.
3. **A suite has a floor.** ``min_tests`` fails a report that collected fewer
   cases than the suite is known to contain, which is what a broken import or an
   over-eager filter actually looks like.
4. **An approved skip must be covered somewhere else.** An entry carrying
   ``covered_by`` is only valid if that suite REQUIRES the same node id to pass.
   So "this is fine, it runs in the dedicated job" is not a claim this file can
   make on its own — the dedicated job has to actually require it, and
   ``tests/test_ci_gates.py`` proves the two halves agree on every run of the
   ordinary suite.

Usage::

    python scripts/check_no_silent_skips.py --suite full-suite --junit junit.xml
    python scripts/check_no_silent_skips.py --self-check
    python scripts/check_no_silent_skips.py --list
"""

from __future__ import annotations

import argparse
import sys

# Trust boundary, stated because a linter will ask: the only input this parses
# is a JUnit report that the SAME job wrote seconds earlier with
# `pytest --junitxml`. It is never fetched, never user-supplied, and never
# crosses a machine. Adding `defusedxml` would put a dependency in the release
# path to harden against a document we generate ourselves.
import xml.etree.ElementTree as ET  # noqa: S405
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ApprovedSkip:
    """One named, justified exception. Anything not listed here fails."""

    #: pytest node id, e.g. ``tests/test_tray.py::test_generate_icon_...``.
    nodeid: str
    #: Substrings, any one of which the RECORDED skip reason must contain. A
    #: node id alone is not enough: a test that starts skipping for a new reason
    #: is a new fact about the suite and must be looked at, not inherited.
    reasons: tuple[str, ...]
    #: Why this skip is acceptable. Enforced non-empty by tests/test_ci_gates.py
    #: — an approval with no stated reason is how an allow-list becomes a
    #: rubber stamp.
    justification: str
    #: The suite in which this same test is REQUIRED to pass. Present whenever
    #: the skip is "it runs elsewhere"; absent only for genuine environment or
    #: platform limitations that no job can lift.
    covered_by: str | None = None


@dataclass(frozen=True)
class Suite:
    """A contract between a CI job and the report it produces."""

    name: str
    description: str
    #: Lower bound on collected cases. A report below it did not run the suite.
    min_tests: int
    #: Node ids that must appear AND must have passed.
    must_pass: tuple[str, ...] = ()
    approved_skips: tuple[ApprovedSkip, ...] = ()

    def approval_for(self, nodeid: str) -> ApprovedSkip | None:
        for entry in self.approved_skips:
            if entry.nodeid == nodeid:
                return entry
        return None


# --- the release-blocking e2e suites: nothing in them may skip ---------------
#
# These exist because the two field incidents came from exactly these two paths.
# There is no approved skip in either: every test is required to pass, and the
# job provisions whatever it needs (the pinned engine, a booted panel) rather
# than letting the suite excuse itself.

_MANAGED_QDRANT_E2E = Suite(
    name="managed-qdrant-e2e",
    description=(
        "tests/test_storage_managed_e2e.py against the real pinned qdrant "
        "binary fetched by scripts/fetch_qdrant.py"),
    min_tests=2,
    must_pass=(
        "tests/test_storage_managed_e2e.py::test_managed_qdrant_real_lifecycle_and_roundtrip",
        "tests/test_storage_managed_e2e.py::test_the_real_engine_actually_writes_to_its_log",
    ),
)

_PANEL_E2E = Suite(
    name="panel-e2e",
    description=(
        "tests/test_panel_e2e.py driven by Playwright against a real service "
        "booted by scripts/serve_panel_e2e.py on an isolated port and data dir"),
    min_tests=8,
    must_pass=(
        "tests/test_panel_e2e.py::test_diagnostics_screen_renders_identity",
        "tests/test_panel_e2e.py::test_dashboard_and_nav_render",
        "tests/test_panel_e2e.py::test_identity_api_reachable_from_browser",
        "tests/test_panel_e2e.py::test_diagnostics_shows_the_storage_engine",
        "tests/test_panel_e2e.py::test_diagnostics_lists_every_collection",
        "tests/test_panel_e2e.py::test_the_scale_warning_matches_the_engine",
        # Required, not optional. This test skips itself unless the panel is on
        # the per-project layout with two POPULATED project collections, so
        # requiring it here is what forces serve_panel_e2e.py to actually build
        # that state instead of booting an empty panel and calling it a pass.
        "tests/test_panel_e2e.py::test_per_project_search_is_isolated_in_the_browser",
        "tests/test_panel_e2e.py::test_no_console_errors_on_any_screen",
    ),
)


# --- the ordinary cross-platform suite ---------------------------------------
#
# Every skip observed on any of the three first-class platforms is named below
# with the reason it records. An entry that does not fire on a given platform is
# reported STALE and is not an error — a Windows-only skip is expected to be
# absent on Linux, and failing there would only teach people to delete entries.

_FULL_SUITE = Suite(
    name="full-suite",
    description="the whole suite, as run by release-validation's `suite` job",
    # The suite collected 2600 cases on 2026-08-01 (2581 passed, 19 skipped).
    # The floor is deliberately well below that: it exists to catch a report
    # that collapsed, not to freeze the test count.
    min_tests=2400,
    approved_skips=(
        # -- covered by a dedicated release-blocking job -----------------------
        # These are the nineteen's centre of gravity and the reason this file
        # exists. They stay approved HERE only because they are REQUIRED there.
        *[
            ApprovedSkip(
                nodeid=f"tests/test_panel_e2e.py::{name}",
                reasons=("panel e2e is resource-gated",),
                justification=(
                    "resource-gated on RAG_E2E_PANEL_URL; the `panel-e2e` job "
                    "boots a real panel and requires this test to pass"),
                covered_by="panel-e2e",
            )
            for name in (
                "test_diagnostics_screen_renders_identity",
                "test_dashboard_and_nav_render",
                "test_identity_api_reachable_from_browser",
                "test_diagnostics_shows_the_storage_engine",
                "test_diagnostics_lists_every_collection",
                "test_the_scale_warning_matches_the_engine",
                "test_per_project_search_is_isolated_in_the_browser",
                "test_no_console_errors_on_any_screen",
            )
        ],
        *[
            ApprovedSkip(
                nodeid=f"tests/test_storage_managed_e2e.py::{name}",
                reasons=("managed-Qdrant e2e is resource-gated",),
                justification=(
                    "resource-gated on RAG_E2E_QDRANT; the `managed-qdrant-e2e` "
                    "job fetches the pinned binary and requires this to pass"),
                covered_by="managed-qdrant-e2e",
            )
            for name in (
                "test_managed_qdrant_real_lifecycle_and_roundtrip",
                "test_the_real_engine_actually_writes_to_its_log",
            )
        ],

        # -- platform facts: the test asserts a property of ONE platform -------
        ApprovedSkip(
            nodeid=("tests/test_installer_quiescence_contract.py::"
                    "test_a_scheduled_task_that_is_not_registered_is_reported_not_thrown"),
            reasons=("Windows PowerShell is the interpreter under test",),
            justification=(
                "it EXECUTES the real `Get-TaskState` from `installer/quiesce.ps1` "
                "under Windows PowerShell and asks it for a task no machine has "
                "registered. The defect it guards is a property of that "
                "interpreter and of nothing else: under "
                "`$ErrorActionPreference = 'Stop'` a native command's stderr "
                "becomes a TERMINATING error the moment stream 2 is redirected, "
                "so `schtasks /query` announcing an absent task aborted the whole "
                "quiescence protocol before a single process was stopped. There "
                "is no schtasks, no Windows PowerShell and no such semantics off "
                "Windows, so no job can lift this. The source-level half of the "
                "same rule — "
                "test_no_native_command_can_turn_its_own_stderr_into_a_terminating_error"
                " — runs on every platform"),
        ),
        ApprovedSkip(
            nodeid="tests/test_engine_lifecycle.py::test_the_console_child_gets_no_window_and_is_never_detached",
            reasons=("console flags are Windows-only",),
            justification=(
                "CREATE_NO_WINDOW is a Win32 creation flag; there is nothing "
                "for it to assert on Linux or macOS"),
        ),
        ApprovedSkip(
            nodeid=("tests/test_installer_quiescence_contract.py::"
                    "test_the_real_enumerator_finds_an_engine_under_the_installation"
                    "_and_spares_one_outside"),
            reasons=("Windows PowerShell is the interpreter under test",),
            justification=(
                "it starts two real 64-bit processes named qdrant.exe and runs "
                "the real Get-OwnedProcess over them under EVERY PowerShell host "
                "on the machine — including the 32-bit one Inno's 32-bit Setup "
                "actually launches, where Process.Path is $null and "
                "Process.Modules is empty for every 64-bit process. There is no "
                "Windows PowerShell, no WOW64 and no {app} off Windows, so no "
                "job can lift this"),
        ),
        ApprovedSkip(
            nodeid="tests/test_scanner_resilience.py::test_a_junction_loop_does_not_multiply_the_index",
            reasons=("junctions are Windows-only",
                     "could not create a junction on this runner"),
            justification=(
                "NTFS junctions do not exist off Windows, and creating one can "
                "be refused even there"),
        ),
        ApprovedSkip(
            nodeid="tests/test_scanner_resilience.py::test_a_broken_junction_does_not_stop_the_scan",
            reasons=("junctions are Windows-only",),
            justification="NTFS junctions do not exist off Windows",
        ),
        ApprovedSkip(
            nodeid="tests/test_selfcheck.py::test_windows_declares_that_it_records_installed_versions",
            reasons=("registry is Windows-only",),
            justification="there is no Windows registry to read on Linux or macOS",
        ),
        ApprovedSkip(
            nodeid="tests/test_selfcheck.py::test_the_registry_is_really_read_and_never_raises",
            reasons=("registry is Windows-only",),
            justification="there is no Windows registry to read on Linux or macOS",
        ),

        # -- filesystem facts: the assertion depends on the FS, not the code ---
        ApprovedSkip(
            nodeid="tests/test_upgrade_scan.py::test_two_casings_of_one_directory_are_one_entry",
            reasons=("two casings are two directories on a case-sensitive",),
            justification=(
                "collapsing two casings is correct only where the filesystem "
                "collapses them; on ext4 they are two real directories"),
        ),
        ApprovedSkip(
            nodeid="tests/test_upgrade_scan.py::test_a_preferred_spelling_wins_so_the_installer_and_path_agree",
            reasons=("the two spellings are distinct directories here",),
            justification="same filesystem fact, from the other side",
        ),
        ApprovedSkip(
            nodeid="tests/test_upgrade_scan.py::test_a_case_sensitive_filesystem_keeps_both_spellings",
            reasons=("this filesystem treats the two spellings as one directory",),
            justification=(
                "the mirror of the two above: it can only run where the "
                "filesystem IS case-sensitive"),
        ),
        ApprovedSkip(
            nodeid="tests/test_dependency_architecture.py::test_a_symlinked_dependency_resolves_to_its_target",
            reasons=("symlink creation not permitted on this machine",
                     "no symlink support"),
            justification=(
                "creating a symlink needs a privilege an unelevated Windows "
                "account does not have"),
        ),

        # -- optional dependencies deliberately NOT shipped -------------------
        *[
            ApprovedSkip(
                nodeid=f"tests/test_onnx_encoder.py::{name}",
                reasons=("fastembed not installed",),
                justification=(
                    "fastembed is not a declared dependency; the ONNX encoder "
                    "is an alternative backend, and the tests that matter for "
                    "the shipped product (its protocol and its error message) "
                    "run without it"),
            )
            for name in ("test_live_onnx_encoder_satisfies_the_protocol",
                         "test_live_onnx_output_is_normalized",
                         "test_empty_batch_is_safe")
        ],
        ApprovedSkip(
            nodeid="tests/test_onnx_encoder.py::test_missing_dependency_gives_an_actionable_error",
            reasons=("fastembed IS installed here",),
            justification=(
                "the inverse gate: this one asserts the error message shown "
                "when fastembed is absent, so it cannot run where it is present"),
        ),
        *[
            ApprovedSkip(
                nodeid=f"tests/test_tray.py::{name}",
                reasons=("Pillow not installed",),
                justification=(
                    "Pillow lives in the optional `tray` extra; the icon "
                    "renderer is not installed by `.[dev]`"),
            )
            for name in ("test_generate_icon_returns_image_for_each_state",
                         "test_generate_icon_embeds_status_color_in_badge_corner",
                         "test_generate_icon_fallback_when_logo_missing")
        ],

        # -- environment facts ------------------------------------------------
        ApprovedSkip(
            nodeid="tests/test_config_isolation.py::test_the_worktree_config_is_not_what_tests_see",
            reasons=("no developer ragtools.toml in the working directory",),
            justification=(
                "it proves the suite ignores a developer's own config; a CI "
                "checkout has none, so there is nothing to ignore"),
        ),
        *[
            ApprovedSkip(
                nodeid=f"tests/test_process.py::{name}",
                reasons=("Could not find a dead PID on this system",),
                justification=(
                    "the helper walks down from PID 3,999,999 looking for one "
                    "that is not alive; on a machine where every candidate is "
                    "live there is no dead PID to assert against"),
            )
            for name in (
                "test_read_pid_raw_returns_value_without_liveness_check",
                "test_read_pid_returns_none_for_dead_pid",
                "test_read_pid_self_heals_stale_file",
                "test_service_status_reports_not_running_for_stale_pid",
                "test_clean_stale_pid_is_idempotent",
                "test_stop_service_returns_true_on_fast_graceful_exit",
            )
        ],
    ),
)


SUITES: dict[str, Suite] = {
    s.name: s for s in (_FULL_SUITE, _MANAGED_QDRANT_E2E, _PANEL_E2E)
}


# --- report parsing ----------------------------------------------------------


@dataclass
class Case:
    nodeid: str
    outcome: str          # passed | failed | error | skipped | xfailed
    reason: str = ""


@dataclass
class Report:
    tests: int = 0
    failures: int = 0
    errors: int = 0
    cases: list[Case] = field(default_factory=list)


def _module_file(dotted: str) -> str:
    """``tests.test_foo`` -> ``tests/test_foo.py``, tolerating rubbish."""
    parts = [p for p in dotted.replace("/", ".").replace("\\", ".").split(".") if p]
    if not parts:
        return "<unknown>"
    if parts[-1] == "py":
        parts = parts[:-1] or ["<unknown>"]
    return Path(*parts).with_suffix(".py").as_posix()


def _module_path(classname: str) -> tuple[str, list[str]]:
    """Split a junit ``classname`` into (file path, enclosing class chain).

    pytest writes ``tests.test_foo`` for a module-level test and
    ``tests.test_foo.TestBar`` for one inside a class, and does not say which is
    which. Resolve it against the filesystem when the checkout is present, and
    fall back to "a segment starting with an upper-case letter is a class" only
    when it is not.
    """
    parts = [p for p in classname.split(".") if p]
    if not parts:
        return "<unknown>", []
    for cut in range(len(parts), 0, -1):
        candidate = Path(*parts[:cut]).with_suffix(".py")
        if (ROOT / candidate).is_file():
            return candidate.as_posix(), parts[cut:]
    cut = len(parts)
    while cut > 1 and parts[cut - 1][:1].isupper():
        cut -= 1
    return Path(*parts[:cut]).with_suffix(".py").as_posix(), parts[cut:]


def _nodeid(testcase: ET.Element) -> str:
    """The pytest node id for a ``<testcase>``.

    A node id with NO ``::`` means a whole module, and that shape is not a
    curiosity — it is the most dangerous entry a report can contain. When a
    module-level ``importorskip`` fires, pytest emits ONE case with an empty
    ``classname`` and the module's dotted path as the ``name``:

        <testcase classname="" name="tests.test_panel_e2e">
          <skipped message="collection skipped">... playwright not installed</skipped>

    The module then contributes no test results whatsoever, so a gate that only
    inspected per-test outcomes would read a clean report. Observed for real on
    2026-08-01: the panel suite booted, indexed, and reported ONE skipped case
    where eight tests should have been. :func:`check` treats a bare module path
    as unapprovable for exactly that reason.
    """
    name = testcase.get("name") or "?"
    filename = testcase.get("file")
    classname = testcase.get("classname") or ""

    if not classname and "." in name and not filename:
        return _module_file(name)

    if filename:
        path, chain = filename.replace("\\", "/"), []
        dotted = path[:-3].replace("/", ".") if path.endswith(".py") else path
        if classname.startswith(dotted + "."):
            chain = classname[len(dotted) + 1:].split(".")
    else:
        path, chain = _module_path(classname)
    return "::".join([path, *chain, name])


def parse_junit(path: Path) -> Report:
    root = ET.parse(path).getroot()
    suites = root.findall(".//testsuite") or ([root] if root.tag == "testsuite" else [])
    if not suites:
        raise SystemExit(f"{path}: no <testsuite> element — this is not a pytest "
                         f"JUnit report")

    report = Report()
    for suite in suites:
        report.tests += int(suite.get("tests") or 0)
        report.failures += int(suite.get("failures") or 0)
        report.errors += int(suite.get("errors") or 0)
        for tc in suite.findall("testcase"):
            nodeid = _nodeid(tc)
            skipped = tc.find("skipped")
            if skipped is not None:
                kind = (skipped.get("type") or "").lower()
                outcome = "xfailed" if "xfail" in kind else "skipped"
                reason = skipped.get("message") or (skipped.text or "")
                report.cases.append(Case(nodeid, outcome, reason.strip()))
            elif tc.find("failure") is not None:
                report.cases.append(Case(nodeid, "failed"))
            elif tc.find("error") is not None:
                report.cases.append(Case(nodeid, "error"))
            else:
                report.cases.append(Case(nodeid, "passed"))
    return report


# --- the checks --------------------------------------------------------------


def self_check() -> list[str]:
    """Invariants of this file alone. Also asserted by tests/test_ci_gates.py."""
    problems: list[str] = []
    for suite in SUITES.values():
        for entry in suite.approved_skips:
            if not entry.justification.strip():
                problems.append(
                    f"{suite.name}: {entry.nodeid} is approved with no "
                    f"justification")
            if not entry.reasons or not all(r.strip() for r in entry.reasons):
                problems.append(
                    f"{suite.name}: {entry.nodeid} declares no skip reason to "
                    f"match, so any future skip of it would be accepted")
            if entry.covered_by is None:
                continue
            owner = SUITES.get(entry.covered_by)
            if owner is None:
                problems.append(
                    f"{suite.name}: {entry.nodeid} claims cover from unknown "
                    f"suite {entry.covered_by!r}")
            elif entry.nodeid not in owner.must_pass:
                problems.append(
                    f"{suite.name}: {entry.nodeid} is excused because "
                    f"{entry.covered_by!r} covers it, but that suite does not "
                    f"REQUIRE it to pass. An excuse nobody honours is a hole.")
    return problems


def check(suite: Suite, reports: list[tuple[Path, Report]]) -> int:
    merged = Report()
    for _, report in reports:
        merged.tests += report.tests
        merged.failures += report.failures
        merged.errors += report.errors
        merged.cases.extend(report.cases)

    print(f"suite      : {suite.name}")
    print(f"purpose    : {suite.description}")
    print(f"reports    : {', '.join(str(p) for p, _ in reports)}")
    print(f"collected  : {merged.tests} case(s); "
          f"{merged.failures} failure(s), {merged.errors} error(s)")

    failures: list[str] = []

    if merged.errors or merged.failures:
        failures.append(
            f"the report records {merged.failures} failure(s) and "
            f"{merged.errors} error(s). A collection error produces no test "
            f"result at all, which looks the same as a suite that had nothing "
            f"to run.")

    if merged.tests < suite.min_tests:
        failures.append(
            f"only {merged.tests} case(s) were collected; this suite is known "
            f"to contain at least {suite.min_tests}. Something filtered, failed "
            f"to import, or was renamed out of collection.")

    by_id: dict[str, Case] = {}
    for case in merged.cases:
        by_id.setdefault(case.nodeid, case)

    # 1. every skip must be declared, by node id AND by recorded reason
    skipped = [c for c in merged.cases if c.outcome in ("skipped", "xfailed")]
    print(f"skipped    : {len(skipped)}")
    seen_approvals: set[str] = set()
    for case in skipped:
        if "::" not in case.nodeid:
            # A whole module, not a test. Never approvable: an approval names a
            # test, and there is no test here to name — the module produced no
            # results at all. See _nodeid for the shape and where it was seen.
            failures.append(
                f"WHOLE MODULE NEVER COLLECTED  {case.nodeid}\n"
                f"      reason: {case.reason or '(none recorded)'}\n"
                f"      Every test in it is missing from this report, so no "
                f"per-test outcome exists to be wrong. Usually a module-level "
                f"importorskip whose dependency the job did not install.")
            continue
        entry = suite.approval_for(case.nodeid)
        if entry is None:
            failures.append(
                f"UNDECLARED SKIP  {case.nodeid}\n"
                f"      reason: {case.reason or '(none recorded)'}\n"
                f"      A release-blocking suite reported this test as "
                f"{case.outcome}. Either make it run, or declare it in "
                f"SUITES[{suite.name!r}].approved_skips with a justification.")
            continue
        if not any(r in case.reason for r in entry.reasons):
            failures.append(
                f"SKIP REASON CHANGED  {case.nodeid}\n"
                f"      recorded: {case.reason or '(none recorded)'}\n"
                f"      approved: {' | '.join(entry.reasons)}\n"
                f"      The approval was granted for a different reason. A test "
                f"that starts skipping for a new one is a new fact.")
            continue
        seen_approvals.add(entry.nodeid)
        print(f"  approved   {case.nodeid}\n"
              f"             ({case.reason})")

    # 2. every required test must be present AND passed
    for nodeid in suite.must_pass:
        case = by_id.get(nodeid)
        if case is None:
            failures.append(
                f"REQUIRED TEST DID NOT RUN  {nodeid}\n"
                f"      It is not in the report at all — deleted, renamed, "
                f"filtered out, or never collected. This is the failure a "
                f"skip-detector that only looked for <skipped> would miss.")
        elif case.outcome != "passed":
            failures.append(
                f"REQUIRED TEST DID NOT PASS  {nodeid}  ({case.outcome}"
                + (f": {case.reason}" if case.reason else "") + ")")

    if suite.must_pass:
        ran = sum(1 for n in suite.must_pass
                  if (c := by_id.get(n)) is not None and c.outcome == "passed")
        print(f"required   : {ran}/{len(suite.must_pass)} passed")

    # 3. stale approvals: reported, never fatal. A Windows-only skip is SUPPOSED
    #    to be absent on Linux, and failing there would only teach people to
    #    delete entries until the file stopped complaining.
    stale = [e.nodeid for e in suite.approved_skips
             if e.nodeid not in seen_approvals]
    if stale:
        print(f"stale      : {len(stale)} approved skip(s) did not fire here "
              f"(expected across platforms; review if it is every platform)")
        for nodeid in sorted(stale):
            print(f"  unused     {nodeid}")

    print("")
    if failures:
        print(f"SILENT-SKIP GATE: FAILED — {len(failures)} problem(s)\n")
        for problem in failures:
            print(f"  * {problem}")
        return 1

    print("SILENT-SKIP GATE: every required test ran and passed; every skip is "
          "declared and justified.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", help=f"one of: {', '.join(sorted(SUITES))}")
    parser.add_argument("--junit", action="append", type=Path, default=[],
                        help="a pytest --junitxml report (repeatable)")
    parser.add_argument("--self-check", action="store_true",
                        help="verify this file's own invariants and exit")
    parser.add_argument("--list", action="store_true",
                        help="print the declared suites and exit")
    args = parser.parse_args(argv)

    if args.list:
        for name, suite in sorted(SUITES.items()):
            print(f"{name}\n  {suite.description}\n"
                  f"  min_tests={suite.min_tests} "
                  f"must_pass={len(suite.must_pass)} "
                  f"approved_skips={len(suite.approved_skips)}")
        return 0

    problems = self_check()
    if problems:
        print("SILENT-SKIP GATE: the gate's own declaration is inconsistent")
        for problem in problems:
            print(f"  * {problem}")
        return 1
    if args.self_check:
        print("SILENT-SKIP GATE: declaration is self-consistent.")
        return 0

    if not args.suite:
        parser.error("--suite is required (or use --self-check / --list)")
    suite = SUITES.get(args.suite)
    if suite is None:
        parser.error(f"unknown suite {args.suite!r}; "
                     f"declared: {', '.join(sorted(SUITES))}")
    if not args.junit:
        parser.error("--junit is required: this gate reads a machine-readable "
                     "report, never console output")

    reports: list[tuple[Path, Report]] = []
    for path in args.junit:
        if not path.is_file():
            # The most important failure in the file. A job whose pytest step
            # died before writing a report must not reach a gate that then
            # finds nothing to complain about.
            print(f"SILENT-SKIP GATE: FAILED — no report at {path}. pytest "
                  f"never wrote one, which means the suite did not finish.")
            return 1
        reports.append((path, parse_junit(path)))

    return check(suite, reports)


if __name__ == "__main__":
    sys.exit(main())
