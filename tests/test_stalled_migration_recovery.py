"""Recovery from the exact state v3.2.0 left an installed machine in.

The fixture is not invented. It is LAKOSHA-TAQAT on 2026-07-29, reconstructed
from `/health` and the on-disk artifacts:

* config already at schema v3, `managed` + `per_project`;
* one relayout plan, `running`, 25 units — **1 done, 24 blocked**;
* `qdrant-owner.json` still asserting a claim on a pid that no longer exists;
* the pre-migration embedded store and `index_state.db` intact;
* a pre-rebuild backup present.

What the next release must do with that state, and — just as importantly — what
it must NOT do: no second plan, no re-running the completed unit, and no deleting
the rollback store.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from ragtools.service import engine_ownership
from ragtools.upgrade import relayout
from ragtools.upgrade.relayout import Inventory, Unit

#: A pid that cannot be alive. Chosen far above the Linux default pid_max and
#: never allocated on Windows.
DEAD_PID = 4_294_000_001


@pytest.fixture
def stalled(tmp_path):
    """A machine mid-migration whose engine died. 1 done, 24 blocked."""
    settings = types.SimpleNamespace(
        state_db=str(tmp_path / "index_state.db"),
        data_dir=str(tmp_path),
        collection_name="markdown_kb",
        storage_backend="managed",
    )

    units = [Unit(relayout.KIND_PROJECT, f"project-{i}", 1000 + i)
             for i in range(25)]
    plan = relayout.begin(settings, Inventory(units=units),
                          from_backend="embedded", to_backend="managed",
                          from_strategy="shared", to_strategy="per_project")

    # One unit finished before the engine died — this is why the log said
    # "24 unit(s) to rebuild" while /health reported total 25.
    relayout.mark(settings, plan, units[0], relayout.STATUS_DONE,
                  points_after=1000)
    relayout.block_all(
        settings, plan,
        "ResponseHandlingException: [WinError 10061] No connection could be "
        "made because the target machine actively refused it")

    # The engine's claim outlived the engine.
    engine_ownership.write_manifest(settings, engine_ownership.EngineClaim(
        instance_id="rag-926ed42e987fca91", pid=DEAD_PID,
        executable=str(tmp_path / "qdrant.exe"),
        storage_path=str(tmp_path / "qdrant-server"),
        http_port=21500, grpc_port=21501, started_at=1785310988.985))

    # The rollback store and the state DB survived — the rebuild aborted before
    # the unlink step.
    embedded = tmp_path / "qdrant" / "collection" / "markdown_kb"
    embedded.mkdir(parents=True)
    (embedded / "storage.sqlite").write_bytes(b"pre-migration vectors")
    Path(settings.state_db).write_bytes(b"index state")

    return settings, plan, units


# --- what the state actually is ------------------------------------------


def test_the_fixture_reproduces_the_reported_health_body(stalled):
    settings, plan, _units = stalled
    report = relayout.progress(settings, plan)

    assert report.total == 25
    assert report.done == 1
    assert report.blocked == 24
    assert report.failed == 0
    assert report.pending == 0
    assert report.stalled is True
    assert not report.complete
    assert "10061" in report.blocked_reason


def test_search_is_refused_while_the_plan_is_open(stalled):
    """Correct, and must not be softened. An empty answer from a half-built
    index is the one response that is both wrong and completely convincing."""
    settings, _plan, _units = stalled
    with pytest.raises(relayout.MigrationInProgress):
        relayout.guard_ready(settings)


# --- what recovery must do ------------------------------------------------


def test_every_blocked_unit_is_re_offered_without_an_operator(stalled):
    """Blocked units consume no attempt budget, so recovery needs no reset —
    a service restart is enough."""
    settings, plan, _units = stalled
    todo = relayout.units_to_do(settings, plan)

    assert len(todo) == 24, f"expected 24 units re-offered, got {len(todo)}"
    assert all(u.unit_id != "project-0" for u in todo), (
        "the completed unit was offered again; hours of correct work would be "
        "repeated")


def test_the_completed_unit_is_never_repeated(stalled):
    settings, plan, _units = stalled
    ids = {u.unit_id for u in relayout.units_to_do(settings, plan)}
    assert "project-0" not in ids


def test_recovery_does_not_create_a_second_plan(stalled):
    """One machine, one migration. A second plan would re-capture an inventory
    from an index that is mid-transition."""
    settings, plan, _units = stalled
    assert relayout.active_plan(settings) == plan
    # Re-reading state must never mint a new plan.
    relayout.units_to_do(settings, plan)
    relayout.progress(settings, plan)
    assert relayout.active_plan(settings) == plan


def test_a_manifest_naming_a_dead_pid_is_not_trusted(stalled):
    """`release()` must recognise the claim as already-exited and clear it,
    rather than signalling a pid that may since have been recycled."""
    settings, _plan, _units = stalled
    claim = engine_ownership.read_manifest(settings)
    assert claim is not None and claim.pid == DEAD_PID
    assert not engine_ownership.process_alive(DEAD_PID)

    outcome = engine_ownership.release(settings, claim)

    assert "already exited" in outcome
    assert not engine_ownership.manifest_path(settings).is_file(), (
        "a manifest naming a dead process still vouches for it")


def test_recovery_completes_the_plan_and_restores_search(stalled):
    """The end state. Rebuild the 24, finalise, search works again."""
    settings, plan, _units = stalled

    class RecoveredOwner:
        """The engine is back and every collection holds points."""

        settings = types.SimpleNamespace(collection_name="markdown_kb")
        _frameworks = None
        router = types.SimpleNamespace(
            all_collections=lambda: [f"proj_project-{i}" for i in range(25)],
            write_collection=lambda pid: f"proj_{pid}")

        def __init__(self):
            self.indexed = []
            self.deleted = []
            outer = self

            class Client:
                def get_collections(self):
                    return types.SimpleNamespace(collections=[
                        types.SimpleNamespace(name=f"proj_project-{i}")
                        for i in range(25)])

                def delete_collection(self, name):
                    outer.deleted.append(name)

            self._client = Client()

        def run_full_index(self, project_id=None):
            self.indexed.append(project_id)
            return {"files_indexed": 3, "chunks_indexed": 30, "projects": []}

        def _count_points(self, name):
            return 1000

        def sync_frameworks(self, refresh=False):
            pass

    owner = RecoveredOwner()
    report = relayout.run_pending(owner, settings, plan_id=plan)

    assert report.complete, f"the plan did not finish: {report.describe()}"
    assert len(owner.indexed) == 24, (
        f"expected exactly the 24 blocked units to be rebuilt, got "
        f"{len(owner.indexed)}")
    assert "project-0" not in owner.indexed
    relayout.guard_ready(settings)          # search works again


def test_recovery_never_deletes_the_rollback_store(stalled):
    """The pre-migration embedded store is the user's way back. Automatic
    recovery must not touch it — `rag storage reclaim` is a deliberate act."""
    settings, _plan, _units = stalled
    rollback = Path(settings.data_dir) / "qdrant" / "collection" / "markdown_kb"
    assert rollback.is_dir()

    engine_ownership.release(settings, engine_ownership.read_manifest(settings))
    relayout.units_to_do(settings, _plan)

    assert (rollback / "storage.sqlite").read_bytes() == b"pre-migration vectors"
    assert Path(settings.state_db).is_file(), "the index state was destroyed"


def test_the_remedy_offered_for_this_machine_can_actually_run(stalled):
    """This installation is `managed`, so `rag upgrade --resume` alone is a dead
    end — the CLI refuses while the service is up and cannot build a client while
    it is down."""
    from ragtools.service.app import migration_remedy

    settings, _plan, _units = stalled
    remedy = migration_remedy(settings)

    assert "restart the service" in remedy


def test_a_stale_manifest_is_not_treated_as_a_reattach_candidate(stalled):
    """With the port free, startup must simply spawn — and never consult a claim
    naming a process that is gone."""
    settings, _plan, _units = stalled
    verdict = engine_ownership.inspect_port(settings, 21500)
    assert verdict.action in ("spawn", "refuse")
    assert verdict.action != "reattach"
