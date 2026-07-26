"""Migration: refuse what cannot finish, prove what did, resume what stopped.

Three concerns, and the ordering between them is the point:

* **preflight** runs before anything is stopped, because a migration that runs
  out of disk half-way has destroyed the old index and not built the new one;
* **reconcile** decides whether success may be reported at all — every gate here
  is a failure this product has actually had, on the real corpus;
* **state** answers "can I go back?" without anyone reading code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ragtools.upgrade.preflight import (
    BLOCK,
    OK,
    WARN,
    check_disk,
    check_memory,
    check_model,
    check_port,
    check_projects,
    estimate_required_bytes,
    run_preflight,
)
from ragtools.upgrade.reconcile import (
    FAIL,
    PASS,
    SKIP,
    gate_counts_agree,
    gate_frameworks_deduplicated,
    gate_no_cross_project_leakage,
    gate_no_framework_files_in_project,
    gate_quality_not_regressed,
    reconcile,
)
from ragtools.upgrade.state import (
    BOUNDARY_STEP,
    STEP_BACKED_UP,
    STEP_BINARIES,
    STEP_CONFIG,
    STEP_INDEXING,
    STEP_PREFLIGHT,
    STEP_RECONCILED,
    STEP_SCANNED,
    STEP_STOPPED,
    STEPS,
    UpgradeState,
)

GB = 1024**3


class _Usage:
    def __init__(self, free):
        self.free = free

    def __call__(self, _path):
        return self


# --- preflight ------------------------------------------------------------


def test_the_disk_estimate_is_based_on_the_measured_corpus():
    """1.2 GB for 147,344 points on the real machine. The estimate must be in
    that neighbourhood, times a safety multiple — being optimistic here is what
    strands a machine mid-migration."""
    required = estimate_required_bytes(147_344)
    assert 3 * GB <= required <= 5 * GB


def test_a_small_index_still_demands_a_floor():
    """Logs, backups and the model need room even when the index is tiny."""
    assert estimate_required_bytes(10) >= 2 * GB


def test_insufficient_disk_blocks_before_anything_stops(tmp_path):
    check = check_disk(tmp_path, 147_344, usage=_Usage(1 * GB))
    assert check.status == BLOCK
    assert "free at least" in check.remedy


def test_a_thin_margin_warns_rather_than_blocks(tmp_path):
    required = estimate_required_bytes(147_344)
    check = check_disk(tmp_path, 147_344, usage=_Usage(int(required * 1.2)))
    assert check.status == WARN


def test_ample_disk_passes(tmp_path):
    check = check_disk(tmp_path, 147_344, usage=_Usage(500 * GB))
    assert check.status == OK


def test_an_unmeasurable_disk_warns_instead_of_guessing(tmp_path):
    def _raise(_p):
        raise OSError("no such device")

    assert check_disk(tmp_path, 100, usage=_raise).status == WARN


def test_low_memory_blocks():
    assert check_memory(available=512 * 1024**2).status == BLOCK


def test_sufficient_memory_passes():
    """The indexer streams in bounded windows — peak RSS was ~1.2 GB flat on a
    38k-file corpus — so this is a floor, not a function of index size."""
    assert check_memory(available=4 * GB).status == OK


def test_a_busy_port_warns_because_it_is_probably_our_own_service():
    """During an upgrade the port IS in use — by the service being replaced.
    Blocking there would make every upgrade fail its own preflight."""
    check = check_port("127.0.0.1", 21420, probe=lambda h, p: True)
    assert check.status == WARN
    assert "stops the previous service" in check.remedy


def test_a_free_port_passes():
    assert check_port("127.0.0.1", 21420, probe=lambda h, p: False).status == OK


def test_a_missing_project_is_reported_never_silently_dropped(tmp_path):
    """"My project vanished after the upgrade" is indistinguishable from data
    loss to the person it happens to."""

    class _P:
        def __init__(self, pid, path):
            self.id, self.path = pid, path

    present = tmp_path / "here"
    present.mkdir()
    check = check_projects([_P("a", str(present)), _P("gone", str(tmp_path / "nope"))])

    assert check.status == WARN
    assert "gone" in check.detail
    assert "not removed from config" in check.remedy


def test_all_projects_resolving_passes(tmp_path):
    class _P:
        id, path = "a", str(tmp_path)

    assert check_projects([_P()]).status == OK


def test_a_model_change_blocks_before_reindexing_44000_files():
    class _S:
        embedding_model = "all-MiniLM-L6-v2"

    assert check_model(_S(), expected="all-MiniLM-L6-v2").status == OK
    blocked = check_model(_S(), expected="bge-base-en")
    assert blocked.status == BLOCK


def test_preflight_reports_every_blocker_in_one_pass(tmp_path):
    """Fixing one blocker per failed attempt is a miserable way to upgrade."""

    class _S:
        data_dir = str(tmp_path)
        service_host, service_port = "127.0.0.1", 21420
        embedding_model = "all-MiniLM-L6-v2"
        projects: list = []

    report = run_preflight(_S(), point_count=147_344, usage=_Usage(1 * GB),
                           available_memory=256 * 1024**2,
                           port_probe=lambda h, p: False)

    assert not report.ok
    assert {c.name for c in report.blockers} == {"disk", "memory"}
    assert "BLOCK" in report.render()


# --- reconciliation gates -------------------------------------------------


def test_counts_that_agree_pass():
    assert gate_counts_agree("alpha", 1163, 1163).status == PASS


def test_the_real_divergence_fails_with_a_named_recovery():
    """88,825 tracked vs 60,930 stored: the state DB survived a storage change
    and described a store that no longer existed."""
    result = gate_counts_agree("khayrgate", 88_825, 60_930)
    assert result.failed
    assert "27,895" in result.detail
    assert "rag index --full --project khayrgate" == result.recovery


def test_framework_files_inside_a_project_collection_fail():
    """The scanner excludes the tree and the sync indexes it. If only the second
    happens, every hit appears twice."""
    result = gate_no_framework_files_in_project(
        "alpha", ["alpha/src/a.py", "alpha/platform/odoo/api.py"], ["alpha/platform/odoo"])
    assert result.failed
    assert "odoo" in result.detail


def test_a_clean_project_collection_passes():
    result = gate_no_framework_files_in_project(
        "alpha", ["alpha/src/a.py"], ["alpha/platform/odoo"])
    assert result.status == PASS


def test_a_project_without_dependencies_skips_the_exclusion_gate():
    assert gate_no_framework_files_in_project("alpha", ["a.py"], []).status == SKIP


def test_two_projects_sharing_one_corpus_is_the_success_case():
    entries = [
        {"project": "alpha", "collection": "fw_odoo_1", "build_id": "abc"},
        {"project": "beta", "collection": "fw_odoo_1", "build_id": "abc"},
    ]
    assert gate_frameworks_deduplicated(entries).status == PASS


def test_one_build_under_two_collections_fails():
    """That is a duplicated 32,000-file core, which is exactly what the shared
    corpus model exists to prevent."""
    entries = [
        {"project": "alpha", "collection": "fw_odoo_1", "build_id": "abc"},
        {"project": "beta", "collection": "fw_odoo_2", "build_id": "abc"},
    ]
    result = gate_frameworks_deduplicated(entries)
    assert result.failed
    assert "2 collections for 1 build identities" in result.detail


def test_released_entries_do_not_count_as_links():
    entries = [{"project": "alpha", "collection": "fw_x", "action": "released"}]
    assert gate_frameworks_deduplicated(entries).status == SKIP


def test_any_cross_project_leak_fails_and_blocks_release():
    result = gate_no_cross_project_leakage("alpha", foreign_hits=1)
    assert result.failed
    assert "do not release" in result.recovery


def test_no_leakage_passes():
    assert gate_no_cross_project_leakage("alpha", 0).status == PASS


def test_a_quality_regression_fails_and_keeps_the_old_data():
    """A migration that completes, reconciles and quietly returns worse answers
    is the failure nobody notices until they stop trusting the tool."""
    result = gate_quality_not_regressed(
        {"recall_at_5": 1.0, "mrr": 1.0}, {"recall_at_5": 0.8, "mrr": 0.9})
    assert result.failed
    assert "recall_at_5 1.000 -> 0.800" in result.detail
    assert "upgrade commit" in result.recovery


def test_matching_quality_passes():
    baseline = {"recall_at_5": 1.0, "mrr": 1.0}
    assert gate_quality_not_regressed(baseline, dict(baseline)).status == PASS


def test_no_baseline_skips_rather_than_inventing_a_verdict():
    assert gate_quality_not_regressed(None, {"recall_at_5": 1.0}).status == SKIP


def test_reconcile_names_every_failed_project():
    """Success is every project reconciled OR listed by name with a recovery."""
    report = reconcile(per_project={
        "alpha": {"state_chunks": 10, "qdrant_points": 10, "paths": ["alpha/a.py"],
                  "framework_prefixes": [], "foreign_hits": 0},
        "beta": {"state_chunks": 10, "qdrant_points": 4, "paths": ["beta/b.py"],
                 "framework_prefixes": [], "foreign_hits": 0},
    })

    assert not report.ok
    assert report.failed_scopes == ["beta"]
    assert "UPGRADE INCOMPLETE" in report.render()
    assert "rag index --full --project beta" in report.render()


def test_a_fully_reconciled_migration_reports_ok():
    report = reconcile(per_project={
        "alpha": {"state_chunks": 10, "qdrant_points": 10, "paths": ["alpha/a.py"],
                  "framework_prefixes": [], "foreign_hits": 0},
    })
    assert report.ok


def test_every_failure_carries_a_recovery_action():
    """A gate that says "failed" and stops is a dead end, not a report."""
    report = reconcile(per_project={
        "beta": {"state_chunks": 10, "qdrant_points": 0, "paths": ["beta/x.py"],
                 "framework_prefixes": ["beta/vendor"], "foreign_hits": 3},
    })
    assert report.failures
    assert all(f.recovery for f in report.failures)


# --- resumable state ------------------------------------------------------


def test_a_fresh_upgrade_starts_at_the_first_step():
    assert UpgradeState().next_step == STEPS[0]


def test_resume_returns_the_step_after_the_last_completed_one():
    state = UpgradeState().complete(STEP_SCANNED).complete(STEP_PREFLIGHT)
    assert state.next_step == STEP_STOPPED


def test_before_the_boundary_rollback_is_available():
    state = UpgradeState()
    for step in (STEP_SCANNED, STEP_PREFLIGHT, STEP_STOPPED, STEP_BACKED_UP,
                 STEP_BINARIES, STEP_CONFIG):
        state.complete(step)
    assert state.can_rollback is True
    assert "rollback is available" in state.explain().lower()


def test_writing_to_the_new_store_crosses_into_forward_only():
    state = UpgradeState(backup_path="/data/RAGTools.pre-v3")
    for step in STEPS[:STEPS.index(BOUNDARY_STEP) + 1]:
        state.complete(step)
    assert state.past_boundary is True
    assert state.can_rollback is False
    explanation = state.explain()
    assert "forward-only" in explanation
    assert "/data/RAGTools.pre-v3" in explanation, "the salvage path must be stated"


def test_completing_a_step_twice_is_harmless():
    """Resume re-runs the last step rather than demanding exactly-once
    semantics from every action."""
    state = UpgradeState().complete(STEP_SCANNED).complete(STEP_SCANNED)
    assert state.completed == [STEP_SCANNED]


def test_steps_are_recorded_in_pipeline_order_regardless_of_call_order():
    state = UpgradeState().complete(STEP_CONFIG).complete(STEP_SCANNED)
    assert state.completed == [STEP_SCANNED, STEP_CONFIG]


def test_an_unknown_step_is_refused():
    with pytest.raises(ValueError, match="unknown upgrade step"):
        UpgradeState().complete("teleport")


def test_a_project_is_either_reconciled_or_failed_never_neither():
    state = UpgradeState()
    state.record_project("alpha", ok=True)
    state.record_project("beta", ok=False, reason="counts differ", recovery="reindex")

    assert state.reconciled == ["alpha"]
    assert state.failed["beta"]["recovery"] == "reindex"


def test_a_retried_project_moves_between_lists_without_duplicating():
    state = UpgradeState()
    state.record_project("beta", ok=False, reason="counts differ")
    state.record_project("beta", ok=True)
    assert state.reconciled == ["beta"]
    assert "beta" not in state.failed


def test_success_requires_every_step_and_zero_failures():
    state = UpgradeState()
    for step in STEPS:
        state.complete(step)
    state.record_project("alpha", ok=True)
    assert state.complete_and_reconciled is True

    state.record_project("beta", ok=False, reason="x", recovery="y")
    assert state.complete_and_reconciled is False, (
        "an upgrade with a failed project must not report success"
    )


def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "upgrade.json"
    state = UpgradeState(from_version="2.7.0", to_version="3.0.0")
    state.complete(STEP_SCANNED).complete(STEP_PREFLIGHT)
    state.record_project("alpha", ok=True)
    state.save(path)

    restored = UpgradeState.load(path)
    assert restored.next_step == STEP_STOPPED
    assert restored.reconciled == ["alpha"]
    assert restored.from_version == "2.7.0"


def test_saving_is_atomic(tmp_path):
    """A crash during the bookkeeping is exactly when this file matters; a torn
    write would leave the machine in a state nothing can classify."""
    path = tmp_path / "upgrade.json"
    UpgradeState().complete(STEP_SCANNED).save(path)
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads(path.read_text(encoding="utf-8"))["completed"] == [STEP_SCANNED]


def test_a_corrupt_state_file_reads_as_not_started(tmp_path):
    """Refusing to run because the least important file is unreadable would
    strand the machine for no reason."""
    path = tmp_path / "upgrade.json"
    path.write_text("{not json", encoding="utf-8")
    assert UpgradeState.load(path).next_step == STEPS[0]


def test_unknown_fields_in_a_newer_state_file_are_ignored(tmp_path):
    """A state file written by a later version must not crash this one."""
    path = tmp_path / "upgrade.json"
    path.write_text(json.dumps({"completed": [STEP_SCANNED], "future_field": 1}),
                    encoding="utf-8")
    assert UpgradeState.load(path).completed == [STEP_SCANNED]
