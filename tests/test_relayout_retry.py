"""A rebuild that cannot write must not keep embedding.

Observed on the affected machine: an entire project re-embedded, a write to an
unreachable Qdrant, a failure, and around again — at significant CPU cost, with
search unavailable throughout, converging on nothing.

Three properties made it unbounded, and all three are asserted here:

* ``units_to_do`` returned everything that was not ``done``, which includes
  ``failed`` — and ``run_pending`` is called on EVERY service start, with Task
  Scheduler restarting the service on failure.
* Each attempt is a FULL re-index: the whole tree re-scanned, every file
  re-chunked and re-embedded from zero, regardless of how much had succeeded.
* Nothing asked whether storage was reachable before paying for any of it.

And one that made it undiagnosable: an unreachable engine and a corrupt file
were both recorded as ``failed`` with a stringified exception, so no retry
policy could tell "try again when storage returns" from "this will never work".
"""

from __future__ import annotations

import types

import pytest

from ragtools.upgrade import relayout
from ragtools.upgrade.relayout import Inventory, Unit


@pytest.fixture
def settings(tmp_path):
    return types.SimpleNamespace(state_db=str(tmp_path / "state.db"))


@pytest.fixture
def plan(settings):
    return relayout.begin(
        settings,
        Inventory(units=[Unit(relayout.KIND_PROJECT, "alpha", 500),
                         Unit(relayout.KIND_PROJECT, "beta", 300)]),
        from_backend="embedded", to_backend="managed",
        from_strategy="shared", to_strategy="per_project")


class Owner:
    """An owner whose storage reachability and per-unit outcome are scriptable."""

    def __init__(self, *, reachable=True, fail_units=()):
        self.reachable = reachable
        self.fail_units = set(fail_units)
        self.indexed = []
        self.router = types.SimpleNamespace(
            all_collections=lambda: [], collection_for=lambda p: p)
        self.settings = types.SimpleNamespace(collection_name="markdown_kb")
        self._frameworks = None
        outer = self

        class Client:
            def get_collections(self):
                if not outer.reachable:
                    raise ConnectionError("connection refused")
                return types.SimpleNamespace(collections=[])

            def delete_collection(self, name):
                pass

        self._client = Client()

    def run_full_index(self, project_id=None):
        # The expensive part. Recording it is how we prove it did not happen.
        self.indexed.append(project_id)
        if not self.reachable:
            raise ConnectionError("connection refused")
        if project_id in self.fail_units:
            raise ValueError("a genuinely broken project")

    def _count_points(self, name):
        return 100

    def sync_frameworks(self, refresh=False):
        pass


# --- the preflight ----------------------------------------------------------


def test_unreachable_storage_costs_no_embedding(settings, plan):
    """The core fix. Nothing is indexed, because nothing could have been stored."""
    owner = Owner(reachable=False)

    report = relayout.run_pending(owner, settings, plan_id=plan)

    assert owner.indexed == [], (
        f"it re-indexed {owner.indexed} against an unreachable engine")
    assert report.blocked == 2
    assert "refused" in report.blocked_reason


def test_a_blocked_plan_says_so(settings, plan):
    relayout.run_pending(Owner(reachable=False), settings, plan_id=plan)

    report = relayout.progress(settings, plan)

    assert "BLOCKED" in report.describe()
    assert report.stalled is True


def test_restarting_against_a_dead_engine_stays_free(settings, plan):
    """The CPU loop, asserted. Ten service starts used to be ten full rebuilds."""
    owner = Owner(reachable=False)

    for _ in range(10):
        relayout.run_pending(owner, settings, plan_id=plan)

    assert owner.indexed == [], f"ten restarts embedded {len(owner.indexed)} times"


def test_work_resumes_once_storage_returns(settings, plan):
    owner = Owner(reachable=False)
    relayout.run_pending(owner, settings, plan_id=plan)
    assert relayout.progress(settings, plan).blocked == 2

    owner.reachable = True
    report = relayout.run_pending(owner, settings, plan_id=plan)

    assert sorted(owner.indexed) == ["alpha", "beta"]
    assert report.done == 2


def test_storage_dying_mid_run_parks_the_rest(settings, plan):
    """One outage must not consume every project's retry budget."""
    owner = Owner(reachable=True)
    real = owner.run_full_index

    def die_after_first(project_id=None):
        real(project_id=project_id)
        owner.reachable = False

    owner.run_full_index = die_after_first

    relayout.run_pending(owner, settings, plan_id=plan)
    report = relayout.progress(settings, plan)

    assert report.blocked >= 1
    assert report.failed == 0, "an outage was charged to the units as failures"


# --- bounded retry ----------------------------------------------------------


def test_a_genuinely_broken_unit_stops_being_retried(settings, plan, monkeypatch):
    """The cap, isolated from the backoff by removing the waiting."""
    monkeypatch.setattr(relayout, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    owner = Owner(fail_units={"alpha"})

    for _ in range(10):
        relayout.run_pending(owner, settings, plan_id=plan)

    attempts = owner.indexed.count("alpha")
    assert attempts == relayout.MAX_ATTEMPTS, (
        f"alpha was rebuilt {attempts} times; the cap is {relayout.MAX_ATTEMPTS}")


def test_a_healthy_unit_is_never_repeated(settings, plan):
    """Completed work is what makes an eight-hour migration survivable."""
    owner = Owner(fail_units={"alpha"})

    for _ in range(5):
        relayout.run_pending(owner, settings, plan_id=plan)

    assert owner.indexed.count("beta") == 1


def test_backoff_defers_the_next_attempt(settings, plan):
    """The first retry is immediate; the one after it waits.

    So a service that restarts in a tight loop cannot spin: attempt 2 sets a
    30-second window, and every start inside it costs nothing.
    """
    owner = Owner(fail_units={"alpha", "beta"})

    relayout.run_pending(owner, settings, plan_id=plan)   # attempt 1
    relayout.run_pending(owner, settings, plan_id=plan)   # attempt 2, immediate
    settled = len(owner.indexed)
    for _ in range(5):
        relayout.run_pending(owner, settings, plan_id=plan)

    assert len(owner.indexed) == settled, (
        "five restarts inside the backoff window each paid for a full rebuild")


def test_exhausted_units_are_named_not_silently_dropped(settings, plan, monkeypatch):
    monkeypatch.setattr(relayout, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    owner = Owner(fail_units={"alpha"})
    for _ in range(6):
        relayout.run_pending(owner, settings, plan_id=plan)

    named = relayout.exhausted_units(settings, plan)

    assert [(k, u) for k, u, _ in named] == [(relayout.KIND_PROJECT, "alpha")]


def test_an_operator_resume_restores_the_budget(settings, plan, monkeypatch):
    """Automatic retries are bounded so a machine cannot loop. A person who has
    fixed the cause is a different thing, and only they know it was fixed."""
    monkeypatch.setattr(relayout, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    owner = Owner(fail_units={"alpha"})
    for _ in range(6):
        relayout.run_pending(owner, settings, plan_id=plan)
    assert owner.indexed.count("alpha") == relayout.MAX_ATTEMPTS

    owner.fail_units.clear()
    report = relayout.run_pending(owner, settings, plan_id=plan, reset=True)

    assert owner.indexed.count("alpha") == relayout.MAX_ATTEMPTS + 1
    assert report.complete


def test_blocking_does_not_consume_attempts(settings, plan):
    """An engine that was down is not the project's fault."""
    owner = Owner(reachable=False)
    for _ in range(5):
        relayout.run_pending(owner, settings, plan_id=plan)

    owner.reachable = True
    report = relayout.run_pending(owner, settings, plan_id=plan)

    assert report.done == 2, (
        "units were exhausted by an outage they had no part in")


# --- readiness --------------------------------------------------------------


def test_a_blocked_plan_still_refuses_to_serve_searches(settings, plan):
    relayout.run_pending(Owner(reachable=False), settings, plan_id=plan)

    with pytest.raises(relayout.MigrationInProgress):
        relayout.guard_ready(settings)


def test_a_finished_plan_serves_normally(settings, plan):
    relayout.run_pending(Owner(), settings, plan_id=plan)

    relayout.guard_ready(settings)  # does not raise


# --- the schema upgrade -----------------------------------------------------


def test_a_plan_store_from_the_previous_release_gains_the_columns(settings):
    """The machine this ships for is mid-migration right now, so its
    `relayout_unit` table already exists — and CREATE TABLE IF NOT EXISTS does
    not add columns."""
    import sqlite3

    path = relayout._db_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(str(path))
    old.executescript("""
        CREATE TABLE relayout_plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
            from_backend TEXT NOT NULL, to_backend TEXT NOT NULL,
            from_strategy TEXT NOT NULL, to_strategy TEXT NOT NULL,
            status TEXT NOT NULL, finished_at REAL);
        CREATE TABLE relayout_unit (
            plan_id INTEGER NOT NULL, kind TEXT NOT NULL, unit_id TEXT NOT NULL,
            status TEXT NOT NULL, points_before INTEGER NOT NULL DEFAULT 0,
            points_after INTEGER NOT NULL DEFAULT 0, error TEXT,
            updated_at REAL NOT NULL, PRIMARY KEY (plan_id, kind, unit_id));
    """)
    old.close()

    plan = relayout.begin(settings, Inventory(units=[Unit("project", "a", 1)]),
                          from_backend="embedded", to_backend="managed",
                          from_strategy="shared", to_strategy="per_project")

    assert relayout.units_to_do(settings, plan)  # the query needs both columns
