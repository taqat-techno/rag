"""Reclaiming a rebuild's leftovers without deleting somebody's index (WP-R03).

A per-project rebuild builds into ``proj_<uuid>_g<n>``, swaps to it once the
replacement is verified, and drops the superseded collection LAST. Two kinds of
leftover accumulate: the superseded collection when the drop fails, and the
staging collection when the process dies before the swap. Neither is visible to
``rag storage reclaim``, which works from the collections the registry currently
points AT.

Reaping them is the one destructive addition in this release, and the shape it
hunts for — ``proj_`` plus 32 hex — is exactly what another installation on a
shared managed engine produces. This project has already shipped the opposite
mistake once: ``obsolete_collections`` returned ``existing - current`` and the
caller deleted it. So these tests pin the guard rails, not the feature.

**How they are asked.** Every behavioural assertion goes through the service's
own diagnostic endpoints (``GET /api/storage/orphans``, ``POST
/api/storage/reap``) rather than through the new module directly. The app exists
in both trees, so a control run against the pre-change tree fails on the ANSWER
— "the installation has no orphan report: 404" — rather than on an import, which
would prove only that a file is new.
"""

from __future__ import annotations

import json
import sqlite3
import types
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import ragtools.service.app as app_module
from ragtools.config import ProjectConfig, Settings
from ragtools.registry import ProjectRegistry
from ragtools.service.app import create_app

DIMENSION = 384


# --- the smallest installation that can have leftovers ----------------------


class FakeStore:
    """A Qdrant stand-in that answers only what the reaper is allowed to ask.

    ``points=None`` for a collection means the engine will not answer a count —
    the case a reaper must never render as "empty, therefore safe to delete".
    """

    def __init__(self, collections: dict, dimension: int = DIMENSION):
        self._points = dict(collections)
        self._dimensions = {}
        self._dimension = dimension
        self.deleted: list[str] = []
        self.undeletable: set[str] = set()

    def set_dimension(self, name: str, size) -> None:
        self._dimensions[name] = size

    def get_collections(self):
        return types.SimpleNamespace(
            collections=[types.SimpleNamespace(name=n) for n in self._points])

    def get_collection(self, name):
        if name not in self._points:
            raise KeyError(name)
        size = self._dimensions.get(name, self._dimension)
        if size is None:
            raise RuntimeError("the engine did not describe this collection")
        return types.SimpleNamespace(config=types.SimpleNamespace(
            params=types.SimpleNamespace(
                vectors=types.SimpleNamespace(size=size))))

    def count(self, collection_name, exact=True):
        value = self._points[collection_name]
        if value is None:
            raise RuntimeError("the engine did not answer")
        return types.SimpleNamespace(count=int(value))

    def delete_collection(self, name):
        if name in self.undeletable:
            raise RuntimeError("the engine refused to drop it")
        self.deleted.append(name)
        self._points.pop(name, None)


class FakeOwner:
    """Just enough owner: the surfaces every destructive path already consults."""

    def __init__(self, settings, store, registry):
        self.settings = settings
        self.client = store
        self.registry = registry
        self.encoder = types.SimpleNamespace(dimension=DIMENSION)
        self.indexing = False
        self.router = types.SimpleNamespace(
            strategy="per_project",
            all_collections=lambda: [r.collection_name
                                     for r in registry.list(include_archived=True)])

    def storage_reachable(self):
        return True, ""

    def _count_points(self, name):
        """UNKNOWN IS NONE — the real one's whole point."""
        try:
            return int(self.client.count(collection_name=name, exact=True).count)
        except Exception:  # noqa: BLE001
            return None


class Install:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.settings = Settings(
            content_root=str(tmp_path),
            qdrant_path=str(tmp_path / "qdrant"),
            state_db=str(tmp_path / "state.db"),
            collection_strategy="per_project",
            projects=[ProjectConfig(id="alpha", path=str(tmp_path))],
        )
        self.registry = ProjectRegistry(str(tmp_path / "registry.db"))
        self.store = FakeStore({})
        self.owner = FakeOwner(self.settings, self.store, self.registry)

    def project(self, project_id: str, *, generation: int = 0):
        """Register a project and move it to ``generation``, as a rebuild would."""
        record = self.registry.add(project_id, path=str(self.tmp))
        base = record.collection_name
        if generation:
            self.registry.set_active_collection(
                record.uuid, f"{base}_g{generation}", generation=generation)
            record = self.registry.get(project_id)
        self.store._points[record.collection_name] = 100
        return record

    def leftover(self, base: str, generation: int, *, points: int = 42) -> str:
        """A generation collection nothing points at — the thing to reclaim."""
        name = f"{base}_g{generation}"
        self.store._points[name] = points
        return name

    def stranger(self, hexuuid: str, *, generation=None, points: int = 999) -> str:
        """A collection with our shape that our registry knows nothing about."""
        name = f"proj_{hexuuid}"
        if generation is not None:
            name = f"{name}_g{generation}"
        self.store._points[name] = points
        return name

    def close(self):
        self.registry.close()


@pytest.fixture
def install(tmp_path):
    inst = Install(tmp_path)
    try:
        yield inst
    finally:
        inst.close()


@pytest.fixture
def api(install, monkeypatch):
    """The service, wired to this installation. No lifespan: nothing here needs it."""
    monkeypatch.setattr(app_module, "_owner", install.owner, raising=False)
    monkeypatch.setattr(app_module, "_settings", install.settings, raising=False)
    return TestClient(create_app())


def sweep(api, **params) -> dict:
    """What this installation says can be reclaimed, and why the rest cannot.

    Asked through the endpoint an operator would ask, so the control against the
    pre-change tree fails on the answer rather than on an import.
    """
    response = api.get("/api/storage/orphans", params=params)
    assert response.status_code == 200, (
        f"this installation cannot report orphaned generation collections: "
        f"{response.status_code} {response.text[:300]}")
    return response.json()


def apply(api, **params):
    response = api.post("/api/storage/reap", params=params)
    assert response.status_code in (200, 409), (
        f"this installation cannot reap orphaned generation collections: "
        f"{response.status_code} {response.text[:300]}")
    return response


def names(entries) -> set:
    return {e["collection"] for e in entries}


def reasons_for(report: dict, collection: str) -> set:
    for entry in report["excluded"]:
        if entry["collection"] == collection:
            return set(entry["reasons"])
    if collection in names(report["reapable"]):
        return set()
    raise AssertionError(
        f"{collection} was not even considered; the report saw "
        f"{sorted(names(report['reapable']) | names(report['excluded']))}")


# --- 1. the dry run is the shipped behaviour --------------------------------


def test_a_leftover_generation_is_reported_and_nothing_is_deleted(install, api):
    """The default sweep names what it would reclaim and reclaims nothing.

    Automatic deletion is opt-in; the report is what ships.
    """
    record = install.project("alpha", generation=2)
    orphan = install.leftover(record.collection_name.rpartition("_g")[0], 1)

    report = sweep(api, grace_hours=0)

    assert report["dry_run"] is True
    assert orphan in names(report["reapable"])
    assert report["deleted"] == []
    assert install.store.deleted == []
    assert orphan in install.store._points


def test_every_exclusion_is_reported_with_a_named_reason(install, api):
    """A destructive tool that silently drops the things it will not touch is a
    tool nobody can audit."""
    record = install.project("alpha", generation=1)
    base = record.collection_name.rpartition("_g")[0]
    stranger = install.stranger("f" * 32, generation=3)
    mismatched = install.leftover(base, 7)
    install.store.set_dimension(mismatched, 768)

    report = sweep(api, grace_hours=0)

    assert reasons_for(report, stranger) == {"not_owned_by_this_installation"}
    assert reasons_for(report, mismatched) == {"embedding_identity_mismatch"}
    assert reasons_for(report, record.collection_name) == {"active_registry_pointer"}
    assert report["exclusion_counts"]["not_owned_by_this_installation"] >= 1


def test_a_collection_that_cannot_be_counted_is_never_reaped(install, api):
    """UNKNOWN IS NOT ZERO. A count that could not be taken is a reason to keep
    the collection, not a confident report that it holds nothing."""
    record = install.project("alpha", generation=2)
    base = record.collection_name.rpartition("_g")[0]
    orphan = install.leftover(base, 1, points=None)

    report = sweep(api, grace_hours=0)

    assert "points_unknown" in reasons_for(report, orphan)
    entry = next(e for e in report["excluded"] if e["collection"] == orphan)
    assert entry["points"] is None, "an uncountable collection rendered as 0 points"

    apply(api, grace_hours=0)
    assert install.store.deleted == []


# --- 2. idempotency ---------------------------------------------------------


def test_sweeping_twice_says_the_same_thing_and_does_not_reset_the_clock(install, api):
    """A polled report must not be able to postpone its own grace period."""
    record = install.project("alpha", generation=2)
    orphan = install.leftover(record.collection_name.rpartition("_g")[0], 1)

    first = sweep(api)
    second = sweep(api)

    assert "within_grace_period" in reasons_for(first, orphan)
    assert "within_grace_period" in reasons_for(second, orphan)
    seen_first = next(e for e in first["excluded"] if e["collection"] == orphan)
    seen_again = next(e for e in second["excluded"] if e["collection"] == orphan)
    assert seen_first["first_seen"] == seen_again["first_seen"]


def test_reaping_twice_drops_the_collection_once(install, api):
    record = install.project("alpha", generation=2)
    orphan = install.leftover(record.collection_name.rpartition("_g")[0], 1)

    first = apply(api, grace_hours=0).json()
    second = apply(api, grace_hours=0).json()

    assert first["deleted"] == [orphan]
    assert second["deleted"] == []
    assert install.store.deleted == [orphan]


# --- 3. crash recovery ------------------------------------------------------


def test_a_staging_collection_abandoned_by_a_crashed_rebuild_is_recovered(install, api):
    """The rebuild created `_g2` and died before the swap, so the registry still
    says `_g1`. Nothing points at `_g2` and nothing ever will."""
    record = install.project("alpha", generation=1)
    base = record.collection_name.rpartition("_g")[0]
    abandoned = install.leftover(base, 2)

    report = sweep(api, grace_hours=0)
    assert abandoned in names(report["reapable"])

    apply(api, grace_hours=0)
    assert install.store.deleted == [abandoned]
    assert record.collection_name in install.store._points, (
        "it deleted the collection the project is actually serving")


def test_a_crash_between_the_audit_and_the_drop_leaves_the_orphan_reclaimable(
        install, api):
    """The audit records intent; it does not record completion. A process that
    dies after writing it must find the collection again, not decide it is
    already handled."""
    record = install.project("alpha", generation=2)
    base = record.collection_name.rpartition("_g")[0]
    orphan = install.leftover(base, 1)
    install.store.undeletable.add(orphan)

    crashed = apply(api, grace_hours=0).json()
    assert crashed["deleted"] == []
    assert [f["collection"] for f in crashed["failures"]] == [orphan]
    assert any(row["action"] == "reap_intent" and row["collection"] == orphan
               for row in sweep(api, grace_hours=0)["audit"])

    install.store.undeletable.clear()
    recovered = apply(api, grace_hours=0).json()
    assert recovered["deleted"] == [orphan]


# --- 4. the active pointer ---------------------------------------------------


def test_the_collection_a_project_is_serving_is_never_a_candidate(install, api):
    record = install.project("alpha", generation=3)

    report = sweep(api, grace_hours=0)

    assert record.collection_name not in names(report["reapable"])
    assert reasons_for(report, record.collection_name) == {"active_registry_pointer"}

    apply(api, grace_hours=0)
    assert install.store.deleted == []
    assert record.collection_name in install.store._points


def test_another_projects_active_generation_is_not_reclaimed_by_its_neighbour(
        install, api):
    """Two projects, each at a generation. Neither one's live collection is the
    other's leftover."""
    alpha = install.project("alpha", generation=1)
    beta = install.project("beta", generation=2)

    report = sweep(api, grace_hours=0)

    assert report["reapable"] == []
    assert reasons_for(report, alpha.collection_name) == {"active_registry_pointer"}
    assert reasons_for(report, beta.collection_name) == {"active_registry_pointer"}


# --- 5. unresolved rebuild work ---------------------------------------------


def _write_intent(settings, payload: dict) -> None:
    path = Path(settings.data_dir) / "rebuild-intent.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_generation_of_a_project_an_interrupted_rebuild_named_is_left_alone(
        install, api):
    record = install.project("alpha", generation=2)
    base = record.collection_name.rpartition("_g")[0]
    orphan = install.leftover(base, 1)
    _write_intent(install.settings, {"operation": "rebuild", "projects": ["alpha"],
                                     "failed_projects": ["alpha"], "at": 0})

    report = sweep(api, grace_hours=0)

    assert "referenced_by_interrupted_rebuild" in reasons_for(report, orphan)
    apply(api, grace_hours=0)
    assert install.store.deleted == []


def test_an_interrupted_rebuild_that_names_nothing_stops_the_whole_sweep(
        install, api):
    """A marker that says a rebuild is unresolved without saying what it touched
    is a reason to reap nothing, not a reason to guess."""
    record = install.project("alpha", generation=2)
    base = record.collection_name.rpartition("_g")[0]
    orphan = install.leftover(base, 1)
    _write_intent(install.settings, {"operation": "rebuild", "at": 0})

    report = sweep(api, grace_hours=0)

    assert "rebuild_unresolved" in reasons_for(report, orphan)
    assert report["reapable"] == []


def test_an_indexing_run_in_flight_refuses_every_candidate(install, api):
    """The destructive preconditions the rest of the product already owns are
    consulted here too, rather than re-derived."""
    record = install.project("alpha", generation=2)
    base = record.collection_name.rpartition("_g")[0]
    orphan = install.leftover(base, 1)
    install.owner.indexing = True

    report = sweep(api, grace_hours=0)

    assert "operation_refused" in reasons_for(report, orphan)
    assert install.store.deleted == []


# --- 6. registry loss --------------------------------------------------------


def test_an_unvouchable_registry_refuses_the_sweep_entirely(install, api):
    """A lost, replaced or rolled-back registry makes EVERY collection look
    orphaned — which is exactly when reaping destroys the index it was meant to
    protect. So nothing is even inspected."""
    record = install.project("alpha", generation=2)
    base = record.collection_name.rpartition("_g")[0]
    orphan = install.leftover(base, 1)
    install.registry.hold("registry.db was restored from an older copy")

    report = sweep(api, grace_hours=0)

    assert report["allowed"] is False
    assert "registry.db was restored from an older copy" in report["refusal"]
    assert report["reapable"] == []
    assert report["excluded"] == []
    assert orphan in install.store._points

    refused = apply(api, grace_hours=0)
    assert refused.status_code == 409
    assert install.store.deleted == []


def test_the_refusal_lifts_once_the_registry_is_vouched_for_again(install, api):
    record = install.project("alpha", generation=2)
    base = record.collection_name.rpartition("_g")[0]
    orphan = install.leftover(base, 1)
    install.registry.hold("registry.db could not be read")
    assert sweep(api, grace_hours=0)["allowed"] is False

    install.registry.release_hold()

    report = sweep(api, grace_hours=0)
    assert report["allowed"] is True
    assert orphan in names(report["reapable"])


# --- 7. the durable audit ----------------------------------------------------


def test_a_deletion_is_audited_before_it_happens(install, api):
    record = install.project("alpha", generation=2)
    base = record.collection_name.rpartition("_g")[0]
    orphan = install.leftover(base, 1, points=17)

    apply(api, grace_hours=0)

    trail = sweep(api, grace_hours=0)["audit"]
    actions = [row["action"] for row in trail if row["collection"] == orphan]
    assert "reap_intent" in actions and "reaped" in actions
    intent = next(r for r in trail
                  if r["collection"] == orphan and r["action"] == "reap_intent")
    assert intent["project"] == "alpha"
    assert intent["generation"] == 1
    assert intent["points"] == 17


def test_the_audit_survives_the_process_that_wrote_it(install, api):
    """Durable means on disk, in the store the project already chose for state —
    not in a dict that dies with the sweep."""
    record = install.project("alpha", generation=2)
    orphan = install.leftover(record.collection_name.rpartition("_g")[0], 1)

    apply(api, grace_hours=0)

    ledger = Path(install.settings.state_db).with_name("reaper.db")
    assert ledger.is_file(), "there is no durable audit"
    conn = sqlite3.connect(str(ledger))
    try:
        rows = conn.execute(
            "SELECT action FROM reap_audit WHERE collection_name=? ORDER BY id",
            (orphan,)).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows][:1] == ["reap_intent"], (
        "the delete was not audited before it happened")


def test_no_ledger_means_no_deletion(install, api):
    """Requirement 9 read backwards: if the audit cannot be written, nothing is
    dropped. A deletion nobody recorded is indistinguishable from data that
    vanished."""
    record = install.project("alpha", generation=2)
    base = record.collection_name.rpartition("_g")[0]
    orphan = install.leftover(base, 1)
    # A directory where the ledger has to be a file: every open fails.
    Path(install.settings.state_db).with_name("reaper.db").mkdir()

    report = sweep(api, grace_hours=0)

    assert "ledger_unavailable" in reasons_for(report, orphan)
    apply(api, grace_hours=0)
    assert install.store.deleted == []


# --- 8. a healthy installation --------------------------------------------


def test_a_healthy_installation_has_nothing_to_reap(install, api):
    """Three projects, each serving the collection the registry points at. The
    sweep must find nothing and, above all, do nothing."""
    for pid, generation in (("alpha", 0), ("beta", 1), ("gamma", 4)):
        install.project(pid, generation=generation)

    report = sweep(api, grace_hours=0)

    assert report["reapable"] == []
    assert set(report["exclusion_counts"]) == {"active_registry_pointer"}
    apply(api, grace_hours=0)
    assert install.store.deleted == []


def test_a_healthy_installation_records_no_sightings(install, api):
    """The grace clock starts on ORPHANS. A healthy install must not accumulate
    a ledger of its own live collections."""
    install.project("alpha", generation=1)

    sweep(api, grace_hours=0)

    ledger = Path(install.settings.state_db).with_name("reaper.db")
    if not ledger.is_file():
        return
    conn = sqlite3.connect(str(ledger))
    try:
        count = conn.execute("SELECT COUNT(*) FROM reap_sighting").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


# --- 9. the stranger ---------------------------------------------------------


def test_a_strangers_project_collection_is_never_a_candidate(install, api):
    """`proj_` plus 32 hex is a SHAPE. Another installation sharing a managed
    engine produces collections that match it perfectly, and only this
    installation's registry knows which of them are its own."""
    install.project("alpha", generation=1)
    bare = install.stranger("a" * 32)
    generation = install.stranger("b" * 32, generation=1)

    report = sweep(api, grace_hours=0)

    assert bare not in names(report["reapable"])
    assert generation not in names(report["reapable"])
    assert reasons_for(report, generation) == {"not_owned_by_this_installation"}
    assert reasons_for(report, bare) == {"no_generation_metadata"}

    apply(api, grace_hours=0)
    assert install.store.deleted == []
    assert bare in install.store._points and generation in install.store._points


def test_a_stranger_is_not_reaped_even_when_everything_else_is(install, api):
    """The dangerous case: a legitimate reap running beside somebody else's
    index. The sweep must be able to act and still leave them alone."""
    record = install.project("alpha", generation=2)
    mine = install.leftover(record.collection_name.rpartition("_g")[0], 1)
    theirs = install.stranger("c" * 32, generation=1)

    result = apply(api, grace_hours=0).json()

    assert result["deleted"] == [mine]
    assert theirs in install.store._points


def test_a_uuid_we_own_pointing_somewhere_else_is_ambiguous_not_reapable(install, api):
    """A registry row that claims the UUID but serves a different base cannot
    say which project this generation belongs to. Ambiguity is not permission."""
    record = install.project("alpha", generation=0)
    base = record.collection_name
    install.registry.set_active_collection(record.uuid, "proj_" + "d" * 32,
                                           generation=5)
    orphan = install.leftover(base, 1)

    report = sweep(api, grace_hours=0)

    assert orphan not in names(report["reapable"])
    assert reasons_for(report, orphan) == {"ambiguous_project_identity"}


# --- the grace period --------------------------------------------------------


def test_a_freshly_seen_orphan_waits_out_its_grace_period(install, api):
    record = install.project("alpha", generation=2)
    orphan = install.leftover(record.collection_name.rpartition("_g")[0], 1)

    guarded = sweep(api, grace_hours=24)
    assert "within_grace_period" in reasons_for(guarded, orphan)

    settled = sweep(api, grace_hours=0)
    assert orphan in names(settled["reapable"])


def test_nothing_is_deleted_while_the_grace_period_is_running(install, api):
    record = install.project("alpha", generation=2)
    install.leftover(record.collection_name.rpartition("_g")[0], 1)

    result = apply(api, grace_hours=24).json()

    assert result["deleted"] == []
    assert install.store.deleted == []


# --- the switch, wired and off ----------------------------------------------


def reap_task(owner):
    """The unattended sweep from the product's own maintenance table."""
    from ragtools.service.maintenance import build_default_tasks

    tasks = build_default_tasks(owner)
    for task in tasks:
        if task.name == "generation-reap":
            return task
    raise AssertionError(
        f"this installation has no periodic orphan sweep, so the grace clock "
        f"never starts and the switch has nothing to switch on: "
        f"{[t.name for t in tasks]}")


def test_the_maintenance_table_sweeps_for_orphaned_generations(install):
    """The periodic sweep exists — otherwise the grace clock never starts on an
    unattended machine and the switch has nothing to switch on."""
    assert reap_task(install.owner).lock == "index", (
        "an unattended reap must skip while an index holds the mutex")


def test_the_periodic_sweep_deletes_nothing_by_default(install):
    """Wired, and off. `reap_generations` defaults to False, so the unattended
    sweep records sightings and reports; it does not reclaim."""
    record = install.project("alpha", generation=2)
    install.leftover(record.collection_name.rpartition("_g")[0], 1)

    assert getattr(install.settings, "reap_generations", None) is False, (
        "unattended deletion is not off by default")
    reap_task(install.owner).action()

    assert install.store.deleted == []


def test_the_periodic_sweep_reclaims_once_it_is_switched_on(install):
    """The switch is real, not decorative — and turning it on relaxes no check."""
    record = install.project("alpha", generation=2)
    orphan = install.leftover(record.collection_name.rpartition("_g")[0], 1)
    stranger = install.stranger("e" * 32, generation=1)

    object.__setattr__(install.settings, "reap_generations", True)
    object.__setattr__(install.settings, "reap_grace_hours", 0.0)
    reap_task(install.owner).action()

    assert install.store.deleted == [orphan]
    assert stranger in install.store._points


# --- the CLI -----------------------------------------------------------------


def test_the_cli_offers_a_reap_command():
    """`rag storage reap` is how an operator asks offline, next to `reclaim`."""
    from typer.testing import CliRunner

    from ragtools.cli import app

    result = CliRunner().invoke(app, ["storage", "reap", "--help"])
    assert result.exit_code == 0, result.output
    assert "--apply" in result.output
