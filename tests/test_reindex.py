"""S11 — consolidated reindex: the quality gate and the dedup-aware plan (pure core).

The one unavoidable re-index (§29) is only safe if it is *planned* and *gated*:
the plan must store framework corpora once per build identity and keep project
collections free of framework content, and the result must be diffed against the
stored S9 baseline so a re-index that silently degrades retrieval is refused. A
model mismatch between the legacy corpus and the target is detected and refused
with a named reason. This pins those pure pieces; the actual indexing run layers
on top.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S11 -> G11; §29)
"""

import pytest

from ragtools.reindex import (
    QualityGateResult,
    check_migration_model,
    compare_to_baseline,
    plan_reindex,
)
from ragtools.embedding.backend import ModelMismatchError


def _metrics(recall5, mrr):
    return {"metrics": {"file_recall_at_5": recall5, "mean_file_mrr": mrr}}


# --- quality gate: diff against the stored baseline ---------------------


def test_identical_result_passes_the_gate():
    r = compare_to_baseline(_metrics(1.0, 1.0), _metrics(1.0, 1.0))
    assert isinstance(r, QualityGateResult)
    assert r.passed is True
    assert r.regressions == []


def test_improved_recall_passes():
    assert compare_to_baseline(_metrics(0.8, 0.7), _metrics(0.95, 0.85)).passed is True


def test_recall_drop_beyond_tolerance_fails_and_is_named():
    r = compare_to_baseline(_metrics(1.0, 1.0), _metrics(0.7, 1.0), recall_tolerance=0.05)
    assert r.passed is False
    assert "file_recall_at_5" in r.regressions


def test_recall_drop_within_tolerance_passes():
    r = compare_to_baseline(_metrics(1.0, 1.0), _metrics(0.97, 1.0), recall_tolerance=0.05)
    assert r.passed is True


def test_mrr_regression_fails():
    r = compare_to_baseline(_metrics(1.0, 0.9), _metrics(1.0, 0.6), recall_tolerance=0.0)
    assert r.passed is False
    assert "mean_file_mrr" in r.regressions


# --- dedup-aware reindex plan (§29 / S7) --------------------------------


_ODOO_EE = {"name": "odoo", "version": "19.0", "edition": "enterprise", "build_id": "sha-AAA"}
_ODOO_EE_2 = {"name": "odoo", "version": "19.0", "edition": "enterprise", "build_id": "sha-BBB"}


def test_two_projects_same_build_share_one_framework_collection():
    plan = plan_reindex(
        projects=[("proj-a", "uuid-a"), ("proj-b", "uuid-b")],
        links=[("proj-a", _ODOO_EE), ("proj-b", _ODOO_EE)],  # SAME build_id
    )
    assert len(plan.project_collections) == 2        # one per project
    assert len(plan.framework_collections) == 1      # deduped by build_id
    assert plan.framework_reuse_count == 1           # second link reused


def test_different_builds_get_separate_framework_collections():
    plan = plan_reindex(
        projects=[("proj-a", "uuid-a"), ("proj-b", "uuid-b")],
        links=[("proj-a", _ODOO_EE), ("proj-b", _ODOO_EE_2)],  # different build_id
    )
    assert len(plan.framework_collections) == 2
    assert plan.framework_reuse_count == 0


def test_project_collections_are_distinct_from_framework_collections():
    # A project's own collection must not be a framework collection (no
    # framework content mixed into the project corpus).
    plan = plan_reindex(
        projects=[("proj-a", "uuid-a")],
        links=[("proj-a", _ODOO_EE)],
    )
    assert set(plan.project_collections).isdisjoint(plan.framework_collections)


# --- model-mismatch refusal on migration --------------------------------


def test_matching_model_migration_is_allowed():
    meta = {"model_name": "all-MiniLM-L6-v2", "dimension": 384, "normalize": True}
    check_migration_model(meta, meta)  # no raise


def test_model_mismatch_between_legacy_and_target_is_refused():
    legacy = {"model_name": "all-MiniLM-L6-v2", "dimension": 384, "normalize": True}
    target = {"model_name": "bge-large", "dimension": 1024, "normalize": True}
    with pytest.raises(ModelMismatchError):
        check_migration_model(legacy, target)
