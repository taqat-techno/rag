"""v3.4.0: the defects v3.3.0's fixes did not reach, and the recovery for them.

v3.3.0 supervised a *spawned* engine correctly and shipped a real engine log.
The machine did not recover, because the failure enters one step earlier: the
encoder's DNS lookup kills startup AFTER the engine has been spawned, the
lifespan's shutdown half never runs, and the engine is orphaned. Every later boot
reattaches to a process it did not spawn — a path tested only at its own seam.

Every test here is written against a specific v3.3.0 behaviour, and the docstring
says which. Where a test is a release gate it names its NEGATIVE CONTROL: the
thing that makes it fail on the previous release. This repository has shipped
vacuous structural tests before — four in one session — and the rule earned from
that is that a test nobody has seen fail proves nothing.
"""

from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

from ragtools.upgrade import relayout
from ragtools.upgrade.relayout import Inventory, Unit


# --------------------------------------------------------------------------
# P1 — prevent recurrence
# --------------------------------------------------------------------------


def test_a_failed_startup_stops_the_engine_it_spawned():
    """THE ORPHAN. The root of the whole cascade.

    v3.3.0 put the engine teardown after ``yield`` with nothing guarding it, so a
    startup that raised skipped it entirely. The engine kept running with no
    parent and its manifest still vouching for it, and every later boot
    reattached instead of spawning.

    NEGATIVE CONTROL: on v3.3.0 there is no ``try``/``finally`` in ``lifespan``,
    so ``request_stop`` is never reached and this fails.
    """
    import asyncio

    from ragtools.service import app as service_app

    stopped = []

    class Engine:
        def request_stop(self):
            stopped.append(True)
            return "stopped"

    def _boom():
        service_app._engine = Engine()
        raise RuntimeError("[Errno 11001] getaddrinfo failed")

    previous_owner, previous_engine = service_app._owner, service_app._engine
    service_app._owner = None
    service_app._engine = None
    service_app._start_service = _boom          # type: ignore[assignment]
    try:
        async def _drive():
            async with service_app.lifespan(None):  # type: ignore[arg-type]
                pass

        with pytest.raises(RuntimeError):
            asyncio.run(_drive())
    finally:
        service_app._owner, service_app._engine = previous_owner, previous_engine
        import importlib
        importlib.reload(service_app)

    assert stopped, (
        "startup failed after the engine was spawned and nothing stopped it — "
        "that orphan is what every later boot reattaches to")


def test_the_startup_exception_is_captured_before_uvicorn_flattens_it():
    """`SystemExit: 3` is what the operator got; the DNS error is what happened.

    NEGATIVE CONTROL: v3.3.0 has no `startup_failure` at all.
    """
    import asyncio
    import importlib

    from ragtools.service import app as service_app

    def _boom():
        raise ValueError("adapter_config.json: getaddrinfo failed")

    service_app._owner = None
    service_app._start_service = _boom          # type: ignore[assignment]
    try:
        async def _drive():
            async with service_app.lifespan(None):  # type: ignore[arg-type]
                pass

        with pytest.raises(ValueError):
            asyncio.run(_drive())
        captured = service_app.startup_failure()
        assert isinstance(captured, ValueError)
        assert "getaddrinfo" in str(captured)
    finally:
        importlib.reload(service_app)


def test_the_encoder_is_loaded_with_the_network_switched_off():
    """The model ships inside the bundle; resolving it must not need DNS.

    NEGATIVE CONTROL: v3.3.0 calls ``SentenceTransformer(model_name,
    device="cpu")`` with no ``local_files_only`` and no ``cache_folder``, so the
    first attempt is an online one and this assertion has nothing to observe.
    """
    from ragtools.embedding import encoder as enc

    seen = {}

    def _fake(name, **kwargs):
        seen["name"] = name
        seen["local_files_only"] = kwargs.get("local_files_only")
        seen["HF_HUB_OFFLINE"] = os.environ.get("HF_HUB_OFFLINE")
        seen["TRANSFORMERS_OFFLINE"] = os.environ.get("TRANSFORMERS_OFFLINE")
        return object()

    original = enc.SentenceTransformer
    enc.SentenceTransformer = _fake             # type: ignore[assignment]
    try:
        enc.load_model("all-MiniLM-L6-v2")
    finally:
        enc.SentenceTransformer = original      # type: ignore[assignment]

    assert seen["local_files_only"] is True, "the first attempt reached the Hub"
    assert seen["HF_HUB_OFFLINE"] == "1"
    assert seen["TRANSFORMERS_OFFLINE"] == "1"


def test_the_offline_switches_are_restored_afterwards():
    """A global env change that outlives the call would silence real downloads."""
    from ragtools.embedding import encoder as enc

    os.environ.pop("HF_HUB_OFFLINE", None)
    original = enc.SentenceTransformer
    enc.SentenceTransformer = lambda name, **kw: object()  # type: ignore[assignment]
    try:
        enc.load_model("all-MiniLM-L6-v2")
    finally:
        enc.SentenceTransformer = original      # type: ignore[assignment]

    assert "HF_HUB_OFFLINE" not in os.environ


def test_a_missing_model_is_named_not_reduced_to_an_exit_code():
    """`ModelUnavailable`, so the crash record can say "encoder"."""
    from ragtools.embedding import encoder as enc

    def _fail(name, **kwargs):
        raise OSError("[Errno 11001] getaddrinfo failed")

    original = enc.SentenceTransformer
    enc.SentenceTransformer = _fail             # type: ignore[assignment]
    try:
        with pytest.raises(enc.ModelUnavailable) as caught:
            enc.load_model("all-MiniLM-L6-v2")
    finally:
        enc.SentenceTransformer = original      # type: ignore[assignment]

    assert "MODEL failure, not a storage failure" in str(caught.value)
    assert isinstance(caught.value.cause, OSError)


def test_the_crash_record_names_the_encoder_behind_the_system_exit(tmp_path):
    """`last_crash.json` must be diagnosable on its own.

    NEGATIVE CONTROL: v3.3.0 writes ``{"exception_type": "SystemExit",
    "message": "3"}`` and nothing else — the causal exception is absent, which is
    why the previous investigation had to read adjacent WARNING lines by hand.
    """
    import importlib

    from ragtools.service import app as service_app
    from ragtools.service import run as service_run

    cause = ValueError("HEAD https://huggingface.co/... getaddrinfo failed")
    cause.__traceback__ = None
    service_app._startup_failure = cause
    settings = types.SimpleNamespace(data_dir=str(tmp_path))
    try:
        service_run._record_fatal_crash(settings, SystemExit(3), "127.0.0.1", 21420)
    finally:
        importlib.reload(service_app)

    payload = json.loads((tmp_path / "logs" / "last_crash.json").read_text())

    assert payload["exception_type"] == "SystemExit", "the shipped UI reads this"
    assert payload["outer"]["type"] == "SystemExit"
    assert any("getaddrinfo" in entry["message"] for entry in payload["cause_chain"]), (
        payload["cause_chain"])
    assert "logs" in payload and payload["logs"]["engine"] == "qdrant.log"


def test_the_crash_context_is_snapshotted_before_teardown_clears_it(tmp_path):
    """`finally` nulls `_engine` and `_settings` before uvicorn re-raises.

    So a crash record built by asking "what is the engine doing?" at recording
    time describes a machine with no engine and no migration — about a failure
    that had both.
    """
    import importlib

    from ragtools.service import app as service_app
    from ragtools.service import run as service_run

    service_app._startup_context = {
        "engine": {"state": "ready", "pid": 20652},
        "migration": {"plan": 1, "done": 1, "total": 25, "blocked": 24},
    }
    settings = types.SimpleNamespace(data_dir=str(tmp_path))
    try:
        service_run._record_fatal_crash(settings, SystemExit(3), "127.0.0.1", 21420)
    finally:
        importlib.reload(service_app)

    payload = json.loads((tmp_path / "logs" / "last_crash.json").read_text())
    assert payload["engine"]["pid"] == 20652
    assert payload["migration"]["blocked"] == 24


# --------------------------------------------------------------------------
# P2 — lifecycle coverage
# --------------------------------------------------------------------------


def test_a_reattached_engine_says_where_its_output_goes(tmp_path):
    """v3.3.0 gave `data_dir` only to the spawn branch.

    So a reattached engine reported ``log_path: null`` with an empty
    ``log_error`` — and /health flags a missing log only when ``log_error`` is
    truthy, making "this engine has no log at all" completely silent.

    NEGATIVE CONTROL: on v3.3.0 the reattach branch constructs
    `QdrantSupervisor` without `data_dir`, so `log_path` is None.
    """
    from ragtools.storage_managed import QdrantSupervisor, engine_log_path

    supervisor = QdrantSupervisor(
        binary_path="qdrant.exe", storage_path=str(tmp_path),
        http_port=21500, grpc_port=21501, data_dir=str(tmp_path))

    assert supervisor.log_path == str(engine_log_path(tmp_path)), (
        "a supervisor that never spawns still knows where that engine's output "
        "is going — the path is a property of the data directory")


def test_the_engine_log_keeps_one_generation_per_engine(tmp_path):
    """One file, one engine's life — so `qdrant.log.1` IS the process that died.

    NEGATIVE CONTROL: v3.3.0 rotates only past 10 MB, so a small log from a dead
    engine is appended to by its successor and the two runs interleave with
    nothing marking the boundary.
    """
    from ragtools.storage_managed import open_engine_log

    handle, path = open_engine_log(tmp_path)
    handle.write(b"INSTANCE-1 banner\nINSTANCE-1 last words\n")
    handle.close()

    handle, path = open_engine_log(tmp_path)
    handle.write(b"INSTANCE-2 banner\n")
    handle.close()

    current = path.read_bytes()
    previous = path.with_suffix(".log.1").read_bytes()

    assert b"INSTANCE-1" not in current, "two engines' output share one file"
    assert b"INSTANCE-1 last words" in previous, "the dead engine's log was lost"
    assert b"INSTANCE-2" in current


def test_an_unexpected_exit_is_marked_in_the_engine_log(tmp_path):
    """The engine cannot write its own epitaph; the supervisor writes it.

    NEGATIVE CONTROL: v3.3.0 records the death only in `service.log`, so anyone
    reading `qdrant.log.1` has to correlate two files by timestamp.
    """
    from ragtools.service import engine_lifecycle as el
    from ragtools.storage_managed import engine_log_path

    settings = types.SimpleNamespace(data_dir=str(tmp_path))
    engine = el.EngineLifecycle(
        settings, starter=lambda s: (None, None), stopper=lambda s, sup: "",
        max_restarts=0, backoff=(0.0,), sleep=lambda s: None)
    engine._status.pid = 4242
    engine._status.started_at = 0.0
    engine._handle_death(3221225477)

    written = engine_log_path(tmp_path).read_text(encoding="utf-8")
    assert "ragtools engine-exit" in written
    assert "pid=4242" in written
    assert "exit_code=3221225477" in written


def test_a_restart_that_reattaches_keeps_watching(tmp_path):
    """`return proc is not None` told the caller to stop watching.

    A restart that reattached therefore left the engine unwatched for the rest
    of the run — the exact v3.2.0 hole, reopened on the recovery path.

    NEGATIVE CONTROL: on v3.3.0 `_handle_death` returns False here.
    """
    from ragtools.service import engine_lifecycle as el
    from ragtools.service import engine_ownership

    settings = types.SimpleNamespace(data_dir=str(tmp_path))
    engine_ownership.write_manifest(settings, engine_ownership.EngineClaim(
        instance_id="rag-test", pid=31337, executable="qdrant.exe",
        storage_path=str(tmp_path), http_port=21500, grpc_port=21501,
        started_at=1.0))

    reattached = types.SimpleNamespace(proc=None, log_path=None, log_error="")

    def _restart(s):
        # A real reattach requires a manifest — `inspect_port` reattaches only
        # when one vouches for the listener. `_handle_death` clears the dead
        # engine's claim first, so the restart re-establishes it.
        engine_ownership.write_manifest(s, engine_ownership.EngineClaim(
            instance_id="rag-test", pid=31337, executable="qdrant.exe",
            storage_path=str(tmp_path), http_port=21500, grpc_port=21501,
            started_at=2.0))
        return reattached, "http://127.0.0.1:21500"

    engine = el.EngineLifecycle(
        settings, starter=_restart, stopper=lambda s, sup: "",
        max_restarts=1, backoff=(0.0,), sleep=lambda s: None)
    engine._status.pid = 999
    engine._status.started_at = 0.0

    assert engine._handle_death(1) is True, (
        "a restart that reattached reported 'stop watching', which is how the "
        "engine becomes unsupervised again immediately after recovering")
    assert engine.status.pid == 31337, "the manifest pid was not adopted"


def test_a_restart_with_nothing_to_watch_says_so(tmp_path):
    """The honest other half: no handle AND no pid is UNSUPERVISED, not silence.

    NOT a release gate, and it should not pretend to be one: it passes on v3.3.0
    too, because the old ``return proc is not None`` happens to be False here for
    the wrong reason. It exists so the v3.4.0 ``return True`` cannot quietly
    become unconditional — which would leave `_poll_reattached` spinning on a pid
    it does not have, a watcher that looks alive and observes nothing.
    """
    from ragtools.service import engine_lifecycle as el

    settings = types.SimpleNamespace(data_dir=str(tmp_path))
    blind = types.SimpleNamespace(proc=None, log_path=None, log_error="")
    engine = el.EngineLifecycle(
        settings, starter=lambda s: (blind, "http://127.0.0.1:21500"),
        stopper=lambda s, sup: "", max_restarts=1, backoff=(0.0,),
        sleep=lambda s: None)
    engine._status.pid = 999
    engine._status.started_at = 0.0

    assert engine._handle_death(1) is False


# --------------------------------------------------------------------------
# P3 — recover persisted state
# --------------------------------------------------------------------------


def empty_collections(n: int = 25) -> dict:
    """The fixture's real shape: the collections EXIST and hold zero points.

    Not an absent collection — that is the *uncountable* case, and it leads to
    the opposite decision. `/api/status` on the stalled machine enumerated all
    25 and reported `points = 0` for every one.
    """
    return {f"proj_project-{i}": 0 for i in range(n)}


class RecoveringOwner:
    """An owner whose collections start empty and fill when indexed."""

    def __init__(self, settings, *, counts=None, reachable=True, files=1):
        self.settings = settings
        self.counts = dict(empty_collections() if counts is None else counts)
        self.reachable = reachable
        self.files = files
        self.indexed: list[str] = []
        self._frameworks = None
        self.router = types.SimpleNamespace(
            all_collections=lambda: [f"proj_{p}" for p in self.counts],
            write_collection=lambda pid: f"proj_{pid}",
        )
        outer = self

        class Client:
            def get_collections(self):
                if not outer.reachable:
                    raise ConnectionError("[WinError 10061] actively refused")
                return types.SimpleNamespace(collections=[
                    types.SimpleNamespace(name=n) for n in outer.counts])

            def delete_collection(self, name):
                outer.counts.pop(name, None)

        self._client = Client()

    def _count_points(self, collection):
        return self.counts.get(collection, 0)

    def _scan_files(self, project_id=None):
        return [("p", Path(f"f{i}.md")) for i in range(self.files)]

    def run_full_index(self, project_id=None):
        self.indexed.append(project_id)
        self.counts[f"proj_{project_id}"] = 1000
        return {"files_indexed": 1, "chunks_indexed": 1000}

    def sync_frameworks(self, refresh=False):
        pass


@pytest.fixture
def stalled(tmp_path):
    """LAKOSHA-TAQAT: 25 units, 1 done over an EMPTY collection, 24 blocked."""
    settings = types.SimpleNamespace(
        state_db=str(tmp_path / "index_state.db"),
        data_dir=str(tmp_path),
        collection_name="markdown_kb",
        storage_backend="managed",
        enabled_projects=[types.SimpleNamespace(id=f"project-{i}",
                                                path=str(tmp_path))
                          for i in range(25)],
    )
    units = [Unit(relayout.KIND_PROJECT, f"project-{i}", 1000 + i)
             for i in range(25)]
    plan = relayout.begin(settings, Inventory(units=units),
                          from_backend="embedded", to_backend="managed",
                          from_strategy="shared", to_strategy="per_project")
    # The false done: recorded complete, collection holds nothing.
    relayout.mark(settings, plan, units[0], relayout.STATUS_DONE, points_after=0)
    relayout.block_all(
        settings, plan,
        "ResponseHandlingException: [WinError 10061] No connection could be "
        "made because the target machine actively refused it")
    return settings, plan, units


def test_reconcile_resets_a_done_unit_whose_collection_is_empty(stalled):
    """The `1/25 done` beside 25 empty collections.

    NEGATIVE CONTROL: v3.3.0 has no `reconcile` at all — the claim was never
    re-examined, so the plan stayed frozen in the shape the outage left it.
    """
    settings, plan, _units = stalled
    owner = RecoveringOwner(settings, counts=empty_collections())

    report = relayout.reconcile(owner, settings, plan)

    assert ("project", "project-0") in [(k, u) for k, u in report.reset]
    after = relayout.progress(settings, plan)
    assert after.done == 0, "a done unit over an empty collection survived"


def test_reconcile_preserves_verified_work(stalled):
    """Re-indexing what is already correct turns 8 hours into 16."""
    settings, plan, _units = stalled
    counts = empty_collections()
    counts["proj_project-0"] = 1000
    owner = RecoveringOwner(settings, counts=counts)

    report = relayout.reconcile(owner, settings, plan)

    assert not report.reset, report.reset
    assert ("project", "project-0", 1000) in report.preserved
    assert relayout.progress(settings, plan).done == 1


def test_reconcile_never_demotes_a_unit_it_could_not_count(stalled):
    """"I could not ask" must never read as "there is nothing there".

    That conflation disabled search on a correctly-rebuilt v3.1.0 machine.
    """
    settings, plan, _units = stalled
    owner = RecoveringOwner(settings, counts=empty_collections())
    owner.router.write_collection = lambda pid: (_ for _ in ()).throw(
        RuntimeError("router unavailable"))

    report = relayout.reconcile(owner, settings, plan)

    assert ("project", "project-0") in report.unknown
    assert not report.reset, "an uncountable unit was demoted"
    assert relayout.progress(settings, plan).done == 1


def test_reconcile_unblocks_once_storage_is_reachable_again(stalled):
    """The stale `WinError 10061`, re-tested instead of believed.

    NEGATIVE CONTROL: v3.3.0 clears a block only by a unit reaching done, so
    /health reported the 22:38 reason indefinitely.
    """
    settings, plan, _units = stalled
    owner = RecoveringOwner(settings, counts=empty_collections())

    relayout.reconcile(owner, settings, plan)

    after = relayout.progress(settings, plan)
    assert after.blocked == 0, "a lifted blocker was still recorded as blocking"
    assert after.blocked_reason == ""
    assert after.pending == 25


def test_reconcile_leaves_the_block_alone_while_storage_is_still_down(stalled):
    """Unblocking a unit whose blocker is real would just re-park it."""
    settings, plan, _units = stalled
    owner = RecoveringOwner(settings, counts=empty_collections(), reachable=False)

    relayout.reconcile(owner, settings, plan)

    assert relayout.progress(settings, plan).blocked == 24


def test_reconcile_backs_up_the_plan_store_before_rewriting_it(stalled):
    """First release that rewrites persisted migration state unasked."""
    settings, plan, _units = stalled
    owner = RecoveringOwner(settings, counts=empty_collections())

    relayout.reconcile(owner, settings, plan)

    backups = list(Path(settings.data_dir).glob("relayout.db.bak-*"))
    assert backups, "persisted state was rewritten with no way back"


def test_reconcile_never_creates_a_second_plan(stalled):
    """A second plan re-captures an inventory from a half-built store."""
    settings, plan, _units = stalled
    owner = RecoveringOwner(settings, counts=empty_collections())

    relayout.reconcile(owner, settings, plan)
    relayout.reconcile(owner, settings, plan)

    assert relayout.active_plan(settings) == plan


def test_the_stalled_fixture_recovers_to_every_unit_done_and_search_reopens(stalled):
    """P5, in one test: the fixture shape in, a searchable index out.

    NEGATIVE CONTROL: on v3.3.0 the false `done` is preserved, so the plan can
    finalize with `project-0` holding nothing — a migration that completes over a
    collection that never received a vector.
    """
    settings, plan, _units = stalled
    owner = RecoveringOwner(settings, counts=empty_collections())

    report = relayout.run_pending(owner, settings, plan_id=plan)

    assert report.complete, report.describe()
    assert relayout.active_plan(settings) is None, "the plan never finalized"
    assert "project-0" in owner.indexed, (
        "the falsely-done unit was skipped, so it stays empty forever")
    assert len(owner.indexed) == 25, owner.indexed

    verified, problems = relayout.validate(owner, settings, plan)
    assert verified, problems
    relayout.guard_ready(settings)          # raises MigrationInProgress if not


def test_a_project_that_is_empty_by_design_completes_with_its_reason(stalled):
    """Zero points is acceptable — but only when something records WHY."""
    settings, plan, units = stalled
    owner = RecoveringOwner(settings, counts=empty_collections(), files=0)
    owner.run_full_index = lambda project_id=None: {"files_indexed": 0}

    relayout.run_pending(owner, settings, plan_id=plan)

    import sqlite3
    conn = sqlite3.connect(str(Path(settings.state_db).with_name("relayout.db")))
    rows = dict(conn.execute(
        "SELECT unit_id, empty_reason FROM relayout_unit WHERE plan_id=?"
        " AND status='done'", (plan,)).fetchall())
    conn.close()
    assert rows, "every unit failed; none was classified empty-by-design"
    assert all(r == "no indexable files" for r in rows.values()), rows


def test_a_missing_project_path_is_a_failure_not_a_completion(tmp_path):
    """It is the honest answer, it is retryable, and it keeps the unit visible."""
    settings = types.SimpleNamespace(
        state_db=str(tmp_path / "state.db"), data_dir=str(tmp_path),
        collection_name="markdown_kb",
        enabled_projects=[types.SimpleNamespace(
            id="gone", path=str(tmp_path / "does-not-exist"))])
    owner = RecoveringOwner(settings, counts={"proj_gone": 0}, files=0)

    disposition, reason = relayout.classify_empty(
        owner, settings, relayout.KIND_PROJECT, "gone")

    assert disposition == relayout.STATUS_FAILED
    assert "path does not exist" in reason


def test_a_framework_corpus_is_counted_rather_than_assumed(tmp_path):
    """v3.3.0 wrote `after = 0` for frameworks as a LITERAL — never counted."""
    settings = types.SimpleNamespace(state_db=str(tmp_path / "state.db"),
                                     collection_name="markdown_kb")
    owner = RecoveringOwner(settings, counts={"fw_odoo_abc": 4242})
    owner._frameworks = types.SimpleNamespace(all_frameworks=lambda: [
        {"id": "odoo-18", "collection_name": "fw_odoo_abc"}])

    assert relayout.points_for_unit(
        owner, relayout.KIND_FRAMEWORK, "odoo-18") == 4242


def test_the_maintenance_table_owns_migration_recovery():
    """NEGATIVE CONTROL: v3.3.0's table has four tasks and none touch migration.

    The storage probe RAISES while storage is down and does nothing whatsoever
    when it comes back, so a parked plan stayed parked after the outage ended.
    """
    from ragtools.service.maintenance import LOCK_INDEX, build_default_tasks

    owner = types.SimpleNamespace(get_status=lambda: None,
                                  storage_reachable=lambda: (True, ""),
                                  sync_frameworks=lambda refresh=False: None)
    tasks = {t.name: t for t in build_default_tasks(owner)}

    assert "migration-recovery" in tasks, (
        "nothing periodic re-tests a persisted block, so a migration parked by "
        "an outage stays parked after the outage ends")
    task = tasks["migration-recovery"]
    assert task.lock == LOCK_INDEX, "it would stack a rebuild on a running one"
    assert task.run_at_startup is True


def test_resume_refuses_to_start_a_second_worker():
    """Reachable from a timer, an engine transition, a request and the CLI."""
    import importlib
    import threading

    from ragtools.service import app as service_app

    release = threading.Event()
    service_app._settings = types.SimpleNamespace(x=1)
    service_app._owner = types.SimpleNamespace()
    service_app._resume_thread = threading.Thread(
        target=release.wait, daemon=True)
    service_app._resume_thread.start()
    try:
        original = relayout.active_plan
        relayout.active_plan = lambda s: 1       # type: ignore[assignment]
        try:
            assert service_app.resume_migration() is False
        finally:
            relayout.active_plan = original      # type: ignore[assignment]
    finally:
        release.set()
        importlib.reload(service_app)


# --------------------------------------------------------------------------
# P4 — correct interfaces
# --------------------------------------------------------------------------


def test_migration_in_progress_is_a_409_not_a_500():
    """The domain signal that escaped as an unhandled exception.

    NEGATIVE CONTROL: v3.3.0's `/api/search` catches only `ScopeUnresolvedError`,
    so `MigrationInProgress` reaches the blanket handler and becomes
    `500 {"detail": "Internal Server Error"}` — with the explanation stripped off.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ragtools.service.errors import install_domain_handlers

    app = FastAPI()
    install_domain_handlers(app)

    @app.get("/api/search")
    def _search():
        raise relayout.MigrationInProgress(relayout.Progress(
            plan_id=1, status=relayout.PLAN_RUNNING, total=25, done=1,
            blocked=24, blocked_reason="[WinError 10061] actively refused"))

    response = TestClient(app, raise_server_exceptions=False).get("/api/search")

    assert response.status_code == 409, response.status_code
    body = response.json()
    assert body["error"] in {"MIGRATION_IN_PROGRESS", "MIGRATION_BLOCKED"}
    assert body["done"] == 1 and body["total"] == 25
    assert body["remediation"], "a refusal with no way forward"
    assert "blocked_reason_recorded" in body, (
        "a persisted reason must be labelled as recorded, not presented as now")


def test_storage_and_model_failures_are_503_with_their_own_codes():
    """Distinct conditions, distinct codes — never one anonymous 500."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ragtools.embedding.encoder import ModelUnavailable
    from ragtools.service.errors import install_domain_handlers
    from ragtools.service.owner import StorageWentAway

    app = FastAPI()
    install_domain_handlers(app)

    @app.get("/storage")
    def _storage():
        raise StorageWentAway("the managed engine is crashed")

    @app.get("/model")
    def _model():
        raise ModelUnavailable("all-MiniLM-L6-v2", ["/model_cache"], OSError("dns"))

    client = TestClient(app, raise_server_exceptions=False)

    storage = client.get("/storage")
    assert storage.status_code == 503
    assert storage.json()["error"] == "STORAGE_UNAVAILABLE"
    assert storage.headers.get("Retry-After")

    model = client.get("/model")
    assert model.status_code == 503
    assert model.json()["error"] == "MODEL_UNAVAILABLE"


def test_status_separates_live_points_from_historical_bookkeeping():
    """"6,546 files · 91,516 chunks" over a store holding nothing.

    NEGATIVE CONTROL: v3.3.0 merges `points_count` (live) and `total_chunks`
    (historical) into one flat dict with no stated relationship, and the
    dashboard renders the historical one.
    """
    from ragtools.service.owner import (
        AVAILABILITY_EMPTY,
        AVAILABILITY_READY,
        AVAILABILITY_REBUILDING,
        AVAILABILITY_STORAGE_DOWN,
        _availability,
    )

    historical = {"total_chunks": 91516, "total_files": 6546}

    assert _availability(0, historical, {"plan": 1, "done": 1, "total": 25,
                                         "stalled": False}) == AVAILABILITY_REBUILDING
    assert _availability(None, historical, None) == AVAILABILITY_STORAGE_DOWN
    assert _availability(91516, historical, None) == AVAILABILITY_READY
    assert _availability(0, {}, None) == AVAILABILITY_EMPTY
    assert _availability(0, historical, {"plan": 1, "done": 1, "stalled": True}) \
        == "blocked"


def test_index_activity_is_reachable_from_status():
    """"What is it doing?" was computed and surfaced nowhere.

    `index_activity()` returns phase/done/total/age — exactly the signal the
    previous investigation could not get — and its only consumer in v3.3.0 was
    `job_handlers`. Not a route, not a page, not /health.
    """
    import inspect

    from ragtools.service import owner as owner_module

    source = inspect.getsource(owner_module.QdrantOwner._compute_status)
    assert "index_activity" in source, (
        "the one signal that answers 'is the rebuild alive or stuck' is still "
        "not on any status surface")
