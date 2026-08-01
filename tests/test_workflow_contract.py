"""R12 — a leg that cannot fail the release must not be able to delay it.

Three facts, established twice on real runs, that a workflow file can express
but cannot enforce on itself:

1. **A retired runner label does not fail — it queues.** `suite (macos-13)` was
   requested on runs ``30653097391`` and ``30666102565``. Neither was ever
   allocated a runner: ``runner_name`` stayed empty for 15+ hours while
   ``macos-14`` legs in the same matrix started in under ten seconds. GitHub
   retired the ``macos-13`` image; the current Intel labels are
   ``macos-15-intel`` and ``macos-26-intel``.

2. **``timeout-minutes`` bounds execution, not queueing.** Both jobs carried a
   30-minute timeout and both sat for 15+ hours. A timeout starts when the job
   starts. Nothing available in a workflow file bounds the wait before that.

3. **Therefore ``needs`` is the real hazard.** ``gate`` listed
   ``suite-macos-intel`` in ``needs`` purely to read its verdict output. That
   single expression let a best-effort leg — one explicitly documented as unable
   to fail the release — hold the release gate open indefinitely. GitHub seals a
   run's job logs until the run completes, so it also sealed the diagnosis of a
   genuinely failed blocking leg (``packaged v3.3.0 -> this build``) until the
   run was cancelled by hand.

The correction is structural, and this module is what keeps it: a job that
cannot fail the release must not appear in the gate's ``needs``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# Labels GitHub actually allocates. An entry here is a claim that a runner will
# be handed to the job; the two incidents above are what an unchecked claim
# costs. Intel macOS is `macos-15-intel` / `macos-26-intel` — `macos-13` and
# `macos-12` are retired and must never reappear.
SUPPORTED_RUNNER_LABELS = {
    "ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04",
    "windows-latest", "windows-2025", "windows-2022",
    "macos-latest", "macos-14", "macos-15", "macos-26",
    "macos-15-intel", "macos-26-intel",
}

RETIRED_RUNNER_LABELS = {"macos-13", "macos-12", "macos-11", "ubuntu-20.04", "windows-2019"}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _workflow_files() -> list[Path]:
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert files, f"no workflow files found under {WORKFLOWS}"
    return files


def _jobs(doc: dict) -> dict:
    return doc.get("jobs") or {}


def _runs_on_labels(job: dict) -> list[str]:
    """Every literal label a job could run on.

    A matrix expression (``${{ matrix.os }}``) is resolved against the matrix
    when one is present, because that is where the retired label actually hid:
    not in `runs-on`, but in the list `runs-on` indexes into.
    """
    runs_on = job.get("runs-on")
    if runs_on is None:
        return []
    if isinstance(runs_on, list):
        return [str(x) for x in runs_on]
    runs_on = str(runs_on)
    if "${{" not in runs_on:
        return [runs_on]

    labels: list[str] = []
    matrix = ((job.get("strategy") or {}).get("matrix")) or {}
    for key, values in matrix.items():
        if key in {"include", "exclude"} or not isinstance(values, list):
            continue
        if f"matrix.{key}" in runs_on:
            labels.extend(str(v) for v in values)
    for entry in (matrix.get("include") or []):
        if isinstance(entry, dict):
            for key, value in entry.items():
                if f"matrix.{key}" in runs_on:
                    labels.append(str(value))
    return labels


# --- 1. every job is bounded in the one dimension a workflow CAN bound --------


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_every_job_has_a_finite_timeout(path: Path):
    missing = [name for name, job in _jobs(_load(path)).items()
               if isinstance(job, dict) and "uses" not in job
               and job.get("timeout-minutes") is None]
    assert not missing, (
        f"{path.name}: jobs without `timeout-minutes`: {missing}. An unbounded "
        "job can hold a run open, and a run holds every job's logs with it."
    )


# --- 2. the label must be one that is actually allocated ---------------------


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_no_job_requests_a_retired_runner_label(path: Path):
    offenders = {
        name: [x for x in _runs_on_labels(job) if x in RETIRED_RUNNER_LABELS]
        for name, job in _jobs(_load(path)).items()
        if isinstance(job, dict)
    }
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, (
        f"{path.name}: retired runner label(s) requested: {offenders}. A retired "
        "label does not fail the job — it queues until GitHub's 24h ceiling, "
        "and `timeout-minutes` cannot help because the job never starts."
    )


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_every_runner_label_is_known_supported(path: Path):
    unknown = {
        name: [x for x in _runs_on_labels(job) if x not in SUPPORTED_RUNNER_LABELS]
        for name, job in _jobs(_load(path)).items()
        if isinstance(job, dict)
    }
    unknown = {k: v for k, v in unknown.items() if v}
    assert not unknown, (
        f"{path.name}: unrecognised runner label(s): {unknown}. Add the label to "
        "SUPPORTED_RUNNER_LABELS only after confirming GitHub still allocates it."
    )


# --- 3. the structural rule: best-effort must not gate the gate --------------


def test_the_release_gate_does_not_wait_on_any_best_effort_job():
    """The rule the incident bought.

    `continue-on-error: true` is the workflow's own statement that a job cannot
    fail the release. Listing such a job in `gate.needs` contradicts it: the
    gate then waits on something whose result it has already agreed to ignore,
    including through an unbounded queue.
    """
    doc = _load(WORKFLOWS / "release-validation.yml")
    jobs = _jobs(doc)
    gate = jobs.get("gate")
    assert gate is not None, "release-validation.yml has no `gate` job"

    needs = gate.get("needs") or []
    if isinstance(needs, str):
        needs = [needs]

    best_effort = [n for n in needs
                   if isinstance(jobs.get(n), dict)
                   and jobs[n].get("continue-on-error") is True]

    assert not best_effort, (
        "the release gate waits on best-effort job(s) "
        f"{best_effort}. A job that cannot fail the release must not be able to "
        "delay it: `needs` waits on QUEUE time too, and nothing bounds that."
    )


def test_the_intel_leg_is_still_present_and_still_reports_a_verdict():
    """Not waited on is not the same as deleted.

    The cheap way to satisfy the rule above would be to remove the Intel leg
    entirely, which would silently drop a platform from validation. It must
    still run and still record pass / fail / unsupported.
    """
    jobs = _jobs(_load(WORKFLOWS / "release-validation.yml"))
    intel = jobs.get("suite-macos-intel")
    assert intel is not None, "the best-effort macOS Intel leg was removed, not un-gated"
    assert intel.get("continue-on-error") is True
    assert intel.get("timeout-minutes") is not None

    verdicts = [s for s in intel.get("steps", [])
                if isinstance(s, dict) and s.get("id") == "verdict"]
    assert verdicts, "the Intel leg records no verdict step"
    body = verdicts[0].get("run", "")
    for outcome in ("pass", "fail", "unsupported"):
        assert outcome in body, (
            f"the Intel leg cannot report '{outcome}'; the approved platform "
            "policy requires all three outcomes to be expressible"
        )
