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

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

logger = logging.getLogger("ragtools.collection_router")

STRATEGY_SHARED = "shared"
STRATEGY_PER_PROJECT = "per_project"
_STRATEGIES = (STRATEGY_SHARED, STRATEGY_PER_PROJECT)

#: What a collection IS. The same vocabulary :meth:`CollectionRouter.describe`
#: reports, so the inventory and a single result agree on the words.
KIND_PROJECT = "project"
KIND_FRAMEWORK = "framework"
KIND_SHARED = "shared"


@dataclass(frozen=True)
class CollectionIdentity:
    """What a collection is, and whose content a hit from it belongs to.

    ``kind`` answers *what store is this* — a project's own collection, a shared
    dependency corpus, or the legacy single collection. ``source`` answers
    *whose content is it*, in terms a reader can act on: a project id, or the
    corpus id of the dependency that answered.

    ``source`` is NEVER a raw ``proj_<uuid>``. That string names a store on
    disk; it is not something anyone can look up, open, or re-scope a search to.
    A search told the user their own code came from ``proj_9f2c…`` — which is
    both wrong and unusable.
    """

    name: str
    kind: str
    source: str = ""

    @property
    def scope(self) -> str:
        """The two-valued label carried on every :class:`~ragtools.models.SearchResult`.

        Everything that is not a vendored corpus is the user's own content, so
        the projection is deliberately total: an unrecognised collection reads
        as ``project`` rather than silently accusing a dependency.
        """
        return "framework" if self.kind == KIND_FRAMEWORK else "project"


def identity_from_name(name: str) -> CollectionIdentity:
    """Classify a collection by its NAME alone — the registry-less fallback.

    Used where no router is available (a bare :class:`~ragtools.retrieval.searcher.Searcher`).
    It can see the *kind* — the naming scheme is minted by
    :mod:`ragtools.identity` and is unambiguous — but not WHICH project a
    ``proj_<uuid>`` belongs to, because that mapping lives in the registry. In
    that case it reports no source at all rather than handing back the store
    name: a wrong-but-plausible identifier is worse than an absent one.
    """
    from ragtools.identity import (
        is_framework_collection_name, is_project_collection_name,
    )

    if is_framework_collection_name(name):
        # A corpus name carries the framework's own slug, so it identifies WHICH
        # dependency answered even without the framework registry.
        return CollectionIdentity(name, KIND_FRAMEWORK, name)
    if is_project_collection_name(name):
        return CollectionIdentity(name, KIND_PROJECT, "")
    return CollectionIdentity(name, KIND_SHARED, "")


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

    def identify(self, names: Iterable[str]) -> dict[str, CollectionIdentity]:
        """What each of ``names`` IS — the one place that answers it.

        POSITION IS NOT AN ANSWER. The searcher used to read the scope of a hit
        off its collection's index in the routed list — "the first one is the
        project's own, everything after it is a framework it vendors" — which is
        true only for the single-project shape the router happens to emit most
        often. Search three projects explicitly and the second and third were
        reported as vendored dependencies, with the raw ``proj_<uuid>`` store
        name shown as the dependency's identity. The list order is a ranking
        preference (see :meth:`read_collections`); it was never a claim about
        what the collections are.

        One registry read for the whole batch, so labelling N collections costs
        one query rather than N.
        """
        wanted = [n for n in dict.fromkeys(names) if n]
        if self._strategy == STRATEGY_SHARED:
            # One collection holds every project; whose a given chunk is comes
            # from its payload, not from the store. Saying nothing here is the
            # honest answer, and keeps the legacy layout byte-identical.
            return {n: CollectionIdentity(n, KIND_SHARED, "") for n in wanted}

        owners = self._project_ids_by_collection()
        out: dict[str, CollectionIdentity] = {}
        for name in wanted:
            project_id = owners.get(name)
            if project_id:
                out[name] = CollectionIdentity(name, KIND_PROJECT, project_id)
            else:
                out[name] = identity_from_name(name)
        return out

    def _project_ids_by_collection(self) -> dict[str, str]:
        """Reverse of the registry's project → collection mapping.

        Archived projects included: their vectors are still on disk and still
        answer a scoped read, so a hit from one must still be able to say whose
        it is.

        Never fatal — a registry fault degrades the LABEL, and a label is not
        worth failing a search over. The caller falls back to the naming scheme.
        """
        try:
            return {rec.collection_name: rec.project_id
                    for rec in self._registry.list(include_archived=True)}
        except Exception:  # noqa: BLE001 — see docstring
            logger.exception("could not read the project registry for labelling")
            return {}

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

    def display_name(self) -> str:
        """One short string naming what this installation's index actually is.

        ``/health``, the tray, the diagnostics header, ``/api/config`` and the
        MCP ``get_config`` tool each need a single value, and every one of them
        printed ``settings.collection_name``. Under ``per_project`` that names
        nothing: the installed v3.4 machine held 15 ``proj_<uuid>`` collections
        and no ``markdown_kb``, so the service reported a collection that does
        not exist as the knowledge base's identity. Say what is there instead.
        The per-installation discriminator two services on one machine are told
        apart by is ``service_id`` / ``data_dir`` on ``/identity``, not a
        collection name.

        COSMETIC ONLY. A caller that needs a collection to QUERY asks
        :meth:`read_collections` / :meth:`all_collections`; in per-project mode
        this returns prose and must never reach Qdrant.
        """
        if self._strategy == STRATEGY_SHARED:
            return self._settings.collection_name
        try:
            count = len(self.all_collections())
        except Exception:  # noqa: BLE001 — a registry fault must not break the
            # liveness probe that carries this label.
            return STRATEGY_PER_PROJECT
        return f"{count} collection{'' if count == 1 else 's'} ({STRATEGY_PER_PROJECT})"

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
    # BEFORE the sync, never after. If registry.db was lost, this constructor
    # just recreated it EMPTY and the sync below would mint a fresh uuid4 per
    # project — N brand-new empty collections beside the N that still hold the
    # data. Arming the hold here is what makes that refusal possible at all;
    # asking afterwards would only describe the damage.
    _guard_registry(settings, registry)
    # Mirror the live TOML projects. Idempotent, and it preserves UUID +
    # collection across rename and path move.
    sync_projects_from_config(settings.projects, registry)
    return (
        CollectionRouter(settings, registry=registry, framework_registry=frameworks),
        registry,
        frameworks,
    )


def _guard_registry(settings, registry) -> None:
    """Arm or clear the registry's write hold from the recorded mapping (R06).

    Reads only. The state DB is opened ONLY if it already exists: a factory must
    not bring a database into being as a side effect, and a state DB that does
    not exist records no mapping — which is ``unknown``, which arms nothing.

    Never fatal. This decides whether *identity-creating writes* are safe, and
    failing to decide must not stop the router from being built; the hold simply
    stays as it was.
    """
    from pathlib import Path

    state_db = getattr(settings, "state_db", None)
    if not state_db or not Path(state_db).exists():
        return
    try:
        from ragtools import registry_integrity
        from ragtools.indexing.state import IndexState

        state = IndexState(str(state_db))
        try:
            registry_integrity.reconcile_startup(state, registry)
        finally:
            state.close()
    except Exception:  # noqa: BLE001 — see docstring
        logger.exception("registry-integrity guard could not run")


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
