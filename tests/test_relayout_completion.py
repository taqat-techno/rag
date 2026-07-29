"""A rebuilt index that the product refuses to search.

Found live on an installed v3.1.0 machine, not in CI. The layout migration had
run to completion — 15 collections, 147,105 points, every project present and
correct on the managed engine — and every search returned
`migration/reindex in progress`. Permanently.

`_points_for_project` called ``owner.router.collection_for(project_id)``.
`CollectionRouter` has no such method; it is ``write_collection``. So every call
raised ``AttributeError``, a bare ``except Exception`` swallowed it, and every
unit recorded ``points_after = 0``. `validate` then read that as "held 41,832
points before the migration and none after" for project after project, refused,
`finalize` never ran, the plan stayed ``running``, and `guard_ready` raised on
every query for the rest of the machine's life.

Three faults, and the test suite has to pin all three:

1. the method name was wrong, and nothing called it in a test with a real router;
2. returning 0 for "I could not count" made a programming error indistinguishable
   from total data loss;
3. one gate answered two questions — "is the rebuild finished?" (decides whether
   search works) and "is it verified?" (decides whether the OLD index may be
   deleted). The conservative answer to the destructive question took the
   product down with it.
"""

from __future__ import annotations

import types

import pytest

from ragtools.collection_router import CollectionRouter
from ragtools.upgrade import relayout
from ragtools.upgrade.relayout import Inventory, Unit


@pytest.fixture
def settings(tmp_path):
    return types.SimpleNamespace(state_db=str(tmp_path / "state.db"),
                                 collection_name="markdown_kb")


@pytest.fixture
def plan(settings):
    return relayout.begin(
        settings,
        Inventory(units=[Unit(relayout.KIND_PROJECT, "alpha", 1821),
                         Unit(relayout.KIND_PROJECT, "beta", 41832)]),
        from_backend="embedded", to_backend="managed",
        from_strategy="shared", to_strategy="per_project")


class Owner:
    """An owner whose router is the REAL class, not a stub.

    That is the point. A stub with a `collection_for` method would have made the
    broken call succeed and the bug invisible — which is exactly how it shipped.
    """

    def __init__(self, *, counts=None, count_raises=False):
        self.counts = counts or {}
        self.count_raises = count_raises
        self.settings = types.SimpleNamespace(collection_name="markdown_kb")
        self._frameworks = None
        self.router = types.SimpleNamespace(
            all_collections=lambda: ["proj_alpha", "proj_beta"],
            write_collection=lambda pid: f"proj_{pid}",
        )
        outer = self

        class Client:
            def get_collections(self):
                return types.SimpleNamespace(collections=[])

            def delete_collection(self, name):
                outer.deleted.append(name)

        self.deleted = []
        self._client = Client()

    def run_full_index(self, project_id=None):
        pass

    def _count_points(self, collection):
        if self.count_raises:
            raise ConnectionError("engine went away")
        return self.counts.get(collection, 0)

    def sync_frameworks(self, refresh=False):
        pass


# --- the missing method -----------------------------------------------------


def test_the_router_has_no_collection_for_method():
    """The literal defect. Named so the next person does not re-add the call."""
    assert not hasattr(CollectionRouter, "collection_for"), (
        "if `collection_for` now exists, `_points_for_project` may use it — but "
        "it did not when this was written, and calling it returned 0 forever"
    )
    assert hasattr(CollectionRouter, "write_collection")


def test_points_are_counted_from_the_collection_the_project_writes_to():
    owner = Owner(counts={"proj_alpha": 1821})

    assert relayout._points_for_project(owner, "alpha") == 1821


def test_a_count_that_fails_is_unknown_not_zero():
    """Returning 0 made a programming error look like total data loss."""
    owner = Owner(count_raises=True)

    assert relayout._points_for_project(owner, "alpha") == relayout.POINTS_UNKNOWN
    assert relayout.POINTS_UNKNOWN != 0


def test_a_zero_from_a_swallowed_failure_is_not_believed():
    """`_count_points` catches its own errors and returns 0 — correct for the
    status display it was written for, and fatal here. A 0 meaning "could not
    ask" and a 0 meaning "empty" lead to opposite decisions, and the wrong one
    disables search on a correctly rebuilt index.

    Without corroboration this defeats the whole fix: `POINTS_UNKNOWN` would
    never fire, because the layer below never raises.
    """
    owner = Owner(counts={})                       # every count comes back 0
    owner._client.get_collections = lambda: types.SimpleNamespace(collections=[])

    assert relayout._points_for_project(owner, "alpha") == relayout.POINTS_UNKNOWN


def test_a_zero_from_a_collection_that_really_exists_is_believed():
    """The converse. A genuinely empty rebuild must still be reported as empty,
    or `validate` stops catching the thing it exists for."""
    owner = Owner(counts={})
    owner._client.get_collections = lambda: types.SimpleNamespace(
        collections=[types.SimpleNamespace(name="proj_alpha")])

    assert relayout._points_for_project(owner, "alpha") == 0


# --- the outage -------------------------------------------------------------


def test_a_completed_rebuild_finalizes_and_search_works_again(settings, plan):
    """The end-to-end symptom. This is what was broken on the real machine."""
    owner = Owner(counts={"proj_alpha": 1821, "proj_beta": 41832})

    report = relayout.run_pending(owner, settings, plan_id=plan)

    assert report.complete, f"the plan never finished: {report.describe()}"
    relayout.guard_ready(settings)          # must not raise
    assert relayout.active_plan(settings) is None


def test_an_uncountable_rebuild_still_lets_searches_run(settings, plan):
    """The fix for the fault that actually bit: a diagnostic failure must not
    disable the product."""
    owner = Owner(count_raises=True)

    report = relayout.run_pending(owner, settings, plan_id=plan)

    assert report.complete, (
        "a failed point COUNT held the plan open, so every search refuses on an "
        "index that was rebuilt correctly")
    relayout.guard_ready(settings)          # must not raise


def test_an_uncountable_rebuild_KEEPS_the_previous_index(settings, plan):
    """The other half of the split. Search resumes; nothing is deleted."""
    owner = Owner(count_raises=True)
    owner.router.all_collections = lambda: ["proj_alpha", "proj_beta"]
    owner._client.get_collections = lambda: types.SimpleNamespace(
        collections=[types.SimpleNamespace(name="markdown_kb"),
                     types.SimpleNamespace(name="proj_alpha")])

    relayout.run_pending(owner, settings, plan_id=plan)

    assert owner.deleted == [], (
        "the old index was retired on the strength of counts we could not take")


def test_a_genuinely_empty_rebuild_is_still_a_problem(settings, plan):
    """The check must keep catching the thing it was written for.

    "Genuinely empty" means the collections EXIST and hold nothing — otherwise
    this is the unverifiable case, which is a different finding with a different
    message.
    """
    owner = Owner(counts={})          # counted, and there really is nothing
    owner._client.get_collections = lambda: types.SimpleNamespace(
        collections=[types.SimpleNamespace(name="proj_alpha"),
                     types.SimpleNamespace(name="proj_beta")])

    relayout.run_pending(owner, settings, plan_id=plan)
    verified, problems = relayout.validate(owner, settings, plan)

    assert not verified
    assert any("none after" in p for p in problems), problems


def test_unfinished_units_still_hold_the_plan_open(settings, plan):
    """Separating the gates must not let an INCOMPLETE rebuild look finished."""
    relayout.mark(settings, plan, Unit(relayout.KIND_PROJECT, "alpha", 1821),
                  relayout.STATUS_BLOCKED, error="storage unreachable")

    assert relayout.units_all_done(settings, plan) is False


def test_all_done_means_all_done(settings, plan):
    for unit_id in ("alpha", "beta"):
        relayout.mark(settings, plan, Unit(relayout.KIND_PROJECT, unit_id, 1),
                      relayout.STATUS_DONE, points_after=5)

    assert relayout.units_all_done(settings, plan) is True
