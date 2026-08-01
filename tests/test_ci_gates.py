"""WP-R10 — the gates exist, they execute, and the gate can see them.

Companion to ``test_workflow_contract.py``, which owns the R12 rule (a leg that
cannot fail the release must not be able to delay it). This module owns the R10
rule, which is the opposite failure:

    A gate that is declared but never executes is indistinguishable, from
    outside, from a gate that passed.

The evidence was a summary line nobody read as a defect::

    2581 passed, 19 skipped

Ten of those nineteen were the managed-engine e2e and the entire admin-panel
browser suite. Both are env-gated; no workflow set the variables; there were
ZERO occurrences of ``RAG_E2E`` anywhere under ``.github/workflows``. So the two
code paths behind the field incidents were validated by nothing, and the
workflow reported success while proving it.

Three families of assertion below:

1. **The skip gate's declaration is real.** Every node id it names must resolve
   to a test that actually exists, so renaming a test breaks the contract here —
   in the ordinary suite, on a developer's machine — rather than in CI six weeks
   later.
2. **The skip gate has teeth.** Synthetic reports drive each failure mode. A
   structural check that has never been shown to FAIL is not evidence of
   anything (this project has produced four vacuous ones in a single session).
3. **The workflow and the gate agree.** Every job is visible to the release
   gate, and every name the gate evaluates is a job it waits on.
"""

from __future__ import annotations

import ast
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from scripts.check_no_silent_skips import (  # noqa: E402
    SUITES,
    main as skipgate_main,
    self_check,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE_VALIDATION = WORKFLOWS / "release-validation.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _defined_tests(path: Path) -> set[str]:
    """Top-level test functions and ``Class::method`` pairs in a test module."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(f"{node.name}::{child.name}")
    return names


def _all_declared_nodeids() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for suite in SUITES.values():
        out += [(suite.name, n) for n in suite.must_pass]
        out += [(suite.name, e.nodeid) for e in suite.approved_skips]
    return out


DECLARED_NODEIDS = _all_declared_nodeids()
#: `::` inside a parametrize id makes the case unselectable by node id, which
#: is the one thing you want when this test is the failure you are chasing.
DECLARED_IDS = [f"{suite}:{nodeid.replace('::', '-')}"
                for suite, nodeid in DECLARED_NODEIDS]


# --- 1. the declaration describes tests that exist ---------------------------


def test_the_skip_gate_declaration_is_self_consistent():
    problems = self_check()
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("suite_name,nodeid", DECLARED_NODEIDS, ids=DECLARED_IDS)
def test_every_node_id_the_skip_gate_names_resolves_to_a_real_test(
        suite_name: str, nodeid: str):
    """The property that makes a rename fail HERE instead of in CI.

    ``must_pass`` is the contract "this test ran and passed". If the test is
    renamed and the contract is not, CI starts reporting REQUIRED TEST DID NOT
    RUN — correct, but discovered on a tag push. This finds it on the next
    ``pytest``.
    """
    path, _, rest = nodeid.partition("::")
    assert (ROOT / path).is_file(), f"{suite_name}: {path} does not exist"
    assert rest, f"{suite_name}: {nodeid} names no test"
    assert rest in _defined_tests(Path(path)), (
        f"{suite_name}: {nodeid} does not exist. Either the test was renamed "
        f"or removed and scripts/check_no_silent_skips.py was not updated — a "
        f"required test that cannot be found is a gate that cannot fire.")


def test_every_approved_skip_states_why_it_is_acceptable():
    for suite in SUITES.values():
        for entry in suite.approved_skips:
            assert entry.justification.strip(), (
                f"{suite.name}: {entry.nodeid} is approved with no reason. An "
                f"allow-list whose entries need no argument is a rubber stamp.")
            assert entry.reasons and all(r.strip() for r in entry.reasons), (
                f"{suite.name}: {entry.nodeid} matches on node id alone, so it "
                f"would inherit the approval for any future skip reason.")


def test_a_skip_excused_as_covered_elsewhere_is_actually_required_there():
    """The hinge of the whole mechanism.

    The ordinary suite is allowed to skip the panel and managed-engine tests
    ONLY because the dedicated jobs require them to pass. If someone adds a new
    env-gated test and approves its skip with ``covered_by``, that claim is
    empty unless the named suite lists it in ``must_pass``. This is what stops
    the allow-list from becoming a way to make new silent skips legal.
    """
    excused = [(s.name, e) for s in SUITES.values()
               for e in s.approved_skips if e.covered_by]
    assert excused, (
        "no approved skip claims cover from another suite. If the e2e suites "
        "stopped being excused this way, this test is measuring nothing — "
        "check that the declaration still routes them to a dedicated job.")
    for suite_name, entry in excused:
        owner = SUITES.get(entry.covered_by)
        assert owner is not None, (
            f"{suite_name}: {entry.nodeid} names unknown suite {entry.covered_by!r}")
        assert entry.nodeid in owner.must_pass, (
            f"{suite_name}: {entry.nodeid} is excused because "
            f"{entry.covered_by!r} covers it, but that suite does not require "
            f"it to pass.")


# --- 2. the gate has teeth: every failure mode, driven ------------------------


def _report(tmp_path: Path, cases: list[tuple[str, str, str, str]],
            *, tests: int | None = None, failures: int = 0,
            errors: int = 0) -> Path:
    """Write a synthetic JUnit report. ``cases`` = (classname, name, kind, msg)."""
    body = []
    for classname, name, kind, message in cases:
        inner = ""
        if kind == "skipped":
            inner = f'<skipped type="pytest.skip" message="{message}"/>'
        elif kind == "failure":
            inner = f'<failure message="{message}"/>'
        body.append(
            f'<testcase classname="{classname}" name="{name}">{inner}</testcase>')
    total = len(cases) if tests is None else tests
    path = tmp_path / "report.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite '
        f'name="pytest" errors="{errors}" failures="{failures}" '
        f'skipped="0" tests="{total}">' + "".join(body)
        + "</testsuite></testsuites>",
        encoding="utf-8")
    return path


def _run(suite: str, report: Path) -> int:
    return skipgate_main(["--suite", suite, "--junit", str(report)])


def _passing_managed_cases() -> list[tuple[str, str, str, str]]:
    return [("tests.test_storage_managed_e2e", name, "passed", "")
            for name in ("test_managed_qdrant_real_lifecycle_and_roundtrip",
                         "test_the_real_engine_actually_writes_to_its_log")]


def test_a_clean_report_passes_the_gate(tmp_path):
    """The positive control. Without it, every assertion below could be passing
    because the gate rejects everything."""
    assert _run("managed-qdrant-e2e", _report(tmp_path, _passing_managed_cases())) == 0


def test_a_required_test_that_reports_skipped_fails_the_gate(tmp_path):
    """THE case this work package exists for: exactly what the suite did for two
    releases, now a build failure."""
    cases = _passing_managed_cases()
    cases[0] = (cases[0][0], cases[0][1], "skipped",
                "managed-Qdrant e2e is resource-gated; set RAG_E2E_QDRANT=1 to run")
    assert _run("managed-qdrant-e2e", _report(tmp_path, cases)) == 1


def test_a_required_test_that_is_absent_entirely_fails_the_gate(tmp_path):
    """The failure a skip-detector that only looked for ``<skipped>`` would miss.

    A renamed file, a mis-typed ``-k``, or a module that failed to import
    produces NO test case at all — which looks like nothing rather than like a
    problem.
    """
    assert _run("managed-qdrant-e2e",
                _report(tmp_path, _passing_managed_cases()[:1], tests=1)) == 1


def test_a_skip_for_an_undeclared_reason_fails_the_gate(tmp_path):
    """An approval is granted for a stated reason, not to a node id forever."""
    cases = [("tests.test_tray", "test_generate_icon_returns_image_for_each_state",
              "skipped", "TODO: flaky, look at this later")]
    assert _run("full-suite", _report(tmp_path, cases, tests=2600)) == 1


def test_a_declared_skip_for_its_declared_reason_passes(tmp_path):
    cases = [("tests.test_tray", "test_generate_icon_returns_image_for_each_state",
              "skipped", "Pillow not installed; skipping icon-render test")]
    assert _run("full-suite", _report(tmp_path, cases, tests=2600)) == 0


def test_a_brand_new_undeclared_skip_fails_the_gate(tmp_path):
    """The regression this file is really guarding: someone adds an env-gated
    test, nothing sets the variable, and the suite line gains a twentieth skip."""
    cases = [("tests.test_indexing", "test_something_new", "skipped",
              "resource-gated; set RAG_E2E_SOMETHING=1 to run")]
    assert _run("full-suite", _report(tmp_path, cases, tests=2600)) == 1


def test_a_collapsed_report_fails_the_gate(tmp_path):
    """Below the floor is a suite that did not run, however green its cases."""
    assert _run("full-suite", _report(tmp_path, [], tests=12)) == 1


def test_a_module_that_was_never_collected_fails_the_gate(tmp_path):
    """The shape a module-level ``importorskip`` actually produces.

    Not hypothetical: this exact report was generated on 2026-08-01 by booting
    the panel harness with Playwright absent. pytest emits ONE case with an
    EMPTY classname and the module's dotted path as the name —

        <testcase classname="" name="tests.test_panel_e2e">
          <skipped message="collection skipped">... playwright not installed

    — and every test in the module is simply missing. The first version of the
    parser raised ``ValueError`` on it, which is a failure but not a diagnosis.
    """
    path = tmp_path / "collection-skip.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite '
        'name="pytest" errors="0" failures="0" skipped="1" tests="1">'
        '<testcase classname="" name="tests.test_panel_e2e">'
        '<skipped message="collection skipped">playwright not installed</skipped>'
        "</testcase></testsuite></testsuites>", encoding="utf-8")
    assert _run("panel-e2e", path) == 1


def test_a_report_carrying_errors_fails_the_gate(tmp_path):
    assert _run("managed-qdrant-e2e",
                _report(tmp_path, _passing_managed_cases(), errors=1)) == 1


def test_a_missing_report_fails_the_gate(tmp_path):
    """A pytest step that died before writing a report must not reach a gate
    that then finds nothing to complain about."""
    assert _run("panel-e2e", tmp_path / "never-written.xml") == 1


def test_the_gate_refuses_a_suite_it_does_not_know(tmp_path):
    with pytest.raises(SystemExit):
        _run("no-such-suite", _report(tmp_path, []))


def test_the_skip_gate_self_check_runs_as_a_subprocess():
    """It is invoked from CI as a script, not as an import."""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_no_silent_skips.py"),
         "--self-check"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- 3. the workflow and the gate agree --------------------------------------


def _gate_lists() -> dict[str, set[str]]:
    """The job names the release gate actually evaluates, by list."""
    jobs = _load(RELEASE_VALIDATION)["jobs"]
    steps = jobs["gate"]["steps"]
    env: dict = {}
    for step in steps:
        env.update(step.get("env") or {})
    lists: dict[str, set[str]] = {}
    for key in ("BLOCKING", "TAG_ONLY"):
        block = env.get(key, "")
        lists[key] = {
            m.group(1) for m in
            re.finditer(r"^\s*(\S+)\s+\$\{\{\s*needs\.", block, re.MULTILINE)
        }
    return lists


def test_every_job_is_visible_to_the_release_gate():
    """A job the gate does not evaluate cannot fail the release.

    That is how this gate became decoration the first time: it named jobs in
    `needs`, echoed, and asserted nothing. The correction was to compare
    results — and a correction that only covers the jobs that existed that day
    rots the moment someone adds one.
    """
    doc = _load(RELEASE_VALIDATION)
    jobs = doc["jobs"]
    gate = jobs["gate"]
    needs = set(gate.get("needs") or [])
    lists = _gate_lists()
    evaluated = lists["BLOCKING"] | lists["TAG_ONLY"]

    for name, job in jobs.items():
        if name == "gate":
            continue
        if job.get("continue-on-error") is True:
            # R12: a best-effort leg must NOT be waited on. Asserted by
            # test_workflow_contract.py; restated here so this test cannot be
            # read as demanding the opposite.
            assert name not in needs, (
                f"{name} is best-effort and must not be in the gate's needs")
            continue
        assert name in needs, (
            f"{name} is release-blocking but the gate does not wait on it. Add "
            f"it to `gate.needs` AND to BLOCKING (or TAG_ONLY).")
        assert name in evaluated, (
            f"{name} is in the gate's `needs` but in neither BLOCKING nor "
            f"TAG_ONLY, so the gate waits for it and then ignores its result. "
            f"That is worse than not waiting at all.")


def test_the_gate_evaluates_nothing_it_does_not_wait_on():
    doc = _load(RELEASE_VALIDATION)
    jobs = doc["jobs"]
    needs = set(jobs["gate"].get("needs") or [])
    for key, names in _gate_lists().items():
        for name in names:
            assert name in jobs, f"{key} names {name!r}, which is not a job"
            assert name in needs, (
                f"{key} evaluates {name!r} but the gate does not wait on it, so "
                f"its result will be read before it is known")


def test_the_tag_only_leg_is_conditioned_on_a_tag():
    """The winget check must not run — and must not fail — on a pre-release PR.

    The manifest cannot legitimately be correct before the installer it
    describes exists, so a check that ran there would be permanently red, and a
    permanently red check is one people learn to ignore.
    """
    jobs = _load(RELEASE_VALIDATION)["jobs"]
    for name in _gate_lists()["TAG_ONLY"]:
        condition = str(jobs[name].get("if") or "")
        assert "refs/tags/" in condition, (
            f"{name} is evaluated as tag-only but its `if:` does not restrict "
            f"it to a tag push: {condition!r}")


# --- the defect itself, asserted directly ------------------------------------


def test_the_resource_gated_suites_are_actually_wired_into_a_workflow():
    """There were ZERO `RAG_E2E` references under .github/workflows.

    That single fact is the whole of WP-R10: two suites that gate themselves on
    an environment variable, and nothing anywhere that sets it. This test fails
    if that state is ever restored.
    """
    text = "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted(WORKFLOWS.glob("*.yml")))
    assert "RAG_E2E_QDRANT" in text, (
        "no workflow sets RAG_E2E_QDRANT, so tests/test_storage_managed_e2e.py "
        "skips everywhere and the managed engine is validated by nothing")
    assert "serve_panel_e2e.py" in text or "RAG_E2E_PANEL_URL" in text, (
        "no workflow boots a panel, so tests/test_panel_e2e.py skips everywhere "
        "and the admin panel is validated by nothing")


def test_every_pytest_job_in_every_workflow_runs_the_skip_detector():
    """A suite whose report nobody parses can go back to skipping silently.

    Across ALL workflows, not just the release one. A rule enforced in one file
    and not the next is a rule with somewhere for a new silent skip to land.
    """
    checked = 0
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, job in (_load(path).get("jobs") or {}).items():
            steps = job.get("steps") or []
            runs = [str(s.get("run") or "") for s in steps if isinstance(s, dict)]
            if not any("--junitxml" in r for r in runs):
                continue
            checked += 1
            assert any("check_no_silent_skips.py" in r for r in runs), (
                f"{path.name}:{name} produces a JUnit report and never checks "
                f"it. The report exists so a skip cannot hide in a summary line.")
    assert checked >= 2, (
        f"only {checked} job(s) produce a JUnit report; both the release "
        f"`suite` and the ordinary `test` workflow should")


def test_the_stress_gate_exceeds_the_range_that_broke_v3_4():
    """A stress run below the wall cannot say anything about the wall.

    The v3.4 failure was ephemeral-port exhaustion against Windows' 16,384-wide
    dynamic range. The script refuses a smaller request count; this asserts the
    default it ships with is already past it.
    """
    from scripts.stress_transport import EPHEMERAL_RANGE

    text = (ROOT / "scripts" / "stress_transport.py").read_text(encoding="utf-8")
    match = re.search(r'"--requests", type=int, default=(\d+)', text)
    assert match, "the stress script no longer declares a default request count"
    assert int(match.group(1)) > EPHEMERAL_RANGE


def test_the_stress_gate_runs_on_all_three_first_class_platforms():
    """The failure was Windows-only, so a Linux-only stress job proves nothing
    about it — and the fix is shared code, so a Windows-only one proves nothing
    about a regression elsewhere."""
    job = _load(RELEASE_VALIDATION)["jobs"]["transport-stress"]
    labels = set((job.get("strategy") or {}).get("matrix", {}).get("os") or [])
    assert {"ubuntu-latest", "macos-14", "windows-latest"} <= labels, labels


# --- the stress gate's negative control has to work on every platform --------
#
# Run 30676422208, all three legs red, and the script was right to refuse:
#
#   NEGATIVE CONTROL — keep-alive disabled, the v3.4 configuration
#     control (max_keepalive_connections=0)
#       requests          : 3000 in 4.0s
#       S1 distinct ports : 1 (reuse factor 3000.0x, 33 samples)
#   * the NEGATIVE CONTROL did not trip: 1 distinct port(s), reuse 3000.0x,
#     against a limit of 256 / 20x.
#
# On Windows the identical control produces 1,743 distinct ports for 3,000
# requests. On Linux it produces ONE, because the kernel reuses the local port
# for successive connections to the same peer — so S1 there reads a
# keep-alive-DISABLED client as the healthiest possible result. The rule the
# script states is right and stays: a control that cannot see the known-bad case
# invalidates the run. What was wrong was asserting a Windows-shaped signal on a
# platform that does not produce it.

#: The runner label -> ``platform.system()`` the script sees there.
_MATRIX_SYSTEMS = {
    "ubuntu-latest": "Linux",
    "macos-14": "Darwin",
    "windows-latest": "Windows",
}


def _outcome(**kwargs):
    from scripts.stress_transport import Outcome

    base = dict(requests=0, seconds=1.0, retries=0, connects=0, distinct_ports=0,
                peak_open=1, samples=100, socket_growth=0,
                time_wait_before=None, time_wait_after=None, sampler_error="")
    base.update(kwargs)
    return Outcome(**base)


def test_every_platform_the_stress_gate_runs_on_has_a_designated_control_signal():
    """A platform with no designated signal is a platform with no control."""
    from scripts.stress_transport import control_signals

    job = _load(RELEASE_VALIDATION)["jobs"]["transport-stress"]
    labels = (job.get("strategy") or {}).get("matrix", {}).get("os") or []
    assert labels, "the stress gate has no matrix at all"
    for label in labels:
        system = _MATRIX_SYSTEMS.get(label)
        assert system, f"the matrix runs on {label} and nothing maps it to a platform"
        assert control_signals(system), (
            f"{label} ({system}) has no signal its negative control is judged on"
        )


def test_the_recorded_negative_control_trips_every_signal_its_platform_designates():
    """The measurement, not the threshold — asked of the numbers CI actually
    produced. The Linux control that failed run 30676422208 must now trip, and
    the Windows one must still trip on the signal it always did."""
    from scripts.stress_transport import (
        SIGNAL_CONNECTS,
        SIGNAL_PORTS,
        control_signals,
    )

    # Recorded: ubuntu-latest, keep-alive disabled, 3000 requests.
    linux_control = _outcome(requests=3000, connects=3000, distinct_ports=1)
    # Recorded: windows-latest, keep-alive disabled, 3000 requests.
    windows_control = _outcome(requests=3000, connects=3000, distinct_ports=1743)

    for system, control in (("Linux", linux_control), ("Darwin", linux_control),
                            ("Windows", windows_control)):
        for signal in control_signals(system):
            assert control.trips(signal), (
                f"on {system} the keep-alive-disabled control does not trip "
                f"{signal}: {control.explain(signal)}. A measurement that cannot "
                f"see the known-bad case cannot vouch for the good one."
            )

    assert not linux_control.trips(SIGNAL_PORTS), (
        "this test has stopped modelling the recorded failure: S1 is supposed to "
        "be blind to the Linux control, which is exactly why it is not designated "
        "there"
    )
    assert SIGNAL_PORTS in control_signals("Windows"), (
        "S1 is the WINDOWS failure signature — [WinError 10048] is ephemeral-port "
        "exhaustion — and dropping it there would remove the only signal taken "
        "where the incident actually happened"
    )
    assert SIGNAL_CONNECTS in control_signals("Windows"), (
        "the Windows control must trip the cross-platform signal too, or the two "
        "platforms are judged by unrelated measurements"
    )


def test_the_shipped_client_still_has_to_clear_every_designated_signal():
    """The control tripping is only half of it: the same thresholds must pass the
    real client, or the gate would be red everywhere."""
    from scripts.stress_transport import control_signals

    # Recorded: ubuntu-latest, the shipped pool, 20,000 requests, 11 ports.
    shipped = _outcome(requests=20000, connects=11, distinct_ports=11)
    for system in ("Linux", "Darwin", "Windows"):
        for signal in control_signals(system):
            assert not shipped.trips(signal), (
                f"the shipped client fails {signal} on {system}: "
                f"{shipped.explain(signal)}"
            )


def test_the_time_wait_signal_measures_the_run_rather_than_the_machine():
    """S3 printed 3035 for a 3,000-request control AND for a 20,000-request run
    in the same job. An identical number for two different workloads is not a
    measurement of either — TIME_WAIT lasts about a minute, so the second reading
    was still counting the first run's residue."""
    residue = _outcome(requests=20000, time_wait_before=3035, time_wait_after=3035)
    assert residue.time_wait_delta == 0, (
        "S3 still reports an absolute system-wide count, so a run that opened "
        "almost no connections is credited with everything the machine happened "
        "to be holding"
    )
    churned = _outcome(requests=3000, time_wait_before=35, time_wait_after=3035)
    assert churned.time_wait_delta == 3000

    unavailable = _outcome(time_wait_before=None, time_wait_after=12)
    assert unavailable.time_wait_delta is None, (
        "a delta that could not be taken must be None, never 0 — that is how a "
        "measurement that was not taken starts reading as a healthy result"
    )


def test_the_connection_counter_separates_a_leaky_client_from_the_shipped_one():
    """S0 against a REAL httpx client, which is what the gate drives.

    The whole point of replacing the control's signal is that the new one has to
    discriminate where the old one could not — so this measures it rather than
    asserting that a constant is present. Loopback only; no engine, no network.
    """
    import http.server
    import threading as _threading

    httpx = pytest.importorskip("httpx")
    from scripts.stress_transport import ConnectCounter

    class Quiet(http.server.BaseHTTPRequestHandler):
        # HTTP/1.1, or the SERVER closes every connection and both clients look
        # equally leaky — the measurement would then be of this fixture rather
        # than of the client. A real Qdrant speaks 1.1.
        protocol_version = "HTTP/1.1"

        def do_GET(self):                               # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):                  # noqa: D102
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    port = server.server_address[1]
    thread = _threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = socket.socket.connect
    try:
        counted = {}
        for label, limits in (
            ("leaky", httpx.Limits(max_connections=None, max_keepalive_connections=0)),
            ("pooled", httpx.Limits(max_connections=32, max_keepalive_connections=32)),
        ):
            with ConnectCounter(port) as counter:
                with httpx.Client(limits=limits, timeout=10) as client:
                    for _ in range(40):
                        assert client.get(f"http://127.0.0.1:{port}/").status_code == 200
            counted[label] = counter.count
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    assert socket.socket.connect is original, (
        "the counter left socket.socket.connect wrapped, which would follow the "
        "rest of the process around"
    )
    assert counted["leaky"] >= 40, (
        f"a keep-alive-disabled client opened only {counted['leaky']} connection(s) "
        f"for 40 requests, so S0 cannot see the known-bad case either"
    )
    assert counted["pooled"] <= 4, (
        f"the pooled client opened {counted['pooled']} connection(s) for 40 "
        f"requests; S0 would flag the shipped configuration as leaky"
    )


def test_the_managed_engine_gate_runs_on_all_three_first_class_platforms():
    job = _load(RELEASE_VALIDATION)["jobs"]["managed-qdrant-e2e"]
    labels = set((job.get("strategy") or {}).get("matrix", {}).get("os") or [])
    assert {"ubuntu-latest", "macos-14", "windows-latest"} <= labels, labels


def test_playwright_is_a_declared_dependency():
    """It was in no extra at all, so even setting RAG_E2E_PANEL_URL by hand
    produced an import-skip rather than a run."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "playwright" in text, (
        "playwright is not declared anywhere in pyproject.toml, so the panel "
        "browser gate cannot be installed by any documented command")
