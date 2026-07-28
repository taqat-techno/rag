"""A destructive layout migration that survives being interrupted.

Rebuilding a real corpus under a new layout is hours of work, and the machine
will be restarted in the middle of it. The properties that make that survivable
are the ones asserted here — each corresponds to a way the naive version loses
data or lies about being finished.
"""

from __future__ import annotations

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.upgrade import relayout
from ragtools.upgrade.relayout import (
    KIND_FRAMEWORK,
    KIND_PROJECT,
    STATUS_DONE,
    STATUS_FAILED,
    Inventory,
    MigrationInProgress,
    Unit,
)


@pytest.fixture
def settings(tmp_path):
    return Settings(qdrant_path=str(tmp_path / "qdrant"),
                    state_db=str(tmp_path / "state.db"),
                    projects=[ProjectConfig(id="alpha", path=str(tmp_path))])


def plan_with(settings, units) -> int:
    return relayout.begin(
        settings, Inventory(units=list(units), total_points=999),
        from_backend="embedded", to_backend="managed",
        from_strategy="shared", to_strategy="per_project")


# --- the ordering that cannot be got wrong --------------------------------


def test_the_inventory_is_recorded_before_anything_is_rebuilt(settings):
    """What must be rebuilt is a fact about the OLD index, and the migration
    destroys the old index. Capture it afterwards and the answer is "nothing" —
    the transition would declare itself complete having rebuilt none of it."""
    units = [Unit(KIND_PROJECT, "alpha", 500), Unit(KIND_PROJECT, "beta", 300)]
    plan = plan_with(settings, units)

    report = relayout.progress(settings, plan)
    assert report is not None
    assert report.total == 2
    assert report.pending == 2 and report.done == 0
    assert [u.unit_id for u in relayout.units_to_do(settings, plan)] == ["alpha", "beta"]


def test_frameworks_are_units_in_their_own_right(settings):
    """A project that links a framework corpus is not responsible for rebuilding
    it, so the corpus needs its own row or it is rebuilt once per linking
    project — or not at all."""
    plan = plan_with(settings, [Unit(KIND_PROJECT, "alpha", 10),
                                Unit(KIND_FRAMEWORK, "fw_odoo_abc", 900)])

    kinds = {u.kind for u in relayout.units_to_do(settings, plan)}
    assert kinds == {KIND_PROJECT, KIND_FRAMEWORK}


# --- resume, which is the whole point -------------------------------------


def test_completed_work_is_never_repeated_after_a_restart(settings):
    """An eight-hour migration that starts over on every reboot never finishes."""
    plan = plan_with(settings, [Unit(KIND_PROJECT, "alpha", 100),
                                Unit(KIND_PROJECT, "beta", 100),
                                Unit(KIND_PROJECT, "gamma", 100)])

    relayout.mark(settings, plan, Unit(KIND_PROJECT, "alpha"), STATUS_DONE,
                  points_after=110)

    remaining = [u.unit_id for u in relayout.units_to_do(settings, plan)]
    assert remaining == ["beta", "gamma"], "finished work was queued again"


def test_a_failed_unit_is_retried_rather_than_skipped(settings):
    """Failure must not read as completion — that is how a project silently
    disappears from a machine that reports success."""
    plan = plan_with(settings, [Unit(KIND_PROJECT, "alpha", 100),
                                Unit(KIND_PROJECT, "beta", 100)])

    relayout.mark(settings, plan, Unit(KIND_PROJECT, "alpha"), STATUS_DONE,
                  points_after=110)
    relayout.mark(settings, plan, Unit(KIND_PROJECT, "beta"), STATUS_FAILED,
                  error="disk full")

    remaining = [u.unit_id for u in relayout.units_to_do(settings, plan)]
    assert remaining == ["beta"]

    report = relayout.progress(settings, plan)
    assert report.failed == 1
    assert report.failures == [(KIND_PROJECT, "beta", "disk full")]


def test_the_plan_survives_a_process_restart(settings):
    """Nothing is held in memory: a new process reads the same state."""
    plan = plan_with(settings, [Unit(KIND_PROJECT, "alpha", 100),
                                Unit(KIND_PROJECT, "beta", 100)])
    relayout.mark(settings, plan, Unit(KIND_PROJECT, "alpha"), STATUS_DONE,
                  points_after=110)

    # A fresh Settings object over the same data directory is what a restart is.
    reopened = Settings(qdrant_path=settings.qdrant_path,
                        state_db=settings.state_db)

    assert relayout.active_plan(reopened) == plan
    assert [u.unit_id for u in relayout.units_to_do(reopened, plan)] == ["beta"]


# --- "incomplete" must never read as "ready" ------------------------------


def test_an_unfinished_migration_refuses_to_answer_searches(settings):
    """An empty result from a half-built index is indistinguishable from "your
    query matched nothing" — the ordinary, reassuring answer — at the exact
    moment the content genuinely is not there yet."""
    plan = plan_with(settings, [Unit(KIND_PROJECT, "alpha", 100)])

    with pytest.raises(MigrationInProgress) as excinfo:
        relayout.guard_ready(settings)

    assert "in progress" in str(excinfo.value)
    assert excinfo.value.report.plan_id == plan


def test_a_finished_migration_answers_normally(settings):
    plan = plan_with(settings, [Unit(KIND_PROJECT, "alpha", 100)])
    relayout.mark(settings, plan, Unit(KIND_PROJECT, "alpha"), STATUS_DONE,
                  points_after=110)
    relayout.finalize(settings, plan)

    relayout.guard_ready(settings)          # must not raise
    assert relayout.active_plan(settings) is None


def test_no_migration_at_all_is_not_a_refusal(settings):
    """The guard must be invisible on a machine that never migrated."""
    relayout.guard_ready(settings)


# --- completion is validated, not assumed ---------------------------------


def test_validation_refuses_while_units_remain(settings):
    plan = plan_with(settings, [Unit(KIND_PROJECT, "alpha", 100),
                                Unit(KIND_PROJECT, "beta", 100)])
    relayout.mark(settings, plan, Unit(KIND_PROJECT, "alpha"), STATUS_DONE,
                  points_after=110)

    ok, problems = relayout.validate(None, settings, plan)

    assert not ok
    assert any("not attempted" in p for p in problems)


def test_a_unit_that_lost_all_its_content_fails_validation(settings):
    """"The indexer returned success" is weaker than "the collection holds
    points", and this is the last moment before the old storage is deleted."""
    plan = plan_with(settings, [Unit(KIND_PROJECT, "alpha", 500)])
    relayout.mark(settings, plan, Unit(KIND_PROJECT, "alpha"), STATUS_DONE,
                  points_after=0)

    ok, problems = relayout.validate(None, settings, plan)

    assert not ok
    assert any("none after" in p for p in problems)


def test_validation_passes_when_every_unit_was_rebuilt(settings):
    plan = plan_with(settings, [Unit(KIND_PROJECT, "alpha", 500),
                                Unit(KIND_FRAMEWORK, "fw_x", 0)])
    relayout.mark(settings, plan, Unit(KIND_PROJECT, "alpha"), STATUS_DONE,
                  points_after=512)
    relayout.mark(settings, plan, Unit(KIND_FRAMEWORK, "fw_x"), STATUS_DONE,
                  points_after=0)

    ok, problems = relayout.validate(None, settings, plan)

    assert ok, problems


def test_a_plan_is_only_complete_once_finalized(settings):
    plan = plan_with(settings, [Unit(KIND_PROJECT, "alpha", 1)])
    relayout.mark(settings, plan, Unit(KIND_PROJECT, "alpha"), STATUS_DONE,
                  points_after=1)

    assert relayout.progress(settings, plan).in_progress
    assert relayout.active_plan(settings) == plan

    relayout.finalize(settings, plan)

    assert relayout.progress(settings, plan).complete
    assert relayout.active_plan(settings) is None


# --- what the user is told ------------------------------------------------


def test_progress_names_the_failures_and_a_retry_path(settings):
    plan = plan_with(settings, [Unit(KIND_PROJECT, "alpha", 1),
                                Unit(KIND_PROJECT, "beta", 1)])
    relayout.mark(settings, plan, Unit(KIND_PROJECT, "alpha"), STATUS_DONE,
                  points_after=1)
    relayout.mark(settings, plan, Unit(KIND_PROJECT, "beta"), STATUS_FAILED,
                  error="permission denied on C:/projects/beta")

    report = relayout.progress(settings, plan)

    assert "in progress" in report.describe()
    assert report.failures[0][2].startswith("permission denied")
