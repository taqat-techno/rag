"""The rest of the v3.2.0 field defects, each pinned by the fact that found it.

Every test here corresponds to something that was measured on a live installed
machine rather than reasoned about: a 62-second status call, a dashboard
reporting zero points against 145,906 indexed chunks, a scheduler with thirteen
passing tests that had never once run, and a remedy string the product printed
for a command that could not execute.
"""

from __future__ import annotations

import ast
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "ragtools"
ROOT = Path(__file__).resolve().parent.parent


# --- a skipped migration unit is not a finished one -----------------------


def test_a_busy_index_leaves_the_unit_unfinished(tmp_path):
    """The integrity bug.

    `run_full_index` takes the index mutex NON-blocking and returns
    `{"busy": True}` when another run holds it — a watcher tick is enough. That
    return value was discarded and the unit marked DONE having been indexed zero
    times. Once `units_all_done` was split from `validate` in v3.2.0, this
    stopped merely stalling the plan and started FINISHING it: search comes back
    on over an index missing that project.
    """
    from ragtools.upgrade import relayout
    from ragtools.upgrade.relayout import Inventory, Unit

    settings = types.SimpleNamespace(state_db=str(tmp_path / "state.db"),
                                     collection_name="markdown_kb")
    plan = relayout.begin(
        settings,
        Inventory(units=[Unit(relayout.KIND_PROJECT, "alpha", 500)]),
        from_backend="embedded", to_backend="managed",
        from_strategy="shared", to_strategy="per_project")

    class BusyOwner:
        settings = types.SimpleNamespace(collection_name="markdown_kb")
        _frameworks = None
        router = types.SimpleNamespace(
            all_collections=lambda: ["proj_alpha"],
            write_collection=lambda pid: f"proj_{pid}")
        _client = types.SimpleNamespace(
            get_collections=lambda: types.SimpleNamespace(collections=[]))

        def run_full_index(self, project_id=None):
            # Exactly what a mutex-skipped run returns.
            return {"files_indexed": 0, "chunks_indexed": 0, "projects": [],
                    "busy": True}

        def _count_points(self, name):
            return 0

        def sync_frameworks(self, refresh=False):
            pass

    relayout.run_pending(BusyOwner(), settings, plan_id=plan)

    report = relayout.progress(settings, plan)
    assert report is not None
    assert report.done == 0, (
        "a unit that was SKIPPED was recorded as rebuilt; the migration would "
        "finalise over an index that project is missing from")
    assert not relayout.units_all_done(settings, plan)


def test_the_busy_return_value_is_actually_inspected():
    """Structural guard: the return value must not go back to being discarded."""
    source = (SRC / "upgrade" / "relayout.py").read_text(encoding="utf-8")
    idx = source.index("owner.run_full_index(project_id=unit.unit_id)")
    window = source[idx - 120:idx + 700]
    assert "busy" in window, (
        "run_full_index's result is not inspected for a skipped run")


# --- status is bounded and honest ----------------------------------------


class DeadStorageOwner:
    """An owner whose engine is gone — the state both machines were left in.

    It carries settings but no client and no encoder: the storage-down branch is
    allowed to read the CONFIGURATION (how many projects exist does not depend
    on the store) and is not allowed to reach for the engine. ``counted`` staying
    empty is what proves the second half.
    """

    def __init__(self):
        self._status_snapshot = None
        self._settings = types.SimpleNamespace(projects=[])
        self.counted = []

    def storage_reachable(self):
        return False, "ResponseHandlingException: [WinError 10061]"


def test_a_dead_engine_does_not_trigger_n_times_two_round_trips(tmp_path):
    """Measured at 62.11 s with 25 collections, all of it under the owner lock.

    `_count_points` makes TWO failing calls per collection before returning 0,
    and `_compute_status` holds `self._lock` for the whole sweep — so a single
    dashboard poll stalled every search, index and rebuild for a minute.
    """
    from ragtools.service.owner import QdrantOwner

    owner = DeadStorageOwner()
    status = QdrantOwner.get_status(owner)  # unbound: no encoder, no client

    assert status["stale"] is True
    assert status["storage_reachable"] is False
    assert owner.counted == [], "the dead engine was queried anyway"


def test_a_dead_engine_reports_unknown_points_not_zero(tmp_path):
    """`points_count: 0` beside `total_chunks: 145906` is not a status page, it
    is a wrong answer that looks like a right one. Unknown and zero lead to
    opposite conclusions."""
    from ragtools.service.owner import QdrantOwner

    status = QdrantOwner.get_status(DeadStorageOwner())
    assert status["points_count"] is None, (
        "an unreachable engine reported 0 points, which is indistinguishable "
        "from a genuinely empty index")


# --- the maintenance table actually runs ---------------------------------


def test_the_storage_probe_can_fail():
    """It called `storage_reachable()` and threw the answer away. Since that
    method never raises, the task recorded success while the store was dead."""
    from ragtools.service.maintenance import build_default_tasks

    class Down:
        def storage_reachable(self):
            return False, "engine gone"

        def get_status(self):
            return {}

        def sync_frameworks(self, refresh=False):
            pass

    probe = {t.name: t for t in build_default_tasks(Down())}["storage-probe"]
    with pytest.raises(RuntimeError):
        probe.action()


def test_the_scheduler_is_wired_into_the_service():
    """It shipped with thirteen passing tests and was referenced from nowhere in
    `src/`. Full coverage, never executed."""
    source = (SRC / "service" / "app.py").read_text(encoding="utf-8")
    assert "MaintenanceScheduler" in source, (
        "the maintenance scheduler is still not constructed by the service")
    assert "start_maintenance" in source

    tree = ast.parse(source)
    called = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "start_maintenance" in called, "the scheduler is defined but never started"


# --- the advertised remedy can execute -----------------------------------


def test_the_remedy_is_not_hard_coded_to_a_command_that_cannot_run():
    """`/health` returned `rag upgrade --resume` unconditionally. On a managed
    installation that refuses while the service is up and raises while it is
    down, because the engine is down with it."""
    source = (SRC / "service" / "routes.py").read_text(encoding="utf-8")
    idx = source.index('"retry"')
    line = source[idx:idx + 120]
    assert "_migration_remedy" in line, (
        "the retry instruction is still a hard-coded string rather than one "
        "computed for this installation")


def test_the_remedy_differs_by_backend():
    from ragtools.service.app import migration_remedy

    embedded = migration_remedy(types.SimpleNamespace(storage_backend="embedded"))
    managed = migration_remedy(types.SimpleNamespace(storage_backend="managed"))
    assert embedded != managed
    assert "restart the service" in managed


def test_the_service_exposes_a_resume_endpoint():
    """The service owns the engine, so the service must be able to do the
    resume — otherwise the remedy has nowhere to run."""
    source = (SRC / "service" / "routes.py").read_text(encoding="utf-8")
    assert '"/api/migration/resume"' in source


def test_the_cli_forwards_resume_instead_of_refusing():
    source = (SRC / "cli.py").read_text(encoding="utf-8")
    idx = source.index("if resume:")
    body = source[idx:idx + 2600]
    assert "/api/migration/resume" in body, (
        "`rag upgrade --resume` still dead-ends when the service is running")


def test_the_offline_rebuild_refuses_on_a_managed_installation():
    """`settings.qdrant_path` is the EMBEDDED store. On a managed install that
    directory is the pre-migration index kept for rollback — deleting it would
    destroy the one copy the migration deliberately preserved while leaving the
    live engine untouched."""
    source = (SRC / "cli.py").read_text(encoding="utf-8")
    idx = source.index("def rebuild()")
    # The WHOLE function, not a fixed character budget: a 2600-char
    # window silently stopped containing the rmtree the moment the
    # offline branch grew a guard, and a test that cannot find what it
    # is ordering is not passing.
    body = source[idx:source.index("\n@app.command()", idx)]
    guard_at = body.index('backend != "embedded"')
    rmtree_at = body.index("shutil.rmtree(qdrant_path)")
    assert guard_at < rmtree_at, (
        "the embedded-only branch deletes the rollback store before checking "
        "which backend is in force")


# --- the version contract -------------------------------------------------


def test_the_client_requirement_is_bounded():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if '"qdrant-client' in ln)
    assert "<" in line, (
        "qdrant-client has no upper bound; that is how v3.2.0 shipped client "
        "1.18.0 against server 1.15.5")


def test_psutil_is_declared():
    """Two of the four ownership proofs silently do not exist without it, and it
    was absent from the v3.2.0 bundle."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"psutil' in text


def test_psutil_is_named_in_the_bundle_spec():
    """Declared as a dependency AND named to the bundler.

    Relying on PyInstaller to find `import psutil` inside a `try:` block is the
    same "decided by luck" property the declaration exists to remove.
    """
    text = (ROOT / "rag.spec").read_text(encoding="utf-8")
    assert '"psutil"' in text


def test_the_compatibility_gate_exists_and_agrees_with_the_pins():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_qdrant_compat.py")],
        capture_output=True, text=True)
    assert result.returncode == 0, (
        f"the shipped client/server pair is outside the supported window:\n"
        f"{result.stdout}\n{result.stderr}")


def test_the_gate_runs_in_ci_on_every_platform():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert text.count("check_qdrant_compat.py") >= 3, (
        "the compatibility gate must run on all three release platforms")
