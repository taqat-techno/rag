"""The gates that decide whether a migration may be called successful.

Five properties, each one a failure this product has actually had:

1. **counts agree** — the state DB and Qdrant disagreed by 27,895 chunks on the
   real corpus, and nothing noticed until a manual count;
2. **no framework files in a project collection** — the scanner excludes them
   and the sync indexes them; if the two disagree, every hit appears twice;
3. **one collection per build identity** — two projects vendoring one Odoo must
   share a corpus, or the dedup that justifies the whole model did not happen;
4. **no cross-project leakage** — the isolation boundary is the reason for
   per-project collections;
5. **retrieval did not regress** — a re-index that silently loses recall is
   worse than one that fails loudly.

An upgrade reports success only when every required project passes, or is listed
by name as failed **with a recovery action**. "Mostly migrated" is not a state
this product is allowed to leave a machine in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

PASS = "pass"
FAIL = "fail"
SKIP = "skip"


@dataclass(frozen=True)
class GateResult:
    gate: str
    status: str
    detail: str
    #: What the operator should do. Required for a failure — a gate that says
    #: "reconciliation failed" and stops is a dead end, not a report.
    recovery: str = ""
    scope: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL


@dataclass
class ReconcileReport:
    results: list[GateResult] = field(default_factory=list)

    def add(self, result: GateResult) -> None:
        self.results.append(result)

    @property
    def failures(self) -> list[GateResult]:
        return [r for r in self.results if r.failed]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def failed_scopes(self) -> list[str]:
        return sorted({r.scope for r in self.failures if r.scope})

    def render(self) -> str:
        lines = []
        for result in self.results:
            head = f"  [{result.status}] {result.gate}"
            if result.scope:
                head += f" ({result.scope})"
            lines.append(f"{head}: {result.detail}")
            if result.failed and result.recovery:
                lines.append(f"        -> {result.recovery}")
        if self.failures:
            lines.append("")
            lines.append(
                f"UPGRADE INCOMPLETE — {len(self.failures)} gate(s) failed for: "
                + ", ".join(self.failed_scopes or ["(unscoped)"])
            )
        return "\n".join(lines)


def gate_counts_agree(project_id: str, state_chunks: int, qdrant_points: int) -> GateResult:
    """State DB and Qdrant must report the same number of chunks.

    The observed divergence was 88,825 tracked versus 60,930 stored: the state
    DB survived a storage-layout change and described a store that no longer
    existed, so every incremental run "skipped" files whose vectors were gone.
    """
    if state_chunks == qdrant_points:
        return GateResult("counts", PASS, f"{qdrant_points:,} chunks", scope=project_id)
    return GateResult(
        "counts", FAIL,
        f"state DB {state_chunks:,} vs Qdrant {qdrant_points:,} "
        f"(delta {abs(state_chunks - qdrant_points):,})",
        recovery=f"rag index --full --project {project_id}",
        scope=project_id,
    )


def gate_no_framework_files_in_project(
    project_id: str, project_paths: list[str], framework_prefixes: list[str]
) -> GateResult:
    """A declared dependency's files must not also live in the project collection.

    Both halves have to be true together: the scanner excludes the tree AND the
    sync indexes it into the shared corpus. If only the first happens the files
    vanish from search; if only the second, every hit appears twice.
    """
    if not framework_prefixes:
        return GateResult("framework-exclusion", SKIP, "no dependencies declared",
                          scope=project_id)
    leaked = [
        path for path in project_paths
        if any(path.startswith(prefix) for prefix in framework_prefixes)
    ]
    if not leaked:
        return GateResult("framework-exclusion", PASS,
                          f"0 of {len(project_paths):,} files are framework files",
                          scope=project_id)
    return GateResult(
        "framework-exclusion", FAIL,
        f"{len(leaked)} framework file(s) in the project collection, e.g. {leaked[0]}",
        recovery=f"rag index --full --project {project_id} (re-applies the exclusion), "
                 "then re-run the framework sync",
        scope=project_id,
    )


def gate_frameworks_deduplicated(entries: list[dict]) -> GateResult:
    """One collection per build identity, however many projects use it.

    ``entries`` are sync results. Two projects on the same build producing two
    collections means the dedup that justifies the shared-corpus model did not
    happen, and the index is carrying a duplicate of a 32,000-file core.
    """
    linked = [e for e in entries if e.get("action") != "released"]
    if not linked:
        return GateResult("framework-dedup", SKIP, "no framework corpora")
    by_collection: dict[str, set] = {}
    for entry in linked:
        by_collection.setdefault(entry["collection"], set()).add(entry.get("project"))
    duplicates = [
        collection for collection, projects in by_collection.items() if len(projects) > 1
    ]
    detail = (f"{len(by_collection)} corpus/corpora for {len(linked)} link(s); "
              f"{len(duplicates)} shared by more than one project")
    # Sharing is the SUCCESS case; the failure is the same build appearing under
    # two collection names, which shows up as more collections than build ids.
    build_ids = {e.get("build_id") or e.get("collection") for e in linked}
    if len(by_collection) > len(build_ids):
        return GateResult(
            "framework-dedup", FAIL,
            f"{len(by_collection)} collections for {len(build_ids)} build identities",
            recovery="rag upgrade repair --frameworks (re-registers by build identity)",
        )
    return GateResult("framework-dedup", PASS, detail)


def gate_no_cross_project_leakage(
    project_id: str, foreign_hits: int, probe: str = ""
) -> GateResult:
    """A scoped search must return nothing belonging to another project.

    This is the boundary the per-project collection model exists to create; a
    single foreign document means the routing is filtering rather than isolating.
    """
    if foreign_hits == 0:
        return GateResult("isolation", PASS,
                          f"0 foreign documents for probe {probe!r}" if probe
                          else "0 foreign documents", scope=project_id)
    return GateResult(
        "isolation", FAIL,
        f"{foreign_hits} document(s) from another project returned",
        recovery="do not release. Verify CollectionRouter.read_collections and "
                 "the scope resolution for this project",
        scope=project_id,
    )


def gate_quality_not_regressed(
    baseline: Optional[dict], measured: Optional[dict], tolerance: float = 0.0
) -> GateResult:
    """Recall@5 and MRR must not drop against the captured baseline.

    A migration that completes, reconciles and quietly returns worse answers is
    the failure mode nobody notices until they stop trusting the tool.
    """
    if not baseline or not measured:
        return GateResult("quality", SKIP, "no baseline captured")
    drops = []
    for metric in ("recall_at_5", "mrr"):
        before = float(baseline.get(metric, 0) or 0)
        after = float(measured.get(metric, 0) or 0)
        if after < before - tolerance:
            drops.append(f"{metric} {before:.3f} -> {after:.3f}")
    if drops:
        return GateResult(
            "quality", FAIL, "; ".join(drops),
            recovery="keep the previous data directory; investigate before "
                     "running `rag upgrade commit`",
        )
    return GateResult(
        "quality", PASS,
        f"recall@5 {measured.get('recall_at_5', 0):.3f}, mrr {measured.get('mrr', 0):.3f}",
    )


def reconcile(
    *,
    per_project: dict,
    framework_entries: Optional[list[dict]] = None,
    baseline: Optional[dict] = None,
    measured: Optional[dict] = None,
) -> ReconcileReport:
    """Run every gate.

    ``per_project`` maps project id -> dict with ``state_chunks``,
    ``qdrant_points``, ``paths``, ``framework_prefixes``, ``foreign_hits``.
    """
    report = ReconcileReport()
    for project_id, facts in sorted(per_project.items()):
        report.add(gate_counts_agree(
            project_id, int(facts.get("state_chunks", 0)), int(facts.get("qdrant_points", 0))))
        report.add(gate_no_framework_files_in_project(
            project_id, list(facts.get("paths", [])),
            list(facts.get("framework_prefixes", []))))
        report.add(gate_no_cross_project_leakage(
            project_id, int(facts.get("foreign_hits", 0)), facts.get("probe", "")))
    report.add(gate_frameworks_deduplicated(list(framework_entries or [])))
    report.add(gate_quality_not_regressed(baseline, measured))
    return report
