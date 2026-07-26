"""Consolidated reindex — quality gate + dedup-aware plan (RAG v3, Stage S11).

Local-mode Qdrant has no snapshot/export, so reaching the target layout costs
exactly one full re-index (§29). That spend is only safe if it is *planned* and
*gated*:

* :func:`plan_reindex` lays out one collection per project plus framework
  collections deduplicated by build identity (composing
  :func:`ragtools.identity.framework_collection_name`), so a framework corpus is
  stored once and never mixed into a project's own collection.
* :func:`compare_to_baseline` diffs a fresh eval result against the stored S9
  baseline and refuses a re-index that degrades retrieval.
* :func:`check_migration_model` refuses a legacy→target migration whose model
  metadata does not match (composing
  :func:`ragtools.embedding.backend.assert_model_compatible`).

Pure and offline — it plans and checks; it does not itself write vectors.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S11 -> G11; §29)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ragtools.embedding.backend import assert_model_compatible
from ragtools.identity import framework_collection_name, project_collection_name

#: Metrics the gate guards, and the direction that counts as regression (all
#: "higher is better", so a drop is bad).
_GATED_METRICS = ("file_recall_at_5", "mean_file_mrr")


@dataclass
class QualityGateResult:
    """Outcome of diffing a re-index's eval result against the baseline."""

    passed: bool
    regressions: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


def compare_to_baseline(
    baseline: dict, current: dict, *, recall_tolerance: float = 0.0
) -> QualityGateResult:
    """Diff ``current`` eval metrics against ``baseline``; fail on regression.

    A gated metric regresses when it drops more than ``recall_tolerance`` below
    baseline. ``passed`` is true only when nothing regressed — the re-index is
    trustworthy exactly when retrieval did not get worse.
    """
    b = baseline.get("metrics", baseline)
    c = current.get("metrics", current)
    regressions: list[str] = []
    details: dict = {}
    for metric in _GATED_METRICS:
        bv = float(b.get(metric, 0.0))
        cv = float(c.get(metric, 0.0))
        details[metric] = {"baseline": bv, "current": cv, "delta": round(cv - bv, 6)}
        if cv < bv - recall_tolerance:
            regressions.append(metric)
    return QualityGateResult(
        passed=not regressions, regressions=regressions, details=details
    )


@dataclass
class ReindexPlan:
    """The proposed collection layout for the one re-index."""

    project_collections: list[str] = field(default_factory=list)
    framework_collections: list[str] = field(default_factory=list)
    #: How many links resolved to an already-planned framework collection —
    #: i.e. corpus indexing avoided by build-identity dedup.
    framework_reuse_count: int = 0


def plan_reindex(projects, links) -> ReindexPlan:
    """Plan the target layout: one collection per project, frameworks deduped.

    ``projects`` is ``[(project_id, project_uuid), ...]``; ``links`` is
    ``[(project_id, framework_dict), ...]`` where the framework dict carries
    ``name`` / ``version`` / ``edition`` / ``build_id``. Two links whose
    frameworks share a build identity resolve to the SAME framework collection —
    that reuse is the whole economic point of S7/S11.
    """
    project_collections = [project_collection_name(uuid) for _pid, uuid in projects]

    fw_seen: dict[str, None] = {}
    reuse = 0
    for _pid, fw in links:
        name = framework_collection_name(
            fw["name"],
            version=fw.get("version", ""),
            edition=fw.get("edition", ""),
            build_id=fw.get("build_id"),
        )
        if name in fw_seen:
            reuse += 1
        else:
            fw_seen[name] = None
    return ReindexPlan(
        project_collections=project_collections,
        framework_collections=list(fw_seen),
        framework_reuse_count=reuse,
    )


def check_migration_model(legacy, target) -> None:
    """Refuse a legacy→target migration whose embedding model does not match.

    Re-indexing under a different model silently invalidates every stored score;
    the mismatch is caught here with a named reason
    (:class:`~ragtools.embedding.backend.ModelMismatchError`) before any vector
    is written.
    """
    assert_model_compatible(legacy, target)
