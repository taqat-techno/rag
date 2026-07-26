"""Retrieval router — ResolvedScope + cross-collection fan-out/fusion (S10, §16-17).

Every search is scoped SERVER-SIDE from the client profile, never from a
caller-supplied project or collection name. :func:`resolve_scope` computes that
scope (fail-closed: an unresolved scope raises, never widens to "all"), and
:func:`fan_out_and_fuse` queries the resolved collections in parallel with a
per-collection budget, isolates failures (project = fail-closed, framework =
explicit degraded partial), and fuses the survivors by RRF
(:func:`ragtools.retrieval.fusion.reciprocal_rank_fusion`) so non-comparable
per-collection scores never leak into the ranking.

This module composes the S12 authz core (:func:`ragtools.profiles.authorize_projects`),
the S6/S7 identity layer (collection refs), and the S10 fusion core.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S10 §16-17 -> G10)
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Callable, Optional

from ragtools.profiles import ClientProfile, authorize_projects
from ragtools.retrieval.fusion import reciprocal_rank_fusion


@dataclass
class ResolvedScope:
    """The server-side scope a search actually ran against (§16.1).

    Echoed back on every response so a client sees the RESOLVED scope, not its
    request. ``primary``/``linked``/``cross_project`` are collection refs;
    ``refused``/``degraded`` are ``(ref, reason)`` pairs.
    """

    client_profile_id: str
    resolution: str  # "explicit" | "inferred" | "unresolved"
    primary: list[str] = field(default_factory=list)
    linked: list[str] = field(default_factory=list)
    cross_project: list[str] = field(default_factory=list)
    refused: list[tuple] = field(default_factory=list)
    degraded: list[tuple] = field(default_factory=list)

    def searchable(self) -> list[str]:
        """All collection refs this scope may search, in tier-ish order."""
        return [*self.primary, *self.cross_project, *self.linked]


def resolve_scope(
    profile: ClientProfile,
    requested: Optional[list[str]],
    *,
    links_by_project: Optional[dict] = None,
) -> ResolvedScope:
    """Resolve a search's scope from the profile — fail-closed.

    ``requested`` present → ``explicit``; absent but the profile auto-scopes to a
    single project → ``inferred``; otherwise :class:`~ragtools.profiles.ScopeDenied`
    (never widened to all). Foreign requested projects are dropped into
    ``refused`` with a reason, never searched. ``linked`` frameworks are read
    from ``links_by_project`` for the authorized projects only — a caller cannot
    inject one through the request.
    """
    authorized = authorize_projects(profile, requested)  # raises ScopeDenied if unresolved
    resolution = "explicit" if requested is not None else "inferred"

    primary = authorized[:1]
    cross_project = authorized[1:] if resolution == "explicit" else []

    links_by_project = links_by_project or {}
    linked: list[str] = []
    for pid in authorized:
        for ref in links_by_project.get(pid, []):
            if ref not in linked:
                linked.append(ref)

    requested_norm = [r.strip() for r in (requested or []) if r and r.strip()]
    authorized_set = set(authorized)
    refused = [
        (r, "not within the client profile's allowed projects")
        for r in requested_norm
        if r not in authorized_set
    ]

    return ResolvedScope(
        client_profile_id=profile.profile_id,
        resolution=resolution,
        primary=primary,
        linked=linked,
        cross_project=cross_project,
        refused=refused,
    )


@dataclass
class FusedResults:
    """Outcome of a cross-collection fan-out: fused hits + provenance."""

    hits: list = field(default_factory=list)
    degraded: list = field(default_factory=list)  # (ref, reason)
    per_collection: dict = field(default_factory=dict)  # ref -> count


def fan_out_and_fuse(
    scope: ResolvedScope,
    search_one: Callable[[str], list],
    *,
    key: Callable,
    project_refs: Optional[set] = None,
    timeout: float = 5.0,
    max_workers: int = 8,
) -> FusedResults:
    """Query every collection in ``scope`` in parallel, fuse survivors by RRF.

    ``search_one(ref)`` returns that collection's ranked hits. Failure isolation
    (§17.1): a **project** collection error is fail-closed (re-raised — an
    incomplete project search must never look complete); a **framework**
    collection error or timeout yields an explicit ``degraded`` entry and the
    query proceeds on the rest. Never the ``except: return []`` anti-pattern —
    an empty result and a failed collection are kept distinct.
    """
    refs = scope.searchable()
    project_refs = project_refs if project_refs is not None else set(scope.primary) | set(scope.cross_project)

    ranked_lists: list[list] = []
    degraded: list[tuple] = []
    per_collection: dict = {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(search_one, ref): ref for ref in refs}
        for future, ref in list(futures.items()):
            is_project = ref in project_refs
            try:
                hits = future.result(timeout=timeout)
            except FutureTimeout:
                if is_project:
                    raise
                degraded.append((ref, "timeout"))
                per_collection[ref] = 0
                continue
            except Exception as exc:  # noqa: BLE001 — isolate, but never swallow silently
                if is_project:
                    raise
                degraded.append((ref, f"error: {type(exc).__name__}"))
                per_collection[ref] = 0
                continue
            per_collection[ref] = len(hits)
            ranked_lists.append(hits)

    fused = reciprocal_rank_fusion(ranked_lists, key=key) if ranked_lists else []
    return FusedResults(hits=fused, degraded=degraded, per_collection=per_collection)


def search_collections(
    query: str,
    collection_names,
    *,
    client,
    encoder,
    top_k: int = 10,
    score_threshold: float = 0.3,
    project_refs: Optional[set] = None,
):
    """Search several Qdrant collections and RRF-fuse the results (v3 read path).

    The multi-collection counterpart to the single-collection :class:`Searcher`:
    encode once, query each collection, and fuse by reciprocal rank so
    non-comparable per-collection scores never distort the merge. Returns the
    top-``k`` fused points. This is what a per-project / project+frameworks
    search runs on once the collection-per-project model is live.
    """
    qvec = encoder.encode_query(query)
    qlist = qvec.tolist() if hasattr(qvec, "tolist") else list(qvec)
    over_fetch = top_k * 3 + 10

    def _search_one(coll):
        res = client.query_points(
            collection_name=coll, query=qlist, limit=over_fetch,
            score_threshold=score_threshold, with_payload=True,
        )
        return list(res.points)

    scope = ResolvedScope(
        client_profile_id="", resolution="explicit", primary=list(collection_names)
    )
    fused = fan_out_and_fuse(
        scope, _search_one, key=lambda p: p.id,
        project_refs=project_refs if project_refs is not None else set(collection_names),
    )
    return fused.hits[:top_k]


def search_by_strategy(
    settings,
    query: str,
    project_id: Optional[str] = None,
    *,
    registry=None,
    client,
    encoder,
    top_k: int = 10,
):
    """Dispatch search by ``settings.collection_strategy``; return ``List[SearchResult]``.

    ``"shared"`` (default) → the single-collection :class:`~ragtools.retrieval.searcher.Searcher`.
    ``"per_project"`` → the project's own collection via :func:`search_collections`,
    with points mapped to the SAME :class:`~ragtools.models.SearchResult` type, so
    callers are agnostic to the collection model. Per-project search is
    fail-closed: it requires a registry and a project scope.
    """
    strategy = getattr(settings, "collection_strategy", "shared")
    if strategy == "per_project":
        if registry is None:
            raise ValueError("collection_strategy='per_project' search requires a registry")
        if not project_id:
            from ragtools.retrieval.scope import ScopeUnresolvedError
            raise ScopeUnresolvedError("per_project search requires an explicit project")
        rec = registry.get(project_id)
        if rec is None:
            raise KeyError(f"no such project: {project_id!r}")
        from ragtools.retrieval.searcher import point_to_search_result
        points = search_collections(
            query, [rec.collection_name], client=client, encoder=encoder, top_k=top_k
        )
        return [point_to_search_result(p) for p in points]

    from ragtools.retrieval.searcher import Searcher
    return Searcher(client=client, encoder=encoder, settings=settings).search(
        query=query, project_id=project_id, top_k=top_k
    )
