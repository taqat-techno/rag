"""Which collection does this call touch? — the one place that decides.

Before this module, ``settings.collection_name`` was read at 38 sites across 11
files (owner, indexer, watcher, searcher, codegraph, map, MCP, routes, pages,
CLI, job handlers). "Which collection" was therefore a decision re-made and
re-hardcoded everywhere, which is why the collection-per-project machinery in
:mod:`ragtools.registry` and :mod:`ragtools.identity` could exist, pass its
tests, and still never reach the running service.

The router turns that into ONE decision with two strategies:

``shared`` (default)
    Every answer is ``settings.collection_name``. The v2 single-collection model
    is preserved *by construction* rather than by a parallel code path — there
    is no second pipeline to drift.

``per_project``
    A project's collection comes from :class:`ragtools.registry.ProjectRegistry`,
    derived from the project's immutable UUID, so it survives both rename and
    path move. Reads additionally span the framework corpora the project is
    linked to, so a vendored framework is indexed once per build identity and
    *referenced* by every project that uses it.

Security invariant
------------------
In ``per_project`` an unknown project raises :class:`UnknownProject`. It must
never fall back to the shared collection: that would answer one project's query
out of another project's vectors. Read-scoping is the security boundary here in
a way it was not under a payload filter, so the failure mode is refusal.

Plan: docs/planning/RAG_COLLECTION_ARCHITECTURE_IMPLEMENTATION_PLAN.md (W1)
"""

from __future__ import annotations

from typing import Iterable, Optional

STRATEGY_SHARED = "shared"
STRATEGY_PER_PROJECT = "per_project"
_STRATEGIES = (STRATEGY_SHARED, STRATEGY_PER_PROJECT)


class UnknownProject(KeyError):
    """A project id has no registry entry, so no collection can be resolved.

    Deliberately fatal: the alternative — quietly using the shared collection —
    is a cross-project read.
    """


class CollectionRouter:
    """Resolve project ids to Qdrant collection names.

    Constructed once by :class:`ragtools.service.owner.QdrantOwner` and passed
    down; there is no module-level singleton, so tests and the migration can
    hold two routers with different strategies at the same time.
    """

    def __init__(self, settings, registry=None, framework_registry=None):
        strategy = (getattr(settings, "collection_strategy", STRATEGY_SHARED)
                    or STRATEGY_SHARED).strip().lower()
        if strategy not in _STRATEGIES:
            raise ValueError(
                f"unknown collection_strategy {strategy!r}; expected one of "
                f"{' or '.join(_STRATEGIES)}"
            )
        if strategy == STRATEGY_PER_PROJECT and registry is None:
            raise ValueError(
                "collection_strategy='per_project' requires a project registry"
            )
        self._settings = settings
        self._strategy = strategy
        self._registry = registry
        self._frameworks = framework_registry

    # -- introspection ---------------------------------------------------

    @property
    def strategy(self) -> str:
        return self._strategy

    @property
    def is_per_project(self) -> bool:
        return self._strategy == STRATEGY_PER_PROJECT

    @property
    def shared_collection(self) -> str:
        """The v2 collection name. Still meaningful in per-project mode as the
        legacy collection retained until the operator drops it explicitly."""
        return self._settings.collection_name

    # -- resolution ------------------------------------------------------

    def write_collection(self, project_id: Optional[str]) -> str:
        """The single collection a project's chunks are written to."""
        if self._strategy == STRATEGY_SHARED:
            return self._settings.collection_name
        return self._record(project_id).collection_name

    def read_collections(
        self,
        project_id: Optional[str] = None,
        project_ids: Optional[Iterable[str]] = None,
        *,
        include_frameworks: bool = True,
    ) -> list[str]:
        """Every collection a scoped query must search, own collection first.

        Ordering is meaningful: a project's own content precedes its framework
        references, so a merge that breaks score ties by position prefers the
        project's own code — the same priority the reranker already encodes.
        """
        if self._strategy == STRATEGY_SHARED:
            return [self._settings.collection_name]

        ids = self._requested_ids(project_id, project_ids)
        if ids is None:
            # Unscoped read (status, maps, admin aggregates). Fail-closed search
            # scoping is enforced by retrieval/scope.py, not here.
            return self.active_collections()

        own: list[str] = []
        frameworks: list[str] = []
        for pid in ids:
            rec = self._record(pid)
            own.append(rec.collection_name)
            if include_frameworks:
                frameworks.extend(self._framework_collections(rec.uuid))
        return _dedupe(own + frameworks)

    def active_collections(self) -> list[str]:
        """Collections of non-archived projects, plus their frameworks."""
        if self._strategy == STRATEGY_SHARED:
            return [self._settings.collection_name]
        names: list[str] = []
        frameworks: list[str] = []
        for rec in self._registry.list(include_archived=False):
            names.append(rec.collection_name)
            frameworks.extend(self._framework_collections(rec.uuid))
        return _dedupe(names + frameworks)

    def all_collections(self) -> list[str]:
        """Every collection holding points, archived projects included.

        ``archive`` keeps both the row and the vectors (registry verb 1), so
        status and point counts must still see them — otherwise archiving a
        project would appear to delete data that is in fact still on disk.
        """
        if self._strategy == STRATEGY_SHARED:
            return [self._settings.collection_name]
        names: list[str] = []
        frameworks: list[str] = []
        for rec in self._registry.list(include_archived=True):
            names.append(rec.collection_name)
            frameworks.extend(self._framework_collections(rec.uuid))
        return _dedupe(names + frameworks)

    def ensure(self, client, project_id: Optional[str], dimension: int) -> str:
        """Create the project's collection if absent; return its name."""
        from ragtools.indexing.indexer import ensure_collection

        name = self.write_collection(project_id)
        ensure_collection(client, name, dimension)
        return name

    def describe(self) -> list[dict]:
        """Collection inventory for /api/status, diagnostics and MCP."""
        if self._strategy == STRATEGY_SHARED:
            return [{"name": self._settings.collection_name, "kind": "shared",
                     "project": None}]
        out: list[dict] = []
        seen: set[str] = set()
        for rec in self._registry.list(include_archived=True):
            out.append({"name": rec.collection_name, "kind": "project",
                        "project": rec.project_id, "archived": rec.archived})
            seen.add(rec.collection_name)
            for fw in self._framework_collections(rec.uuid):
                if fw in seen:
                    continue
                seen.add(fw)
                out.append({"name": fw, "kind": "framework", "project": None})
        return out

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        """Release the registry connections this router owns. Idempotent.

        Callers that build a router for a one-shot operation (the CLI indexer,
        the watcher) must close it: the registries hold SQLite handles, and on
        Windows an open handle keeps the .db file locked, so a leaked router
        leaves an undeletable file behind.
        """
        for registry in (self._registry, self._frameworks):
            if registry is not None:
                try:
                    registry.close()
                except Exception:  # noqa: BLE001
                    pass
        self._registry = None
        self._frameworks = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- helpers ---------------------------------------------------------

    def _record(self, project_id: Optional[str]):
        if not project_id:
            raise UnknownProject(
                "a project id is required to resolve a collection in "
                "per-project mode"
            )
        rec = self._registry.get(project_id)
        if rec is None:
            raise UnknownProject(
                f"no registered project {project_id!r}; refusing to fall back to "
                f"the shared collection (that would read another project's data)"
            )
        return rec

    def _framework_collections(self, project_uuid: str) -> list[str]:
        if self._frameworks is None:
            return []
        try:
            return list(self._frameworks.framework_collections_for(project_uuid))
        except Exception:  # noqa: BLE001 — a framework registry fault must not
            # take down search of the project's own content.
            return []

    @staticmethod
    def _requested_ids(project_id, project_ids) -> Optional[list[str]]:
        if project_ids is not None:
            ids = [p for p in project_ids if p]
            return ids or None
        if project_id:
            return [project_id]
        return None


def build_router(settings):
    """Construct a router plus the registries it needs, from settings alone.

    One factory for every entry point — the service owner, the CLI watcher, the
    migration — so "how do I get a router" is not re-implemented (and re-drifted)
    per caller.

    In ``shared`` mode nothing is opened and no SQLite file is created: an
    install that never elects per-project pays nothing for the feature's
    existence. Returns ``(router, project_registry, framework_registry)`` where
    the registries are ``None`` in shared mode.
    """
    strategy = (getattr(settings, "collection_strategy", STRATEGY_SHARED)
                or STRATEGY_SHARED).strip().lower()
    if strategy != STRATEGY_PER_PROJECT:
        return CollectionRouter(settings), None, None

    from pathlib import Path

    from ragtools.registry import (
        FrameworkRegistry, ProjectRegistry, sync_projects_from_config,
    )

    data_dir = Path(settings.data_dir)
    registry = ProjectRegistry(str(data_dir / "registry.db"))
    frameworks = FrameworkRegistry(str(data_dir / "frameworks.db"))
    # Mirror the live TOML projects. Idempotent, and it preserves UUID +
    # collection across rename and path move.
    sync_projects_from_config(settings.projects, registry)
    return (
        CollectionRouter(settings, registry=registry, framework_registry=frameworks),
        registry,
        frameworks,
    )


def _dedupe(names: Iterable[str]) -> list[str]:
    """Order-preserving dedupe — two projects linking one framework build must
    search that corpus once, not twice."""
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out
