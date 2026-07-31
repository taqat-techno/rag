"""QdrantOwner — sole owner of Qdrant client and Encoder.

All search and indexing operations go through this singleton.
Protected by threading.RLock to serialize Qdrant + encoder access.
"""

import logging
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from qdrant_client import QdrantClient

from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.retrieval.scope import resolve_scope
from ragtools.ignore import IgnoreRules
from ragtools.chunking.dispatch import chunk_file
from ragtools.indexing.indexer import (
    ensure_collection,
    delete_file_points,
    delete_files_points,
    chunks_to_points,
    upsert_points,
    index_file,
    apply_source_class_and_redaction,
)
from ragtools.indexing.scanner import (
    get_project_relative_path,
    scan_configured_projects,
)
from ragtools.indexing.state import IndexState
from ragtools.models import SearchResult
from ragtools.retrieval.formatter import format_context
from ragtools.retrieval.searcher import Searcher

# Batch size for windowed lock release during indexing
_INDEX_BATCH_SIZE = 30

# Qdrant local-mode scale thresholds. Qdrant's own guidance recommends
# against local mode above 20,000 points. We warn earlier (at 15,000) so
# users see the signal before it bites.
_QDRANT_LOCAL_SOFT_WARN = 15_000
_QDRANT_LOCAL_HARD_WARN = 20_000

logger = logging.getLogger("ragtools.service")

#: What :meth:`QdrantOwner.get_status_projects` may say about one project.
#: Exactly one applies at a time and each names a DIFFERENT remedy — which is
#: the entire point. The dashboard used to render one string, "Not indexed yet",
#: for a project that was scanned and legitimately had nothing, one whose folder
#: had been moved, one the user had disabled, and one whose rebuild had failed.
#: Four causes, four remedies, one string.
PROJECT_STATE_DISABLED = "disabled"                      # off on purpose
PROJECT_STATE_PATH_MISSING = "path_missing"              # fix the path
PROJECT_STATE_NEVER_INDEXED = "never_indexed"            # index it
PROJECT_STATE_INDEXED = "indexed"                        # nothing to do
PROJECT_STATE_INDEXED_STALE = "indexed_stale"            # re-run the index
PROJECT_STATE_NO_ELIGIBLE_FILES = "no_eligible_files"    # widen the Mode
PROJECT_STATE_FAILED = "failed"                          # read the rebuild error
PROJECT_STATE_STORAGE_UNAVAILABLE = "storage_unavailable"  # fix the engine
PROJECT_STATE_DRIFTED = "drifted"                        # rebuild THIS project

PROJECT_STATES = (
    PROJECT_STATE_DISABLED,
    PROJECT_STATE_PATH_MISSING,
    PROJECT_STATE_NEVER_INDEXED,
    PROJECT_STATE_INDEXED,
    PROJECT_STATE_INDEXED_STALE,
    PROJECT_STATE_NO_ELIGIBLE_FILES,
    PROJECT_STATE_FAILED,
    PROJECT_STATE_STORAGE_UNAVAILABLE,
    PROJECT_STATE_DRIFTED,
)

#: Shape returned when status is requested while indexing owns the lock and no
#: snapshot has been taken yet. Never blocks the caller.
_EMPTY_STATUS = {
    "points_count": 0, "collection_name": "", "total_files": 0, "total_chunks": 0,
    "projects": [], "last_indexed": None,
    # The project counts, named apart. `len(projects)` answers "how many have at
    # least one indexed FILE" and nothing else; rendering it under the bare word
    # "projects" above a table of the CONFIGURED ones is the 14-vs-15.
    "projects_configured": 0, "projects_enabled": 0, "projects_indexed": 0,
    "projects_searchable": None,
    "collection_strategy": "shared", "collections": [],
    "storage": {"backend": "embedded", "engine_version": None, "hnsw": False,
                "payload_indexes": False, "concurrent_readers": False},
    "scale": {"level": "unknown", "points_count": 0, "message": ""},
    "freshness": {"level": "unknown", "message": ""},
}


def governing_collection(per_collection) -> tuple[int, str, int]:
    """The count the engine's ceiling actually applies to.

    Returns ``(points, collection_name, collection_count)``.

    **The limit is per collection, not per index.** In embedded mode every
    collection is its own brute-force scan, so twenty-five collections of two
    thousand points each are twenty-five fast scans — not one slow one. Summing
    them and comparing the total against the ceiling is simply the wrong
    arithmetic, and it fails in the direction that matters: it would report
    "over" permanently on exactly the per-project layout that fixes the problem,
    training the operator to ignore the warning that was right.

    The converse is equally wrong. A single project that vendors a framework can
    exceed the ceiling alone while the average across collections looks
    comfortable, so the maximum is the honest summary — not the mean, and not
    the total.
    """
    if not per_collection:
        return 0, "", 0
    worst = max(per_collection, key=lambda entry: entry.get("points", 0) or 0)
    return int(worst.get("points", 0) or 0), str(worst.get("name", "")), len(per_collection)


def compute_scale_warning(points_count: int, capabilities=None, *,
                          collection: str = "", collection_count: int = 1) -> dict:
    """Return a structured scale-warning record for a given collection size.

    Levels:
      - ok        (< 15,000 points): no action required
      - approaching (15,000 - 19,999): user should start pruning or plan migration
      - over      (>= 20,000): past Qdrant's own local-mode recommendation

    **The ceiling belongs to the engine, not to the data.** The 20,000 figure is
    a property of Qdrant's local mode — a pure-Python brute-force scan with no
    HNSW. On a real server (managed or external) that limit does not exist, so
    passing ``capabilities`` with ``hnsw=True`` returns ``ok`` at any size: the
    warning describes a limitation that has been removed, and repeating it there
    would train the operator to ignore it.

    ``capabilities`` is optional so every existing caller keeps its behaviour;
    omitted means "assume the embedded engine", which is the safe default.

    The record is attached to /api/status so the admin panel and `rag doctor`
    can surface the signal. Pure function — no side effects, easy to unit-test.
    """
    if capabilities is not None and getattr(capabilities, "hnsw", False):
        return {
            "level": "ok",
            "points_count": points_count,
            "soft_limit": None,
            "hard_limit": None,
            "message": "",
            "engine": "server",
        }

    # Name the collection when there is more than one, so "which one?" is
    # answerable from the message itself. With a single collection it is noise.
    subject = (f"Collection '{collection}'" if collection and collection_count > 1
               else "Collection")

    if points_count >= _QDRANT_LOCAL_HARD_WARN:
        level = "over"
        message = (
            f"{subject} has {points_count:,} points, which is above Qdrant's "
            f"recommended local-mode limit of {_QDRANT_LOCAL_HARD_WARN:,}. "
            "Search latency and memory use may degrade. "
            # Every action named here must be one the user can actually take.
            # This previously advised "migrating Qdrant to server mode" — which
            # no CLI command, API field or admin-panel control could do, because
            # `storage_backend` had no setter anywhere. Advice that cannot be
            # followed reads as a broken product rather than a warning.
            "Reduce it with ignore rules (`rag ignore add`), or move this "
            "install to a real engine with `rag storage backend managed`."
        )
    elif points_count >= _QDRANT_LOCAL_SOFT_WARN:
        level = "approaching"
        message = (
            f"{subject} has {points_count:,} points, approaching the local-mode "
            f"limit of {_QDRANT_LOCAL_HARD_WARN:,}. Review ignore_patterns "
            "for large generated files and consider archiving completed projects."
        )
    else:
        level = "ok"
        message = ""

    return {
        "level": level,
        "points_count": points_count,
        "soft_limit": _QDRANT_LOCAL_SOFT_WARN,
        "hard_limit": _QDRANT_LOCAL_HARD_WARN,
        "message": message,
    }


#: The states an index can actually be in, as one word the UI can switch on.
#:
#: Derived rather than stored, because every input is already known and a stored
#: copy is one more thing to get out of date. The point is that "empty" and
#: "stale" and "rebuilding" have DIFFERENT remedies and were all rendering as a
#: chunk count from a database that described the previous store.
AVAILABILITY_READY = "ready"
AVAILABILITY_EMPTY = "empty"
AVAILABILITY_REBUILDING = "rebuilding"
AVAILABILITY_BLOCKED = "blocked"
AVAILABILITY_PARTIAL = "partial_unavailable"
AVAILABILITY_STALE = "stale_searchable"
AVAILABILITY_STORAGE_DOWN = "storage_unavailable"


def _availability(live_points: int | None, summary: dict, migration: dict | None,
                  freshness_level: str = "") -> str:
    """One word for "can this index answer a question right now?".

    Ordering matters: a migration in flight explains an empty store, and saying
    "empty" while a rebuild is running would tell the user their data is gone at
    the one moment it is merely absent.
    """
    if live_points is None:
        return AVAILABILITY_STORAGE_DOWN
    if migration:
        if migration.get("stalled"):
            return AVAILABILITY_BLOCKED
        if not migration.get("done"):
            return AVAILABILITY_REBUILDING
        return AVAILABILITY_PARTIAL if live_points else AVAILABILITY_REBUILDING
    if live_points:
        # Searchable, but the content may have moved on. A distinct state
        # because it has a distinct remedy — run an index, not a rebuild.
        return (AVAILABILITY_STALE if freshness_level == "stale"
                else AVAILABILITY_READY)
    # Nothing live. Whether that is "you have not indexed yet" or "your index
    # vanished" is decided by whether anything was EVER recorded.
    return AVAILABILITY_PARTIAL if summary.get("total_chunks") else AVAILABILITY_EMPTY


def compute_index_freshness(last_indexed, stale_after_hours: float = 24, now=None) -> dict:
    """Classify index freshness from a ``last_indexed`` timestamp. Pure function.

    Levels:
      - ``never``   : no index has ever been built (last_indexed is falsy)
      - ``fresh``   : age <= stale_after_hours
      - ``stale``   : age >  stale_after_hours (results may be out of date)
      - ``unknown`` : last_indexed could not be parsed

    Mirrors :func:`compute_scale_warning` so /api/status, /health and
    ``rag doctor`` can surface a staleness signal consistently. Easy to unit-test
    (inject ``now``); no side effects.
    """
    from datetime import datetime as _dt

    now = now or _dt.now()
    base = {
        "last_indexed": last_indexed,
        "age_seconds": None,
        "stale_after_hours": stale_after_hours,
    }
    if not last_indexed:
        return {**base, "level": "never", "message": "Index has never been built."}
    try:
        ts = _dt.fromisoformat(str(last_indexed))
    except (ValueError, TypeError):
        return {**base, "level": "unknown",
                "message": "last_indexed timestamp could not be parsed."}
    # Tolerate tz-aware vs naive mismatch (state stores naive local times).
    if (ts.tzinfo is None) != (now.tzinfo is None):
        ts = ts.replace(tzinfo=None)
        now = now.replace(tzinfo=None)
    age = max((now - ts).total_seconds(), 0.0)
    if age > stale_after_hours * 3600:
        return {**base, "age_seconds": age, "level": "stale",
                "message": (f"Index last updated ~{age / 3600:.1f}h ago, beyond the "
                            f"{stale_after_hours}h freshness threshold. If the watcher "
                            "is not running, search results may be out of date.")}
    return {**base, "age_seconds": age, "level": "fresh", "message": ""}




class StorageWentAway(RuntimeError):
    """The storage engine disappeared while an indexing run was in flight.

    Distinct from an ordinary write failure on purpose: it means "stop, the
    destination is gone" rather than "this file failed". Raised at a progress
    boundary, so at most one committed window is lost.
    """


class QdrantOwner:
    """Holds Qdrant client + Encoder, protected by RLock.

    Args:
        settings: Application settings.
        client: Optional pre-created client (for testing with in-memory).
    """

    def __init__(self, settings: Settings, client: QdrantClient | None = None):
        self._lock = threading.RLock()
        # Last good reads, served without the lock while indexing runs.
        self._status_snapshot: dict | None = None
        self._projects_snapshot: list | None = None
        self._status_projects_snapshot: list | None = None
        #: ``(taken_at, {project_id: eligible_file_count})``. See
        #: :meth:`_explain_unindexed` for why the scan behind it is cached.
        self._eligibility_cache: tuple[float, dict] | None = None
        self._settings = settings
        self._client = client or settings.get_qdrant_client()
        self._encoder = Encoder(settings.embedding_model)

        # Global ignore rules (per-project merging happens at scan time)
        self._ignore_rules = IgnoreRules(
            content_root=".",
            global_patterns=settings.ignore_patterns,
            use_ragignore=settings.use_ragignore_files,
            secret_allowlist=settings.secret_allowlist,
        )

        # Which collection(s) a call touches is decided in ONE place. In
        # 'shared' mode the router answers with settings.collection_name for
        # everything, so the v2 single-collection behaviour is preserved by
        # construction rather than by a second pipeline.
        # One indexing run at a time, process-wide.
        #
        # The job engine serialises index JOBS, but the watcher thread calls
        # run_incremental_index directly and bypasses that queue. Normally
        # harmless (its runs skip everything and finish in milliseconds), it
        # became severe once a storage/layout change forced a real re-index:
        # the watcher and the job both re-chunked all 38,286 files at once,
        # pegged a core, and starved uvicorn's event loop so completely that
        # every endpoint — even /identity — timed out.
        #
        # Non-blocking: a second caller is TOLD it was skipped rather than
        # queueing up behind a run that may take half an hour.
        self._index_mutex = threading.Lock()
        #: Heartbeat for the run currently holding the mutex, or None when idle.
        #: Written only under the mutex, read without it — a waiter must be able
        #: to ask "is the holder alive" precisely because it cannot take the lock.
        self._index_run: dict | None = None
        #: "Is storage gone?" — set by the service lifespan; see set_storage_gate.
        self._storage_gate = None
        self._registry = None
        self._frameworks = None
        self._capabilities = None
        self._router = self._build_router(settings)
        # Is the registry we just opened the one this index was built against?
        # Asked BEFORE anything writes: a registry that cannot be vouched for
        # must not mint identities or swap pointers, and the answer is also what
        # /health reports. Never fatal — see `_reconcile_registry_integrity`.
        self._reconcile_registry_integrity()
        self._ensured_collections: set[str] = set()

        for name in self._router.all_collections():
            ensure_collection(self._client, name, self._encoder.dimension)
            self._ensured_collections.add(name)
        logger.info("QdrantOwner initialized (model=%s, strategy=%s, collections=%d)",
                    settings.embedding_model, self._router.strategy,
                    len(self._router.all_collections()))
        from ragtools.service.activity import log_activity
        log_activity("info", "service", f"Engine initialized (model={settings.embedding_model})")

        # Validate configured project paths
        for p in settings.enabled_projects:
            if not Path(p.path).exists():
                logger.warning("Project '%s' path does not exist: %s", p.id, p.path)
                log_activity("warning", "config", f"Project '{p.id}' path missing: {p.path}")

    def _build_router(self, settings):
        """Construct the collection router and the registries it needs."""
        from ragtools.collection_router import build_router

        router, self._registry, self._frameworks = build_router(settings)
        return router

    @property
    def router(self):
        """The collection router — how every caller learns which collection(s)
        to touch."""
        return self._router

    @property
    def indexing(self) -> bool:
        """True while an indexing run is in progress anywhere in this process."""
        return self._index_mutex.locked()

    def index_activity(self) -> dict | None:
        """What the running index has done most recently, or ``None`` if idle.

        The point is to let a *waiter* distinguish a slow run from a dead one.
        :attr:`indexing` answers "is the mutex held", which cannot tell those
        apart — and a caller that cannot tell them apart has to guess, which is
        how a queued job came to announce that a perfectly healthy startup sync
        was "stuck" after waiting a fixed 900 seconds for it.

        ``age`` is seconds since the run last reported progress. Silence is the
        signal that matters; elapsed time is not, because a legitimate first
        index of a large corpus runs for hours.
        """
        run = self._index_run
        if run is None:
            return None
        return {**run, "age": max(0.0, time.time() - run["last_tick"])}

    def _begin_index_run(self, what: str) -> None:
        self._index_run = {"what": what, "started_at": time.time(),
                           "last_tick": time.time(),
                           "done": 0, "total": 0, "phase": "starting"}

    def set_storage_gate(self, gate) -> None:
        """Register "is storage gone?" — checked at every progress boundary.

        A callable returning a reason string when storage is unusable, or "".
        Registered by the service lifespan so the owner stays free of engine
        policy.
        """
        self._storage_gate = gate

    def _beat(self, done, total, phase) -> None:
        """Record that the running index is alive and where it has got to.

        Called from the single progress funnel of both indexers, so it cannot
        drift out of sync with the work: anything that reports progress to a
        caller reports liveness here by the same call.

        **It is also where a run notices storage has gone away.** That makes this
        the cancellation boundary the class already documents — progress is
        reported between files and between committed windows, so stopping here
        loses at most one window. Without it, an engine that died mid-migration
        was rediscovered one exception at a time while the indexer went on
        scanning, chunking and embedding a project whose every write would be
        refused. That is the CPU the v3.2.0 incident burned for ten minutes.
        """
        run = self._index_run
        if run is None:  # a run that beats outside its own context; ignore
            return
        run["last_tick"] = time.time()
        run["done"] = done or 0
        run["total"] = total or 0
        run["phase"] = phase

        gate = self._storage_gate
        if gate is not None:
            try:
                reason = gate()
            except Exception:  # noqa: BLE001 — a gate we cannot ask is not a stop
                reason = ""
            if reason:
                raise StorageWentAway(reason)

    @contextmanager
    def _exclusive_index(self, what: str):
        """Hold the index mutex, or yield ``None`` if a run is already going.

        Callers must treat ``None`` as "skipped, not failed": a watcher tick
        that coincides with a running re-index has nothing useful to add, and
        the changes it saw will be picked up by the next tick anyway.
        """
        if not self._index_mutex.acquire(blocking=False):
            logger.info("%s skipped: an indexing run is already in progress", what)
            yield None
            return
        self._begin_index_run(what)
        try:
            yield True
        finally:
            # Cleared before the mutex is released, so a waiter that wakes on
            # the release never reads the finished run's heartbeat as a live one.
            self._index_run = None
            self._index_mutex.release()

    # ------------------------------------------------------------------
    # Streaming index core
    #
    # Both indexers used to be two-phase: chunk EVERY file into a `pending`
    # list, then encode/upsert in windows. Peak memory was therefore O(corpus)
    # — 2.46 GB for 38,286 files — and nothing was durable until the whole
    # corpus had been chunked (the point count sat flat for ~15 minutes).
    #
    # Now one window of files is chunked, encoded, written and dropped before
    # the next is read. Peak memory is O(window) whatever the corpus size, and
    # an interruption costs at most one window instead of everything.
    # ------------------------------------------------------------------

    def _flush_window(self, window, stats, indexed_key, emptied) -> None:
        """Chunk, encode, write and record one window of files.

        ``window`` is ``[(pid, relative_path, file_hash, file_path), ...]``.
        Chunking happens outside the lock (pure I/O + CPU); only the encode,
        the Qdrant writes and the state update take it.
        """
        prepared = []
        for pid, relative_path, file_hash, file_path in window:
            chunks = chunk_file(
                file_path=file_path,
                project_id=pid,
                relative_path=relative_path,
                chunk_size=self._settings.chunk_size,
                chunk_overlap=self._settings.chunk_overlap,
            )
            if chunks:
                # Redaction + source_class on the SERVICE path (this bypasses
                # index_file): secrets never reach the vector or the payload,
                # and every point gets a real source class.
                apply_source_class_and_redaction(chunks, relative_path)
                prepared.append((pid, relative_path, file_hash, chunks))
            else:
                # Changed, but yields nothing indexable. The caller decides
                # whether that means "drop its old vectors" or "ignore".
                emptied.append((pid, relative_path, file_hash))

        if not prepared:
            return

        with self._lock:
            state = IndexState(self._settings.state_db)
            try:
                all_chunks = []
                chunk_map = []          # (start, count, pid, rel_path, hash)
                for pid, relative_path, file_hash, chunks in prepared:
                    chunk_map.append((len(all_chunks), len(chunks), pid,
                                      relative_path, file_hash))
                    all_chunks.extend(chunks)

                embeddings = self._encoder.encode_batch([c.text for c in all_chunks])

                by_collection: dict[str, list] = {}
                stale: dict[str, list[str]] = {}
                for start, count, pid, relative_path, file_hash in chunk_map:
                    coll = self._write_collection(pid)
                    stale.setdefault(coll, []).append(relative_path)
                    points = chunks_to_points(
                        all_chunks[start : start + count],
                        embeddings[start : start + count],
                        file_hash,
                    )
                    by_collection.setdefault(coll, []).extend(points)

                # Stale chunks go first and in ONE request per collection: a
                # file that shrank must not keep its higher-index chunks, and
                # per-file deletes exhausted the ephemeral port range.
                for collection, paths in stale.items():
                    delete_files_points(self._client, collection, paths)
                for collection, points in by_collection.items():
                    upsert_points(self._client, collection, points)

                # State is written only after the vectors land, so an
                # interrupted run re-does this window rather than skipping it.
                for _start, count, pid, relative_path, file_hash in chunk_map:
                    state.update(relative_path, pid, file_hash, count)
                    stats[indexed_key] += 1
                    stats["chunks_indexed"] += count
                    stats["projects"].add(pid)
                state.commit()
            finally:
                state.close()

    def _stream_index(self, work, total, tick, stats, indexed_key) -> list:
        """Drive :meth:`_flush_window` over ``work`` in bounded windows.

        ``work`` may be a generator — files are hashed and read lazily, one
        window at a time. Returns the files that produced no chunks.
        """
        emptied: list[tuple[str, str, str]] = []
        window: list = []
        for item in work:
            window.append(item)
            if len(window) >= _INDEX_BATCH_SIZE:
                self._flush_window(window, stats, indexed_key, emptied)
                window = []
                # Safe boundary: this window is committed. Report, allow cancel.
                tick(stats[indexed_key], total, "embed")
        if window:
            self._flush_window(window, stats, indexed_key, emptied)
            tick(stats[indexed_key], total, "embed")
        return emptied

    def sync_frameworks(self, progress=None, refresh: bool = False) -> list[dict]:
        """Register, index and link the framework corpora projects declare.

        Driven by ``ProjectConfig.dependency_paths`` — a field that existed and
        was read by nothing. Declaring a path says "this is a shared dependency,
        not my code". Three things then have to be true together, and doing only
        some of them is worse than doing none:

        1. the corpus is registered once per BUILD identity, so two projects
           vendoring the same Odoo build share ONE collection;
        2. the corpus is **indexed into that collection** — the scanner already
           excluded it from the project scan, so without this step declaring a
           dependency would delete content from search entirely;
        3. the project's own collection is **purged** of those files, so
           adopting a dependency on an already-indexed project does not leave a
           duplicate copy behind.

        Idempotent, and cheap when nothing changed: a corpus that some project
        already links is reused rather than re-imported (``refresh=True`` forces
        a fresh import when the framework's files have actually changed).

        A no-op in shared mode and when nothing is declared, so an existing
        install cannot change until someone opts in.
        """
        if not self._router.is_per_project or self._frameworks is None:
            return []

        from ragtools.frameworks import describe_dependency, resolve_dependency_roots

        def _tick(done, total, phase):
            if progress is not None:
                progress(done, total, phase)

        results: list[dict] = []
        released: list[dict] = []
        for project in self._settings.projects:
            record = self._registry.get(project.id) if self._registry else None
            if record is None:
                continue
            # Catalog links first, legacy raw paths second, de-duplicated —
            # so a config part-way through the migration resolves to exactly
            # the same set and no edit can transiently drop a root.
            declared = self._settings.dependency_paths_for(project)
            # Collections this project SHOULD link after this run. Built as we
            # go, then diffed against what it actually links — that diff is
            # what makes removing a dependency work (see _release_frameworks).
            expected: set[str] = set()
            if not declared:
                released.extend(self._release_frameworks(project, record, expected))
                continue

            roots, problems = resolve_dependency_roots(project.path, declared)
            for problem in problems:
                logger.warning("Project %s dependency %s", project.id, problem)
                from ragtools.service.activity import log_activity
                log_activity("warning", "config",
                             f"Project '{project.id}': dependency {problem}")

            for root in roots:
                info = describe_dependency(root.path)
                if info is None:          # resolve_dependency_roots already checked
                    continue
                fw, created = self._frameworks.register(**info.as_registration())
                expected.add(fw.collection_name)

                # Skip the import when this corpus is already complete.
                #
                # A LINK is the completeness signal: linking happens strictly
                # after a successful `_index_framework_corpus`, so "some project
                # links it" proves an import finished. Point count alone would
                # not — an interrupted run leaves a partial corpus with plenty
                # of points, and skipping that would strand it half-indexed
                # forever.
                #
                # Without this, every dependency edit anywhere re-imports every
                # declared corpus: linking a second project to a 32,782-file
                # Odoo core re-embedded all of it to change one row. The whole
                # promise of the model is "indexed once, shared".
                complete = bool(self._frameworks.projects_for(fw.collection_name))
                if complete and not refresh:
                    indexed = {"files": 0, "chunks": 0, "reused": True}
                    logger.info("Framework %s already complete — reusing (%d points)",
                                fw.collection_name, self._count_points(fw.collection_name))
                else:
                    indexed = self._index_framework_corpus(fw.collection_name, root.path,
                                                           info.name, _tick)
                # Link only after the corpus exists, so a project is never
                # pointed at an empty collection.
                self._frameworks.link(record.uuid, fw.collection_name,
                                      link_kind="declared", detector=info.detector)
                # Adoption cleanup: the same files may already be in the
                # project's own collection from before the dependency was
                # declared. Only ever after the framework copy is confirmed.
                # Adoption cleanup runs regardless: THIS project may still
                # hold a copy even when the shared corpus was already complete
                # for someone else.
                purged = self._purge_dependency_from_project(project, record, root, indexed)

                results.append({
                    "project": project.id,
                    "framework": info.name,
                    "version": info.version,
                    "edition": info.edition,
                    "build_id": info.build_id,
                    "collection": fw.collection_name,
                    "created": created,
                    "detector": info.detector,
                    "root": str(root.path),
                    "inside_project": root.inside_project,
                    "files_indexed": indexed["files"],
                    "chunks_indexed": indexed["chunks"],
                    "purged_from_project": purged,
                })
                logger.info(
                    "Framework %s (%s) %s for %s -> %s (%d files, purged %d from project)",
                    info.name, info.version or "unversioned",
                    "registered" if created else "reused", project.id,
                    fw.collection_name, indexed["files"], purged,
                )

            released.extend(self._release_frameworks(project, record, expected))

        # Releases go LAST so the linked entries keep their existing positions
        # and shape — callers that read results[0] predate this reconciliation.
        return results + released

    def _release_frameworks(self, project, record, expected: set[str]) -> list[dict]:
        """Unlink framework corpora this project no longer declares.

        The forward direction (declare -> index -> link -> purge) is only half a
        lifecycle. Without this, un-declaring a dependency leaves the project
        still linked, so its search keeps returning the framework copy — and the
        project re-indexes those same files into its own collection now that the
        scanner no longer excludes them. The user sees every hit twice, with no
        way to tell which is which.

        Dropping the corpus is refused while any OTHER project still links it
        (``FrameworkRegistry.remove``), which is what keeps two projects sharing
        one Odoo build safe: the second project unlinking must not delete the
        collection the first is still reading.
        """
        if self._frameworks is None:
            return []
        released: list[dict] = []
        for collection in self._frameworks.framework_collections_for(record.uuid):
            if collection in expected:
                continue
            self._frameworks.unlink(record.uuid, collection)
            remaining = self._frameworks.projects_for(collection)
            dropped = False
            if not remaining:
                # Last referent gone: no search can reach this corpus any more,
                # so keeping it is storage nobody can read.
                try:
                    self._frameworks.remove(collection)
                    self._client.delete_collection(collection_name=collection)
                    self._ensured_collections.discard(collection)
                    dropped = True
                except Exception as exc:  # noqa: BLE001
                    # Unlinked but not dropped is SAFE (orphaned storage, no
                    # wrong answers); the reverse would not be. Never fatal.
                    logger.warning("framework %s unlinked but not dropped: %s",
                                   collection, exc)
            released.append({
                "action": "released",
                "project": project.id,
                "collection": collection,
                "dropped": dropped,
                "still_linked_by": len(remaining),
            })
            logger.info("Framework %s released from %s (dropped=%s, %d project(s) left)",
                        collection, project.id, dropped, len(remaining))
            from ragtools.service.activity import log_activity
            log_activity("info", "config",
                         f"Project '{project.id}': dependency corpus {collection} "
                         f"{'removed' if dropped else 'unlinked'}")
        return released

    def _index_framework_corpus(self, collection: str, root: Path,
                                framework_id: str, tick) -> dict:
        """Index a framework root into its shared collection, streaming.

        Uses the same bounded-window path as project indexing, so a 38k-file
        framework costs one window of memory rather than the whole corpus. The
        corpus is keyed by the FRAMEWORK id, not the project's — that is what
        makes one copy serve every project that links it.
        """
        from ragtools.indexing.scanner import discover_indexable_files

        ensure_collection(self._client, collection, self._encoder.dimension)
        self._ensured_collections.add(collection)

        # A framework corpus is reference material: index docs AND code, and
        # apply the project's ignore rules for build/vendor noise.
        files = discover_indexable_files(root, ignore_rules=self._ignore_rules,
                                         mode="general")
        total = len(files)
        stats = {"files": 0, "chunks": 0}
        window: list = []

        def _flush(batch):
            if not batch:
                return
            prepared = []
            for path in batch:
                rel = f"{framework_id}/{path.resolve().relative_to(root).as_posix()}"
                chunks = chunk_file(
                    file_path=path, project_id=framework_id, relative_path=rel,
                    chunk_size=self._settings.chunk_size,
                    chunk_overlap=self._settings.chunk_overlap,
                )
                if chunks:
                    apply_source_class_and_redaction(chunks, rel)
                    prepared.append((rel, chunks))
            if not prepared:
                return
            with self._lock:
                all_chunks = []
                spans = []
                for rel, chunks in prepared:
                    spans.append((len(all_chunks), len(chunks), rel))
                    all_chunks.extend(chunks)
                embeddings = self._encoder.encode_batch([c.text for c in all_chunks])
                points = []
                for start, count, rel in spans:
                    points.extend(chunks_to_points(
                        all_chunks[start : start + count],
                        embeddings[start : start + count],
                        rel,
                    ))
                    stats["files"] += 1
                    stats["chunks"] += count
                delete_files_points(self._client, collection, [r for _s, _c, r in spans])
                upsert_points(self._client, collection, points)

        for path in files:
            window.append(path)
            if len(window) >= _INDEX_BATCH_SIZE:
                _flush(window)
                window = []
                tick(stats["files"], total, "framework")
        _flush(window)
        tick(stats["files"], total, "framework")
        return stats

    def _purge_dependency_from_project(self, project, record, root, indexed) -> int:
        """Remove a newly-declared dependency's files from the PROJECT collection.

        Adopting a dependency on an already-indexed project would otherwise
        leave every framework file in two places: the framework collection and
        the project's own — duplicated search results and wasted storage.

        Refuses to purge unless the framework copy actually exists. Deleting the
        only copy because the framework index silently produced nothing would
        turn an optimisation into data loss.
        """
        if indexed["chunks"] <= 0:
            logger.warning(
                "Not purging %s from project %s: the framework corpus indexed 0 "
                "chunks, so the project collection holds the only copy",
                root.path, project.id,
            )
            return 0

        prefix = None
        if root.inside_project and root.relative:
            # Project paths are stored as "<project_id>/<relative>".
            prefix = f"{project.id}/{root.relative}/"
        if prefix is None:
            return 0

        collection = self._router.write_collection(project.id)
        state = IndexState(self._settings.state_db)
        try:
            victims = [r["file_path"] for r in state.get_all_for_project(project.id)
                       if r["file_path"].startswith(prefix)]
            if not victims:
                return 0
            with self._lock:
                delete_files_points(self._client, collection, victims)
                for path in victims:
                    state.remove(path)
                state.commit()
        finally:
            state.close()
        logger.info("Purged %d dependency files from project collection %s",
                    len(victims), collection)
        return len(victims)

    def _purge_missing(self, current_paths: set, project_id, stats) -> None:
        """Remove tracked files that are no longer on disk.

        Shared by both indexers. The full index never did this, so a file
        deleted from disk kept its vectors until someone rebuilt — search went
        on returning a file that no longer existed.
        """
        state = IndexState(self._settings.state_db)
        try:
            if project_id:
                tracked = {r["file_path"] for r in state.get_all_for_project(project_id)}
            else:
                tracked = state.get_all_paths()
            missing = tracked - current_paths
            if not missing:
                return
            with self._lock:
                # Resolve each path's collection FIRST — the state row is the
                # only record of which project owned it, and it is about to go.
                drop: dict[str, list[str]] = {}
                for path in missing:
                    for coll in self._collections_for_path(state, path):
                        drop.setdefault(coll, []).append(path)
                for coll, paths in drop.items():
                    delete_files_points(self._client, coll, paths)
                for path in missing:
                    state.remove(path)
                    stats["deleted"] += 1
                state.commit()
        finally:
            state.close()

    def _drop_stale_vectors(self, emptied, stats, counter_key) -> None:
        """Remove vectors for files that no longer produce any chunks.

        Without this a file edited down below the chunker's minimum keeps its
        old vectors for ever, so search keeps returning content the file no
        longer contains.
        """
        if not emptied:
            return
        with self._lock:
            state = IndexState(self._settings.state_db)
            try:
                drop: dict[str, list[str]] = {}
                for pid, relative_path, _hash in emptied:
                    drop.setdefault(self._write_collection(pid), []).append(relative_path)
                for coll, paths in drop.items():
                    delete_files_points(self._client, coll, paths)
                for pid, relative_path, file_hash in emptied:
                    state.update(relative_path, pid, file_hash, 0)
                    stats[counter_key] += 1
                state.commit()
            finally:
                state.close()

    @staticmethod
    def _skipped_incremental() -> dict:
        return {"indexed": 0, "skipped": 0, "deleted": 0, "chunks_indexed": 0,
                "projects": [], "busy": True}

    def _write_collection(self, project_id: str | None) -> str:
        """Collection for this project's writes, created on first use.

        A project added after boot (or registered mid-migration) has no
        collection yet; creating it lazily here means indexing never fails on a
        missing collection, and ``ensure_collection`` is a no-op once it exists.
        """
        name = self._router.write_collection(project_id)
        if name not in self._ensured_collections:
            ensure_collection(self._client, name, self._encoder.dimension)
            self._ensured_collections.add(name)
        return name

    def _read_collections(self, project_id=None, project_ids=None) -> list[str]:
        """Collections a scoped read must span (own + linked frameworks)."""
        return self._router.read_collections(project_id, project_ids)

    #: Chunks returned for one file. A generated bundle can hold thousands;
    #: the panel that consumes this shows the total alongside, so the cap is
    #: visible rather than a silent truncation.
    _FILE_CHUNK_LIMIT = 60

    def get_file_chunks(self, project_id: str, file_path: str,
                        collection: str | None = None,
                        limit: int | None = None) -> dict:
        """The chunks actually stored for one file — text and all.

        This is the only way to see what the indexer really kept for a file.
        Everything else infers it: search shows whichever chunk matched a
        query, and the map shows a dot. "What is in my index for this file"
        was unanswerable without guessing a query that would return it.

        ``collection`` disambiguates. A project and a framework it vendors can
        BOTH hold ``odoo/api.py``, so a lookup by path alone would return the
        wrong copy — and which copy matters, because one is editable and the
        other is not. The caller passes the collection its point came from.

        Fail-closed on scope: the collection must be one the project may
        already read. Otherwise this becomes a way to read any collection by
        name, bypassing the isolation the per-project model exists to enforce.
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        from ragtools.secret_scan import redact_text

        limit = limit or self._FILE_CHUNK_LIMIT
        with self._lock:
            allowed = self._read_collections(project_id)
            if collection:
                if collection not in allowed:
                    raise ValueError(
                        f"collection {collection!r} is not readable by project "
                        f"{project_id!r}"
                    )
                targets = [collection]
            else:
                # Own collection first (router order), so an ambiguous path
                # resolves to the project's own copy rather than a framework's.
                targets = allowed

            flt = Filter(must=[FieldCondition(key="file_path",
                                              match=MatchValue(value=file_path))])
            for target in targets:
                try:
                    points, _ = self._client.scroll(
                        collection_name=target, scroll_filter=flt,
                        limit=limit, with_payload=True, with_vectors=False,
                    )
                except Exception:  # noqa: BLE001 — a missing collection is not an error
                    continue
                if not points:
                    continue
                try:
                    total = int(self._client.count(collection_name=target,
                                                   count_filter=flt,
                                                   exact=True).count)
                except Exception:  # noqa: BLE001
                    total = len(points)

                chunks = []
                for point in points:
                    payload = point.payload or {}
                    # Serve-time redaction, exactly as search applies it. This
                    # is a content surface: skipping it here would expose in
                    # the panel what every other read path masks.
                    text = redact_text(payload.get("text", "") or "")
                    chunks.append({
                        "id": str(point.id),
                        "index": payload.get("chunk_index", 0) or 0,
                        "text": text,
                        "line_start": payload.get("line_start", 0) or 0,
                        "line_end": payload.get("line_end", 0) or 0,
                        "headings": payload.get("headings", []) or [],
                        "language": payload.get("language", "") or "",
                        "chunk_type": payload.get("chunk_type", "") or "",
                        "source_class": payload.get("source_class", "") or "",
                        "class_name": payload.get("class_name") or "",
                        "function_name": payload.get("function_name") or "",
                        "signature": payload.get("signature", "") or "",
                        "symbols": payload.get("symbols", []) or [],
                        "imports": payload.get("imports", []) or [],
                        "exports": payload.get("exports", []) or [],
                        "token_count": payload.get("token_count", 0) or 0,
                    })
                chunks.sort(key=lambda c: (c["index"], c["line_start"]))
                first = chunks[0]
                return {
                    "file_path": file_path,
                    "project": project_id,
                    "collection": target,
                    "scope": "framework" if target.startswith("fw_") else "project",
                    "language": first["language"],
                    "chunk_type": first["chunk_type"],
                    "source_class": first["source_class"],
                    "total": total,
                    "returned": len(chunks),
                    "truncated": total > len(chunks),
                    "line_start": min(c["line_start"] for c in chunks),
                    "line_end": max(c["line_end"] for c in chunks),
                    "tokens": sum(c["token_count"] for c in chunks),
                    "chunks": chunks,
                }

        return {"file_path": file_path, "project": project_id, "collection": "",
                "scope": "project", "total": 0, "returned": 0, "truncated": False,
                "chunks": []}

    def _identity_registry(self):
        """The registry the store identity is fingerprinted against, or None.

        ``None`` under ``shared``, where there is no registry and the identity
        is exactly what it was before R06 — so the fingerprint stays "" and
        nothing about the default layout changes.

        A registry that raises is NOT smoothed over into ``None`` here. Both
        callers already guard: the check assumes trustworthy (its existing
        behaviour), and the stamp is skipped, which leaves the previous identity
        standing and forces a re-index next run. Substituting "" instead would
        stamp "unknown" and quietly switch the guard off for this install.
        """
        return self._registry

    def _check_index_identity(self, state) -> tuple[bool, list[str]]:
        """Whether ``state``'s file hashes may be trusted for skipping.

        STORE-WIDE only: engine, layout, legacy collection name, model,
        dimension. A change to any of those moves every project's vectors at
        once, so the whole state DB stops describing this store.

        Where a project's own mapping moved, ask :meth:`_untrusted_projects`.
        Folding that into this answer is what made adding one project re-embed
        the corpus.
        """
        from ragtools.index_identity import current_identity, reconcile

        try:
            identity = current_identity(self._settings, self._encoder.dimension,
                                        registry=self._identity_registry())
            return reconcile(state, identity)
        except Exception:  # noqa: BLE001 — never block indexing on the guard
            logger.exception("index-identity check failed; assuming trustworthy")
            return True, []

    def _untrusted_projects(self, state) -> dict[str, list[str]]:
        """``project_id -> changed fields`` for projects that must be re-indexed.

        The per-project half of R06. Only a project whose OWN recorded mapping
        (uuid, collection, generation, embedding identity) differs appears here;
        a project that merely appeared alongside does not, which is the entire
        difference between this and the registry-wide fingerprint.

        An empty dict on failure, matching :meth:`_check_index_identity`: the
        guard narrows a re-index, and a guard that cannot run must not widen one.
        """
        from ragtools.index_identity import current_project_identities, untrusted_projects

        try:
            live = current_project_identities(self._settings, self._encoder.dimension,
                                              registry=self._identity_registry())
            return untrusted_projects(state, live)
        except Exception:  # noqa: BLE001 — never block indexing on the guard
            logger.exception("per-project identity check failed; assuming trustworthy")
            return {}

    def _stamp_index_identity(self, projects=None) -> None:
        """Record the current store on the state DB, after a run has written it.

        Two stamps, written together:

        * the store-wide identity, including the registry fingerprint — the
          GLOBAL integrity signal that notices a lost or restored registry;
        * one row per project, so a later change can be localised to the project
          it actually affects instead of invalidating every project's hashes.

        They are written together on purpose: a fingerprint with no per-project
        rows is exactly the ambiguous state that has to be answered
        conservatively (see ``index_identity.reconcile``), so leaving one behind
        would re-create the blast radius this refinement removes.

        ``projects`` limits the per-project rows to the ones this run actually
        covered; ``None`` means all of them, which is right for an unscoped run
        and for a rebuild. A SCOPED run must not stamp its neighbours: recording
        project B's current mapping because project A was indexed is how a
        project that still needs re-indexing comes to look finished — the same
        "confidently empty" shape, one project wide.
        """
        from ragtools.index_identity import (
            current_identity, current_project_identities, stamp, stamp_projects,
        )

        try:
            registry = self._identity_registry()
            state = IndexState(self._settings.state_db)
            try:
                stamp(state, current_identity(self._settings,
                                              self._encoder.dimension,
                                              registry=registry))
                live = current_project_identities(
                    self._settings, self._encoder.dimension, registry=registry)
                if projects is not None:
                    wanted = {p for p in projects if p}
                    live = {k: v for k, v in live.items() if k in wanted}
                stamp_projects(state, live)
                # The mapping has just been proven against real writes, so a
                # hold armed at boot can now clear. Without this an install that
                # recovered stays locked out of swaps until it is restarted.
                self._reconcile_registry_integrity(state)
            finally:
                state.close()
        except Exception:  # noqa: BLE001
            logger.exception("could not record the index identity")

    def _reconcile_registry_integrity(self, state=None):
        """Re-derive registry integrity and arm/release the write hold.

        Non-fatal by construction: a reporting-and-guarding pass must never be
        the reason the service will not boot. A failure here leaves the hold as
        it was, which is the conservative side of the trade.
        """
        from ragtools import registry_integrity

        registry = self._identity_registry()
        if registry is None:
            return None
        owned = state is None
        try:
            if owned:
                state = IndexState(self._settings.state_db)
            try:
                return registry_integrity.reconcile_startup(state, registry)
            finally:
                if owned:
                    state.close()
        except Exception:  # noqa: BLE001
            logger.exception("registry-integrity reconciliation failed")
            return None

    def _collections_for_path(self, state, relative_path: str) -> list[str]:
        """Which collection(s) hold a tracked path's points.

        In shared mode this is always the one collection. In per-project mode
        the owning project comes from the state row; if the row is gone or its
        project is no longer registered (config edited out from under us) we
        fall back to sweeping every collection, because leaving orphaned vectors
        behind would keep serving deleted files in search results.
        """
        if not self._router.is_per_project:
            return [self._router.shared_collection]
        record = state.get(relative_path) or {}
        pid = record.get("project_id")
        if pid:
            try:
                return [self._router.write_collection(pid)]
            except Exception:  # noqa: BLE001 — unregistered project; sweep below
                pass
        return self._router.all_collections()

    @property
    def registry(self):
        """Project registry, or None in shared mode."""
        return self._registry

    @property
    def framework_registry(self):
        """Framework registry, or None in shared mode."""
        return self._frameworks

    @property
    def client(self) -> QdrantClient:
        return self._client

    @property
    def encoder(self) -> Encoder:
        return self._encoder

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def ignore_rules(self) -> IgnoreRules:
        return self._ignore_rules

    def search(
        self,
        query: str,
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        top_k: int | None = None,
        score_threshold: float | None = None,
        allow_unscoped: bool = False,
    ) -> list[SearchResult]:
        """Search the knowledge base. Thread-safe.

        Fail-closed boundary (S1/A2): an unscoped or requested-but-empty scope
        is REFUSED here with ``ScopeUnresolvedError`` unless ``allow_unscoped``
        is explicitly set. This single choke point covers HTTP and MCP-direct.

        It is also where a rebuild in flight is refused. While a layout
        migration is running the new collections exist and are being filled, so
        a query would return the ordinary "no matches" shape from an index that
        has not been built yet — telling the user their content is gone, in the
        one form they have no reason to doubt.
        """
        from ragtools.upgrade.relayout import guard_ready

        guard_ready(self._settings)
        resolve_scope(project_id, project_ids, allow_unscoped=allow_unscoped)
        with self._lock:
            searcher = Searcher(
                client=self._client,
                encoder=self._encoder,
                settings=self._settings,
            )
            return searcher.search(
                query=query,
                project_id=project_id,
                project_ids=project_ids,
                top_k=top_k,
                score_threshold=score_threshold,
                # Scope already validated above; the searcher trusts the owner's
                # decision (it still refuses a requested-but-empty scope itself).
                allow_unscoped=True,
                # Under the per-project model this is the security boundary:
                # the router returns ONLY this scope's collections, so another
                # project's vectors are not merely filtered out — they are never
                # queried. `collection_scoped` tells the searcher that, so it
                # drops the now-redundant project_id filter (which would also
                # hide linked framework corpora).
                collections=self._read_collections(project_id, project_ids),
                collection_scoped=self._router.is_per_project,
            )

    def search_formatted(
        self,
        query: str,
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        top_k: int | None = None,
        compact: bool = False,
        allow_unscoped: bool = False,
    ) -> dict:
        """Search and return both raw results and formatted context."""
        results = self.search(
            query,
            project_id,
            project_ids=project_ids,
            top_k=top_k,
            allow_unscoped=allow_unscoped,
        )
        if compact:
            from ragtools.retrieval.formatter import format_context_compact
            formatted = format_context_compact(results, query)
        else:
            formatted = format_context(results, query)
        return {
            "query": query,
            "count": len(results),
            "results": [
                {
                    "score": r.score,
                    "confidence": r.confidence,
                    "text": r.raw_text,
                    "file_path": r.file_path,
                    "project_id": r.project_id,
                    "headings": r.headings,
                    "source_class": r.source_class,
                    "chunk_type": r.chunk_type,
                    "language": r.language,
                    "line_start": r.line_start,
                    "line_end": r.line_end,
                    # WHICH collection answered. Without this the caller cannot
                    # tell "your code does X" from "the framework you vendor
                    # does X" — the payload alone cannot say, because a
                    # framework chunk carries the framework's id, not the
                    # project's. Computed in the searcher and, until now,
                    # dropped here.
                    "scope": r.scope,
                    "scope_source": r.scope_source,
                }
                for r in results
            ],
            "formatted": formatted,
        }

    def search_project_context(
        self,
        query: str,
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        top_k: int | None = None,
        allow_unscoped: bool = False,
    ) -> dict:
        """Codebase-first layered retrieval for development requests.

        Runs the dev-search pipeline (code → docs → config, then rerank by
        context priority) and returns a dict with the ranked results plus a
        formatted "Project Context Mode" block ready for answer generation.

        Fail-closed (S1/A2): refuses an unscoped/empty scope unless
        ``allow_unscoped`` is set.
        """
        resolve_scope(project_id, project_ids, allow_unscoped=allow_unscoped)
        with self._lock:
            from ragtools.retrieval.dev_pipeline import dev_search
            from ragtools.retrieval.formatter import format_dev_context

            searcher = Searcher(
                client=self._client,
                encoder=self._encoder,
                settings=self._settings,
                # Route it. Without this the dev pipeline's layers each called
                # search() with no collections and hit the legacy fallback, so
                # /api/dev-search answered `count: 0` on every per-project
                # install — indistinguishable from "no matches".
                collections=self._read_collections(project_id),
            )
            outcome = dev_search(
                searcher,
                query,
                project_id=project_id,
                project_ids=project_ids,
                top_k=top_k,
                force_dev=True,  # the dev endpoint is always code-first
            )
            formatted = format_dev_context(outcome.results, query, outcome.triggers,
                                           outcome.warnings, outcome.code_indexed)
            return {
                "query": query,
                "count": len(outcome.results),
                "is_dev_request": outcome.is_dev_request,
                "triggers": outcome.triggers,
                "layers": outcome.layers,
                "warnings": outcome.warnings,
                "code_indexed": outcome.code_indexed,
                "results": [
                    {
                        "score": r.score,
                        "confidence": r.confidence,
                        "text": r.raw_text,
                        "file_path": r.file_path,
                        "project_id": r.project_id,
                        "headings": r.headings,
                        "source_class": r.source_class,
                        "language": r.language,
                        "chunk_type": r.chunk_type,
                        "class_name": r.class_name,
                        "function_name": r.function_name,
                        "symbols": r.symbols,
                        "line_start": r.line_start,
                        "line_end": r.line_end,
                        "scope": r.scope,
                        "scope_source": r.scope_source,
                    }
                    for r in outcome.results
                ],
                "formatted": formatted,
            }

    def find_definitions(self, symbol: str, project_id: str | None = None, top_k: int = 25) -> list[dict]:
        """Find likely definition sites for a symbol (cross-file code-graph v1)."""
        with self._lock:
            from ragtools.retrieval.codegraph import find_definitions as _find
            searcher = Searcher(
                client=self._client, encoder=self._encoder, settings=self._settings,
            )
            # A symbol may be defined in the project or in a framework it
            # references — the code graph must look in both.
            searcher.definition_collections = self._read_collections(project_id)
            searcher.collection_scoped = self._router.is_per_project
            return _find(searcher, symbol, project_id=project_id, top_k=top_k)

    def audit_secrets(self, project_id: str | None = None, limit: int = 5000) -> dict:
        """Audit indexed chunk text for secret material — reports file:line + rule
        names, NEVER the value. Catches live secrets in legacy (pre-redaction)
        points and flags files whose secrets are already redaction-masked (rotate
        them at the source)."""
        with self._lock:
            from qdrant_client.models import FieldCondition, Filter, MatchValue
            from ragtools.secret_scan import scan
            from ragtools.source_class import GENERATED, classify_source_class

            flt = None
            if project_id:
                flt = Filter(must=[FieldCondition(key="project_id", match=MatchValue(value=project_id))])

            _sev = {"high": 3, "medium": 2, "low": 1}
            findings: list[dict] = []
            scanned = 0
            # Audit every collection in scope. Auditing only the shared
            # collection would report a clean bill of health for an install
            # whose secrets all live in per-project collections.
            for collection in self._read_collections(project_id):
                offset = None
                while scanned < limit:
                    points, offset = self._client.scroll(
                        collection_name=collection,
                        scroll_filter=flt, with_payload=True,
                        limit=min(256, limit - scanned), offset=offset,
                    )
                    if not points:
                        break
                    for p in points:
                        scanned += 1
                        payload = p.payload or {}
                        fp = payload.get("file_path", "")
                        # Don't audit generated mirrors (coverage/build) — they
                        # inflate findings with copies of source and never hold
                        # a real secret.
                        if classify_source_class(fp) == GENERATED:
                            continue
                        text = payload.get("text", "") or ""
                        hits = scan(text)
                        redacted = text.count("***REDACTED:")
                        if hits or redacted:
                            severity = max((h.get("severity", "low") for h in hits),
                                           key=lambda s: _sev.get(s, 0), default="low")
                            findings.append({
                                "file_path": fp,
                                "project_id": payload.get("project_id", ""),
                                "line_start": payload.get("line_start", 0),
                                "rules": sorted({h["rule"] for h in hits}),
                                "severity": severity,
                                "redacted_markers": redacted,
                            })
                    if offset is None:
                        break
            return {"scanned": scanned, "files_with_secrets": len(findings), "findings": findings}

    def get_map_points(self, force_recompute: bool = False,
                       project_id: str | None = None) -> dict:
        """Map payload: ``{points, coverage, excluded, cache?}``. Thread-safe.

        ``project_id`` computes that project's own collections directly rather
        than filtering a global sample. Filtering the sample is what made
        ``?project=rag`` answer ``count: 0`` for a project holding 1,716
        chunks — the filter cannot recover data the sampler never fetched.
        """
        with self._lock:
            from ragtools.service.map_data import (
                compute_map_points, load_cached_map, save_map_cache, invalidate_map_cache,
            )

            if project_id:
                # Scoped requests are not cached: they are rare, cheap (one
                # project), and must never be served from the global blob.
                return compute_map_points(
                    self._client, self._settings,
                    self._read_collections(project_id),
                )

            if not force_recompute:
                # Serve whatever we have IMMEDIATELY (including a stale map) and
                # let a background job refresh it. Recomputing on the request
                # path meant scrolling every point + PCA while the machine was
                # already busy indexing. Staleness is stamped on the payload so
                # the UI can say so rather than implying the map is current.
                cached = load_cached_map(self._settings.state_db, allow_stale=True)
                if isinstance(cached, dict) and "points" in cached:
                    self._request_map_refresh_if_stale()
                    return cached

            result = compute_map_points(
                self._client, self._settings, self._router.all_collections()
            )
            save_map_cache(self._settings.state_db, result)
            return result

    def run_full_index(self, project_id: str | None = None, progress=None) -> dict:
        """Full index — re-index everything.

        Uses split-lock strategy like run_incremental_index:
        scan/hash/chunk outside lock, encode/upsert inside lock in batches.

        ``progress(done, total, phase)`` is an optional callback invoked at safe
        boundaries (between files, and between upsert batches). It does double
        duty: it reports progress, and **it may raise to cancel** — the index
        then stops at that boundary. Because indexing upserts per batch, a
        cancelled run leaves a partial-but-consistent index that a later
        incremental run completes.
        """
        from ragtools.service.activity import log_activity

        with self._exclusive_index("Full index") as acquired:
            if acquired is None:
                log_activity("info", "indexer",
                             "Full index skipped — another indexing run is in progress")
                return {"files_indexed": 0, "chunks_indexed": 0, "projects": [],
                        "busy": True}
            return self._run_full_index_locked(project_id, progress, log_activity)

    def _run_full_index_locked(self, project_id, progress, log_activity) -> dict:
        log_activity("info", "indexer", "Full index started")

        def _tick(done, total, phase):
            self._beat(done, total, phase)
            if progress is not None:
                progress(done, total, phase)

        # Before the scan, not only after it. `_scan_files` is one call over the
        # whole corpus and reports nothing while it walks tens of thousands of
        # files; a waiter reading the heartbeat would otherwise see the run go
        # quiet for the entire walk and have no idea it was scanning.
        _tick(0, 0, "scan")
        files = self._scan_files(project_id)
        total_files = len(files)
        _tick(0, total_files, "scan")

        stats = {"files_indexed": 0, "chunks_indexed": 0, "deleted": 0,
                 "projects": set()}

        # Files present on disk, so vectors for anything else can go. A full
        # index that only wrote what it found left the vectors of deleted files
        # behind for ever — search kept returning files that no longer exist.
        current_paths = {self._resolve_relative_path(pid, fp) for pid, fp in files}
        self._purge_missing(current_paths, project_id, stats)

        def _work():
            # Lazy: each file is hashed and read only when its window comes up,
            # so peak memory is one window rather than the whole corpus.
            for n, (pid, file_path) in enumerate(files, start=1):
                _tick(n, total_files, "chunk")
                relative_path = self._resolve_relative_path(pid, file_path)
                yield pid, relative_path, IndexState.hash_file(file_path), file_path

        emptied = self._stream_index(_work(), total_files, _tick, stats, "files_indexed")
        self._drop_stale_vectors(emptied, stats, "deleted")

        with self._lock:
            self._invalidate_map_cache()

        stats["projects"] = sorted(stats["projects"])
        logger.info("Full index: %d files, %d chunks", stats["files_indexed"], stats["chunks_indexed"])
        log_activity("success", "indexer",
                     f"Full index: {stats['files_indexed']} files, {stats['chunks_indexed']} chunks")
        # The state DB now describes this store (engine + collection layout).
        # Scoped to what ran: a single-project full index proves that project's
        # mapping and says nothing about anyone else's.
        self._stamp_index_identity(None if project_id is None else [project_id])
        # Surface a scale warning into logs (and via /api/status) if applicable.
        self._emit_scale_warning_after_index(log_activity)
        return stats

    def run_incremental_index(self, project_id: str | None = None, progress=None) -> dict:
        """Incremental index — only new/changed/deleted.

        Uses split-lock strategy: scan/hash/chunk outside lock (I/O only),
        then encode/upsert/state-update inside lock in batches, releasing
        between batches so search requests aren't blocked for minutes.

        ``progress(done, total, phase)`` mirrors :meth:`run_full_index`. It
        matters most here: a normal incremental finishes in milliseconds, but
        one forced to re-index everything (storage or layout change) runs for
        many minutes — and with no callback the UI showed ``0/None`` for the
        whole run, which is indistinguishable from a hung job.
        """
        with self._exclusive_index("Incremental index") as acquired:
            if acquired is None:
                # A watcher tick that lands during a long re-index has nothing
                # to add — the next tick picks up whatever changed.
                return self._skipped_incremental()
            return self._run_incremental_index_locked(project_id, progress)

    def _run_incremental_index_locked(self, project_id: str | None = None,
                                      progress=None) -> dict:
        def _tick(done, total, phase):
            self._beat(done, total, phase)
            if progress is not None:
                progress(done, total, phase)

        # --- Phase 1: outside lock — scan, hash, chunk, detect changes ---
        # Beat first: the scan reports nothing while it runs, and a job waiting
        # on this lock reads silence as a possible stall. See `_STALL_SECONDS`.
        _tick(0, 0, "scan")
        files = self._scan_files(project_id)

        # Open a read-only state connection for change detection
        read_state = IndexState(self._settings.state_db)

        # Does this state DB describe the store we are about to write to? After
        # a storage-engine or collection-layout change it describes the PREVIOUS
        # one, and trusting its hashes skips every file against an empty store.
        trustworthy, changed = self._check_index_identity(read_state)
        if not trustworthy:
            from ragtools.index_identity import explain
            logger.warning(
                "Index state does not describe the current store (%s) — "
                "re-indexing instead of skipping unchanged files",
                explain(changed),
            )
            from ragtools.service.activity import log_activity as _log
            _log("warning", "indexer",
                 f"Storage changed ({explain(changed)}) — full re-index required")

        # Per project, and ONLY the ones whose own mapping moved. A project
        # added alongside these does not appear here, so it costs its own index
        # run and nothing else — the whole point of separating this from the
        # registry-wide fingerprint.
        stale_projects = {} if not trustworthy else self._untrusted_projects(read_state)
        if stale_projects:
            from ragtools.index_identity import explain_project
            from ragtools.service.activity import log_activity as _log
            for pid, fields in sorted(stale_projects.items()):
                reason = explain_project(pid, fields)
                logger.warning("Re-indexing one project: %s", reason)
                _log("warning", "indexer",
                     f"Re-indexing '{pid}' only — {reason}. Other projects are "
                     f"unaffected and keep their index.")

        stats = {"indexed": 0, "skipped": 0, "deleted": 0, "chunks_indexed": 0,
                 "projects": set()}
        total_files = len(files)
        _tick(0, total_files, "scan")

        # Deletion detection needs the full set of live paths up front. This is
        # the one thing that cannot stream — but it is cheap: path resolution
        # only, no file reads.
        current_paths = {self._resolve_relative_path(pid, fp) for pid, fp in files}
        self._purge_missing(current_paths, project_id, stats)

        def _work():
            """Yield only the files that need indexing, hashing lazily.

            A generator so each file is read and hashed when its window comes
            up. Materialising this (the old `pending` list of every chunk of
            every file) cost 2.46 GB on a 38k-file project.
            """
            for n, (pid, file_path) in enumerate(files, start=1):
                # Reported per file (the callback throttles itself). Without it
                # a forced full re-index sat at 0/None for twenty minutes.
                _tick(n, total_files, "chunk")
                relative_path = self._resolve_relative_path(pid, file_path)
                current_hash = IndexState.hash_file(file_path)
                # Two independent reasons a matching hash may not mean "already
                # in the store": the STORE changed (`trustworthy` False — every
                # project), or THIS PROJECT's collection changed (`stale_projects`
                # — that project alone). Both mean re-index; neither means delete.
                if (trustworthy and pid not in stale_projects
                        and not read_state.file_changed(relative_path, current_hash)):
                    stats["skipped"] += 1
                    continue
                yield pid, relative_path, current_hash, file_path

        try:
            emptied = self._stream_index(_work(), total_files, _tick, stats, "indexed")
        finally:
            read_state.close()

        # Files that still exist but no longer produce any chunks. Counting them
        # as "skipped" (the old behaviour) left their previous vectors in place
        # for ever, so search kept returning content the file no longer has.
        self._drop_stale_vectors(emptied, stats, "deleted")

        # Finalize
        with self._lock:
            self._invalidate_map_cache()

        stats["projects"] = sorted(stats["projects"])
        logger.info("Incremental index: %d indexed, %d skipped, %d deleted",
                    stats["indexed"], stats["skipped"], stats["deleted"])
        from ragtools.service.activity import log_activity
        log_activity("success", "indexer",
                     f"Incremental: {stats['indexed']} indexed, {stats['skipped']} skipped, {stats['deleted']} deleted")
        # Only now — after the vectors are actually written — does the state DB
        # describe this store. Stamping earlier would let an interrupted
        # migration look complete on the next run, and stamping projects this
        # run did not touch would do the same thing one project at a time.
        self._stamp_index_identity(None if project_id is None else [project_id])
        self._emit_scale_warning_after_index(log_activity)
        return stats

    def rebuild(self) -> dict:
        """Drop all data and rebuild from scratch.

        **Excludes indexing, not merely other rebuilds.** This used to take
        ``self._lock`` alone, while ``run_full_index`` takes ``_index_mutex`` and
        holds ``self._lock`` only per 30-file window. Two different primitives
        meant a rebuild could interleave at a window boundary and drop every
        collection and delete the state DB *underneath a running migration*,
        which then recreated the state DB and wrote into freshly emptied
        collections. Exactly that was attempted during the v3.2.0 incident; it
        failed only because the engine was already dead.

        **The state DB is deleted last, and only once every collection is proven
        to exist.** The backup covers the state DB — it does not cover vectors —
        so an ordering that drops collections and then fails leaves a machine
        with no index and no way back.
        """
        from ragtools.service import destructive

        with self._exclusive_index("Rebuild") as acquired:
            if acquired is None:
                raise destructive.OperationRefused(
                    "an indexing run is in progress; rebuild would drop the "
                    "collections it is writing into", code="index_busy")
            with self._lock:
                return self._rebuild_locked()

    def _rebuild_locked(self) -> dict:
        """Rebuild every enabled project — one at a time, replacement first.

        v3.4 dropped EVERY collection and deleted the state DB *before* it
        indexed anything, then walked every ``(project, file)`` pair in one loop
        with no ``try``/``except``. On the installed machine it reached project
        14 of 15, hit a transient ``WinError 10048`` from the client, and the
        whole run ended: project 15 was left holding the empty collection it had
        already been given, project 14 kept 1,442 of ~35,000 files, and because
        ``clear_intent`` sat in a ``finally`` the only durable evidence was
        erased — ``/health`` reported ``degraded: false, issues: []`` for the
        next twelve hours.

        So the unit of work is ONE PROJECT, and the order for each of them is
        **build → verify → swap → drop**:

        * the replacement is indexed into a NEW collection (``<base>_g<n+1>``)
          while the live one goes on serving;
        * it is adopted only once it has been COUNTED — ``None`` (could not ask)
          and ``0`` after chunks were written are both failures, not successes;
        * the swap is one atomic registry UPDATE
          (:meth:`ragtools.registry.ProjectRegistry.set_active_collection`);
        * the superseded collection is dropped LAST, and failing to drop it
          leaves an orphan to reclaim later rather than failing a rebuild that
          worked.

        A project that fails is logged, recorded in ``failed_projects`` and left
        EXACTLY as it was; the loop moves on. The state DB is never deleted —
        each project's rows are replaced only after its own swap — so a run that
        failed can still be diagnosed from what it says.
        """
        from ragtools.service import destructive
        from ragtools.service.activity import log_activity

        state_path = Path(self._settings.state_db)
        targets = list(self._router.all_collections())

        # PRECONDITION BEFORE ANY MUTATION — including before the backup, which
        # used to be the first thing that happened. A rebuild that cannot write
        # must not start by taking a backup and dropping collections.
        ok, detail = self.storage_reachable()
        if not ok:
            raise destructive.OperationRefused(
                f"the vector store is not reachable ({detail}); refusing to drop "
                f"an index that could not be rebuilt", code="storage_unreachable")

        # Snapshot the state DB before we touch it. Best-effort — failures
        # here must not block the rebuild itself (disk full, etc.).
        try:
            from ragtools.backup import backup_state_db, prune_backups
            backup_state_db(self._settings, trigger="rebuild")
            prune_backups(self._settings)
        except Exception as e:
            logger.warning("Pre-rebuild backup failed (non-fatal): %s", e)

        projects = list(self._settings.enabled_projects)
        intent = {"operation": "rebuild", "collections": targets,
                  "state_db": str(state_path),
                  "projects": [p.id for p in projects]}
        destructive.record_intent(self._settings, intent)

        log_activity("info", "indexer",
                     f"Rebuild started — {len(projects)} project(s), each one "
                     f"built and verified before anything of its own is dropped")

        stats = {"files_indexed": 0, "chunks_indexed": 0, "deleted": 0,
                 "projects": [], "failed_projects": [], "empty_projects": {},
                 "status": "completed"}
        rebuilt: list[str] = []
        failed: list[str] = []
        for project in projects:
            try:
                result = self._rebuild_project(project.id)
            except Exception as exc:  # noqa: BLE001 — ONE project, not the run
                # The v3.4 loop had no handler here at all, which is why the
                # first error ended the run and every project after it in scan
                # order kept the empty collection it had already been handed.
                failed.append(project.id)
                logger.exception("Rebuild failed for project %s", project.id)
                log_activity(
                    "error", "indexer",
                    f"Rebuild failed for project '{project.id}': {exc} — its "
                    f"previous index was left in place")
                continue
            stats["files_indexed"] += result["files_indexed"]
            stats["chunks_indexed"] += result["chunks_indexed"]
            stats["deleted"] += result.get("deleted", 0)
            if result.get("empty_reason"):
                stats["empty_projects"][project.id] = result["empty_reason"]
            rebuilt.append(project.id)

        stats["projects"] = sorted(rebuilt)
        stats["failed_projects"] = failed
        stats["status"] = "completed_with_failures" if failed else "completed"

        self._invalidate_map_cache()
        self._stamp_index_identity()

        if failed:
            # NOT in a `finally`, and NOT cleared. A hard kill left the marker
            # behind; an exception — the common case, and the one that actually
            # happened — used to wipe it, so /health reported a clean bill of
            # health over a half-rebuilt index.
            destructive.record_intent(self._settings, {
                **intent, "status": stats["status"],
                "failed_projects": failed, "projects_rebuilt": stats["projects"],
            })
            logger.error("Rebuild finished with failures: %s", ", ".join(failed))
            log_activity(
                "error", "indexer",
                f"Rebuild incomplete: {len(failed)} of {len(projects)} project(s) "
                f"failed ({', '.join(failed)}); their previous index is unchanged")
        else:
            destructive.clear_intent(self._settings)
            logger.info("Rebuild complete: %s", stats)
            log_activity("success", "indexer",
                         f"Rebuild: {stats['files_indexed']} files, "
                         f"{stats['chunks_indexed']} chunks")
        return stats

    @staticmethod
    def _staging_collection(record) -> str:
        """The name the NEXT generation of ``record``'s collection is built under.

        The suffix is stripped before it is re-applied so names cannot accrete
        one per rebuild (``proj_ab_g1_g2_g3``). A project collection is
        ``proj_<32 hex>`` (:func:`ragtools.identity.project_collection_name`),
        and hex has no ``g``, so a trailing ``_g<digits>`` is unambiguously a
        generation and never part of the identity.
        """
        base = record.collection_name
        head, sep, tail = base.rpartition("_g")
        if sep and tail.isdigit():
            base = head
        return f"{base}_g{int(record.generation) + 1}"

    def _rebuild_project(self, project_id: str) -> dict:
        """Rebuild ONE project, or raise. Never leaves it half-replaced.

        The per-project layout gets the full build → verify → swap. The shared
        layout cannot have one — every project writes to the same collection, so
        there is nothing to swap — and gets the established indexing pipeline
        scoped to a single project instead.
        """
        if not self._router.is_per_project or self._registry is None:
            return self._rebuild_project_in_place(project_id)

        record = self._registry.get(project_id)
        if record is None:
            raise RuntimeError(
                f"project {project_id!r} has no registry entry, so there is no "
                f"collection to rebuild it into")

        staging = self._staging_collection(record)
        previous = record.collection_name
        ensure_collection(self._client, staging, self._encoder.dimension)
        self._ensured_collections.add(staging)

        swapped = False
        try:
            rows, files_indexed, chunks_indexed = self._index_into(project_id, staging)
            self._verify_rebuilt(project_id, self._count_points(staging),
                                 staging, chunks_indexed)
            empty_reason = (self._explain_empty(project_id)
                            if chunks_indexed == 0 else "")

            # THE SWAP. One UPDATE, after verification — and nothing before this
            # line touched what the project was serving.
            self._registry.set_active_collection(
                record.uuid, staging, generation=int(record.generation) + 1)
            swapped = True

            self._replace_state_rows(project_id, rows)

            # Only now. A collection we could not drop is an orphan to reclaim
            # (`rag storage reclaim`), not a reason to fail a rebuild that worked.
            if previous != staging:
                try:
                    self._client.delete_collection(previous)
                    self._ensured_collections.discard(previous)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Rebuild: could not drop the superseded collection %s "
                        "(%s); it is an orphan to reclaim, not a failure",
                        previous, exc)
            return {"files_indexed": files_indexed,
                    "chunks_indexed": chunks_indexed,
                    "empty_reason": empty_reason}
        except Exception:
            # Best-effort, and ONLY while the project still points elsewhere:
            # after the swap `staging` IS the live collection, and deleting it
            # would be the exact data loss this method exists to prevent.
            if not swapped:
                try:
                    self._client.delete_collection(staging)
                except Exception:  # noqa: BLE001
                    pass
                self._ensured_collections.discard(staging)
            raise

    def _rebuild_project_in_place(self, project_id: str) -> dict:
        """Shared layout: rebuild one project inside the one shared collection.

        There is no swap to make here — every project writes to
        ``settings.collection_name`` — so this is the ordinary full-index
        pipeline scoped to a single project. Deliberately so: a second indexing
        pipeline is what the architecture forbids, and the streaming indexer
        already deletes a file's previous points immediately before writing its
        new ones, one window at a time. Nothing is dropped up front, and a
        failure costs this project rather than the install.
        """
        collection = self._write_collection(project_id)
        stats = {"files_indexed": 0, "chunks_indexed": 0, "deleted": 0,
                 "projects": set()}

        files = self._scan_files(project_id)
        total = len(files)

        def _tick(done, count, phase):
            self._beat(done, count, phase)

        _tick(0, total, "scan")
        current_paths = {self._resolve_relative_path(pid, fp) for pid, fp in files}
        self._purge_missing(current_paths, project_id, stats)

        def _work():
            for n, (pid, file_path) in enumerate(files, start=1):
                _tick(n, total, "chunk")
                relative_path = self._resolve_relative_path(pid, file_path)
                yield pid, relative_path, IndexState.hash_file(file_path), file_path

        emptied = self._stream_index(_work(), total, _tick, stats, "files_indexed")
        self._drop_stale_vectors(emptied, stats, "deleted")

        self._verify_rebuilt(project_id,
                             self._count_project_points(collection, project_id),
                             collection, stats["chunks_indexed"])
        empty_reason = (self._explain_empty(project_id)
                        if stats["chunks_indexed"] == 0 else "")
        return {"files_indexed": stats["files_indexed"],
                "chunks_indexed": stats["chunks_indexed"],
                "deleted": stats["deleted"], "empty_reason": empty_reason}

    def _index_into(self, project_id: str, collection: str):
        """Index one project's files into ``collection``.

        Returns ``(rows, files_indexed, chunks_indexed)``. ``rows`` are the state
        records the run WOULD write, held in memory rather than committed: until
        the swap happens the state DB must go on describing the index the project
        is actually serving.
        """
        files = self._scan_files(project_id)
        total = len(files)
        rows: list[tuple[str, str, str, int]] = []
        chunks_indexed = 0
        for n, (pid, file_path) in enumerate(files, start=1):
            # Per file. A rebuild is the longest thing this process does, and one
            # that reports nothing is indistinguishable from a hung one. `_beat`
            # is also where the run notices storage has gone away.
            self._beat(n, total, "rebuild")
            relative_path = self._resolve_relative_path(pid, file_path)
            file_hash = IndexState.hash_file(file_path)
            count = index_file(
                client=self._client,
                encoder=self._encoder,
                collection_name=collection,
                project_id=pid,
                file_path=file_path,
                relative_path=relative_path,
                chunk_size=self._settings.chunk_size,
                chunk_overlap=self._settings.chunk_overlap,
            )
            rows.append((relative_path, pid, file_hash, count))
            chunks_indexed += count
        return rows, len(rows), chunks_indexed

    @staticmethod
    def _verify_rebuilt(project_id: str, count: int | None, collection: str,
                        chunks_indexed: int) -> None:
        """Refuse to adopt a replacement that has not been PROVEN to hold data.

        UNKNOWN IS NOT ZERO and ZERO IS NOT SUCCESS. ``_count_points`` returns
        ``None`` when it could not ask, and a rebuild nobody can verify is not
        one that worked — adopting it is how a dead engine came to render as a
        confidently empty index.
        """
        if count is None:
            raise RuntimeError(
                f"{collection} could not be counted, so the rebuild of "
                f"{project_id!r} cannot be verified; refusing to adopt it")
        if chunks_indexed > 0 and count == 0:
            raise RuntimeError(
                f"{chunks_indexed} chunk(s) were written for {project_id!r} and "
                f"{collection} reports 0 points")

    def _explain_empty(self, project_id: str) -> str:
        """Why a project rebuilt to nothing — and whether that is legitimate.

        A project with no eligible files is empty BY DESIGN and must still be
        swapped; one that is empty because something went wrong must not be. The
        vocabulary is :func:`ragtools.upgrade.relayout.classify_empty`'s on
        purpose: the migration already draws exactly this distinction, and a
        second answer to the same question is how the two drift apart.
        """
        from ragtools.upgrade import relayout

        disposition, reason = relayout.classify_empty(
            self, self._settings, relayout.KIND_PROJECT, project_id)
        if disposition != relayout.STATUS_DONE:
            raise RuntimeError(f"{project_id!r} rebuilt to zero points: {reason}")
        return reason

    def _replace_state_rows(self, project_id: str, rows) -> None:
        """Swap ONE project's state rows; every other project's are untouched.

        A rebuild does not delete the state DB. It is the only record of what was
        ever indexed, and a run that fails needs it more than one that succeeds.
        """
        state = IndexState(self._settings.state_db)
        try:
            for row in state.get_all_for_project(project_id):
                state.remove(row["file_path"])
            for relative_path, pid, file_hash, count in rows:
                state.update(relative_path, pid, file_hash, count)
            state.commit()
        finally:
            state.close()

    def _count_project_points(self, collection: str, project_id: str) -> int | None:
        """Points ONE project holds inside a collection, or ``None`` if unknown.

        The shared layout has no per-project collection to count, and counting
        the whole thing would let one project's rebuild be "verified" by another
        project's vectors.
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        try:
            return int(self._client.count(
                collection_name=collection,
                count_filter=Filter(must=[FieldCondition(
                    key="project_id", match=MatchValue(value=project_id))]),
                exact=True).count)
        except Exception:  # noqa: BLE001 — unknown is None, never 0
            return None

    def get_status(self, lock_timeout: float = 0.75) -> dict:
        """Collection and index status. Thread-safe and **non-blocking**.

        The dashboard polls this. It used to take ``self._lock`` unconditionally,
        which is the same lock indexing holds across every encode/upsert batch —
        so during an index the whole UI hung waiting for it (reported from real
        use; ``search`` was unaffected because it never takes the lock).

        Now: try briefly, and if the index owns the lock return the last known
        snapshot marked ``stale`` + ``indexing`` instead of blocking. Callers
        render live-ish numbers with a spinner rather than freezing.
        """
        # A DEAD ENGINE IS ANSWERED FROM CACHE, NOT BY ASKING IT 50 TIMES.
        #
        # `_collection_points` counts every collection, and `_count_points` makes
        # TWO failing round-trips each before returning 0. Measured against a
        # dead engine with 25 collections: 62 seconds — the whole of it holding
        # `self._lock`, which is the same lock search and indexing need. One
        # dashboard poll stalled the entire owner for a minute, and the dashboard
        # polls this.
        #
        # The counts are also WRONG in that state, not merely slow: every one
        # comes back 0, so the page reports a confidently empty, non-stale index
        # beside a state DB saying 145,906 chunks. "Unknown" and "zero" lead to
        # opposite conclusions and must not render identically.
        reachable, detail = self.storage_reachable()
        if not reachable:
            snapshot = dict(self._status_snapshot or _EMPTY_STATUS)
            configured = list(getattr(self._settings, "projects", None) or [])
            snapshot.update({
                "stale": True,
                "storage_reachable": False,
                "storage_error": detail,
                # None, never 0 — the same distinction `POINTS_UNKNOWN` makes in
                # the migration, applied where the number is actually shown.
                "points_count": None,
                "live_points": None,
                "index_availability": AVAILABILITY_STORAGE_DOWN,
                "collections": [{**c, "points": None}
                                for c in (snapshot.get("collections") or [])],
                # How many projects EXIST does not depend on the store, so it is
                # still known here. How many are searchable is precisely what
                # nobody can say while the store is unreachable.
                "projects_configured": len(configured),
                "projects_enabled": sum(1 for p in configured
                                        if getattr(p, "enabled", True)),
                "projects_searchable": None,
            })
            return snapshot

        acquired = self._lock.acquire(timeout=lock_timeout)
        if not acquired:
            snapshot = dict(self._status_snapshot or _EMPTY_STATUS)
            snapshot["stale"] = True
            snapshot["indexing"] = True
            return snapshot
        try:
            status = self._compute_status()
            self._status_snapshot = status
            return dict(status, stale=False, storage_reachable=True,
                        storage_error="")
        finally:
            self._lock.release()

    def _collection_points(self) -> tuple[int | None, list[dict]]:
        """Point count per collection, and the total across all of them.

        Status must aggregate: with one collection per project, reading only
        ``settings.collection_name`` would report 0 points on a fully indexed
        install.

        The total is ``None`` when ANY collection could not be counted. A
        partial sum presented as the total is the same failure as a zero
        presented as empty — it is a number the caller cannot tell is wrong.
        Each entry carries ``reachable`` so a single broken collection is
        visible instead of merely subtracting from the total.
        """
        per: list[dict] = []
        total = 0
        unknown = False
        for entry in self._router.describe():
            count = self._count_points(entry["name"])
            if count is None:
                unknown = True
            else:
                total += count
            per.append({**entry, "points": count, "reachable": count is not None})
        return (None if unknown else total), per

    def _existing_collections(self) -> set | None:
        """Collection names the store currently holds, or ``None`` if unasked.

        The difference between "the collection is not there" and "the store did
        not answer" is not visible in a count — both make ``_count_points``
        raise — and the two have opposite meanings for a project: its index was
        dropped, versus nobody knows.
        """
        try:
            return {c.name for c in self._client.get_collections().collections}
        except Exception:  # noqa: BLE001 — unknown, and it says so
            return None

    def _searchable_projects(self, per_collection) -> int | None:
        """How many CONFIGURED projects hold live vectors, or ``None``.

        UNKNOWN IS NOT ZERO, twice over:

        * a collection whose count could not be taken does not vote. It is not
          searchable *and* it is not empty — it is unasked;
        * under ``shared`` every project's vectors live in one collection, so
          the inventory cannot attribute them to projects at all. That is
          unknown, not zero, and the per-project answer — which costs a
          payload-filtered count — is :meth:`get_status_projects`.

        Registered-but-unconfigured collections (an archived project the config
        no longer lists) are excluded, so this can never exceed
        ``projects_configured``.
        """
        if not self._router.is_per_project:
            return None
        configured = {p.id for p in (getattr(self._settings, "projects", None) or [])}
        return sum(1 for entry in per_collection
                   if entry.get("project") in configured
                   and (entry.get("points") or 0) > 0)

    def _count_points(self, collection: str) -> int | None:
        """Exact point count for one collection, or ``None`` if it cannot be taken.

        Uses ``count(exact=True)`` rather than ``get_collection().points_count``:
        the latter is an optimizer-maintained estimate that lags recent
        upserts and deletes, so status could report an unchanged total right
        after a file shrank — and it can be ``None`` on a fresh collection,
        which reads as "empty" rather than "unknown".

        UNKNOWN IS NOT ZERO. Swallowing the error and returning 0 is what let a
        dead engine render as a confidently empty index beside a state DB
        reporting 145,906 chunks. ``_availability`` already treats ``None`` as
        ``storage_unavailable``; this function simply has to stop lying to it.
        """
        try:
            return int(self._client.count(collection_name=collection, exact=True).count)
        except Exception:  # noqa: BLE001 — fall back to the estimate, not to a guess
            pass
        try:
            estimate = self._client.get_collection(collection).points_count
        except Exception:  # noqa: BLE001
            return None
        return None if estimate is None else int(estimate)

    def _migration_snapshot(self) -> dict | None:
        """The live migration state, or None when no plan is running."""
        try:
            from ragtools.upgrade import relayout

            plan = relayout.active_plan(self._settings)
            if plan is None:
                return None
            report = relayout.progress(self._settings, plan)
            if report is None:
                return {"plan": plan}
            return {
                "plan": plan, "done": report.done, "total": report.total,
                "blocked": report.blocked, "failed": report.failed,
                "pending": report.pending, "stalled": report.stalled,
                "blocked_reason_recorded": report.blocked_reason or "",
            }
        except Exception:  # noqa: BLE001 — status must never fail on a diagnostic
            return None

    def _compute_status(self) -> dict:
        """Build the status dict. Caller must hold ``self._lock``."""
        points_count, collections = self._collection_points()
        _worst_points, _worst_name, _collection_count = governing_collection(collections)

        state_path = Path(self._settings.state_db)
        if state_path.exists():
            state = IndexState(self._settings.state_db)
            summary = state.get_summary()
            state.close()
        else:
            summary = {"total_files": 0, "total_chunks": 0, "projects": [], "last_indexed": None}

        migration = self._migration_snapshot()
        freshness = compute_index_freshness(
            summary.get("last_indexed"),
            getattr(self._settings, "stale_index_hours", 24),
        )
        configured = list(getattr(self._settings, "projects", None) or [])
        return {
            "points_count": points_count,
            # FOUR QUESTIONS, FOUR NUMBERS, AND THE LABEL SAYS WHICH.
            #
            # `projects` (below, via **summary) is `SELECT DISTINCT project_id
            # FROM file_state` — projects with at least one indexed FILE. The
            # dashboard rendered its length under the word "projects", directly
            # above a table iterating `settings.projects`. On the installed
            # machine that read "14" above a list of 15 and BOTH were correct:
            # one project was configured, enabled, had a real folder, and had
            # lost all 41,832 of its points. Patching the 14 to a 15 would have
            # hidden exactly that.
            "projects_configured": len(configured),
            "projects_enabled": sum(1 for p in configured
                                    if getattr(p, "enabled", True)),
            "projects_indexed": len(summary.get("projects") or []),
            "projects_searchable": self._searchable_projects(collections),
            # THE TWO NUMBERS, NAMED. `points_count` (live) and `total_chunks`
            # (historical, from the state DB) were merged into one flat dict
            # with no stated relationship, and the dashboard rendered the
            # historical one. So a machine whose every collection held zero
            # points advertised "6,546 files · 91,516 chunks" and looked
            # healthy. Both are true; neither means what the other means.
            "live_points": points_count,
            "historical_chunks": summary.get("total_chunks", 0),
            "historical_files": summary.get("total_files", 0),
            "historical_as_of": summary.get("last_indexed"),
            "index_availability": _availability(points_count, summary, migration,
                                                freshness.get("level", "")),
            "migration": migration,
            "index_activity": self.index_activity(),
            # What the index IS, from the router. `settings.collection_name`
            # names no collection under `per_project`, and this field is what
            # the MCP `index_status` tool prints back to the agent as
            # "Collection:" — so an agent was being told the knowledge base was
            # a collection that did not exist. `collections` below is the real
            # inventory; this is the one-line label for it.
            "collection_name": self._router.display_name(),
            "collection_strategy": self._router.strategy,
            "collections": collections,
            "storage": self.storage_info(),
            # The scale ceiling is a property of the ENGINE, not of the data:
            # on a real server there is no brute-force limit to warn about.
            # Per-collection, not the sum: the engine scans each collection
            # separately, so totalling them compares the wrong number against
            # the ceiling and would report "over" forever under per_project.
            "scale": compute_scale_warning(
                _worst_points, capabilities=self.capabilities(),
                collection=_worst_name, collection_count=_collection_count),
            "freshness": freshness,
            **summary,
        }

    def capabilities(self):
        """What the live storage engine actually supports."""
        if self._capabilities is None:
            try:
                from ragtools.storage import resolve_backend
                self._capabilities = resolve_backend(self._settings).capabilities()
            except Exception:  # noqa: BLE001 — unknown engine: assume the weakest
                from ragtools.storage import _EMBEDDED_CAPS
                self._capabilities = _EMBEDDED_CAPS
        return self._capabilities

    #: How long a storage-reachability probe is trusted. /health is polled
    #: frequently; probing the engine on every request would add load without
    #: adding information.
    _STORAGE_PROBE_TTL_S = 5.0

    def storage_reachable(self) -> tuple[bool, str]:
        """Is the vector store actually answering? ``(ok, detail)``, cached.

        ``/health`` used to report ``status: ready`` purely because the process
        was up. With an embedded engine that was almost true — the store is
        in-process. With a managed or external server it is not: the engine can
        die while the service keeps happily answering /health, and every search
        and index then fails against a store nobody said was gone.

        Cheap by construction: one ``get_collections`` behind a short TTL.
        """
        import time as _time

        now = _time.monotonic()
        cached = getattr(self, "_storage_probe", None)
        if cached and now - cached[0] < self._STORAGE_PROBE_TTL_S:
            return cached[1], cached[2]

        try:
            self._client.get_collections()
            result = (True, "")
        except Exception as exc:  # noqa: BLE001 — any failure means unreachable
            result = (False, f"{type(exc).__name__}: {exc}"[:200])
        self._storage_probe = (now, result[0], result[1])
        return result

    def storage_info(self) -> dict:
        """Engine mode + version, for /api/status and diagnostics."""
        caps = self.capabilities()
        return {
            "backend": (getattr(self._settings, "storage_backend", "embedded")
                        or "embedded"),
            "engine_version": caps.server_version,
            "hnsw": caps.hnsw,
            "payload_indexes": caps.payload_indexes,
            "concurrent_readers": caps.concurrent_readers,
        }

    def _emit_scale_warning_after_index(self, log_activity) -> None:
        """Check current collection size and surface a scale warning if needed.

        Called at the end of run_full_index / run_incremental_index so the
        signal appears in the activity log next to the index result, and
        again through the normal logger at WARNING level for service.log.
        Safe to call without the RLock held as callers already hold it.
        """
        try:
            _total, per_collection = self._collection_points()
        except Exception:
            return

        worst, name, count = governing_collection(per_collection)
        record = compute_scale_warning(worst, capabilities=self.capabilities(),
                                       collection=name, collection_count=count)
        if record["level"] == "over":
            logger.warning("[scale=over] %s", record["message"])
            log_activity("warning", "indexer", record["message"])
            self._notify_scale_warning("over", record["message"])
        elif record["level"] == "approaching":
            logger.info("[scale=approaching] %s", record["message"])
            log_activity("info", "indexer", record["message"])
            self._notify_scale_warning("approaching", record["message"])

    def _notify_scale_warning(self, level: str, message: str) -> None:
        """Fire a desktop toast for the scale warning. Best-effort; never raises.

        Kept in a separate method so the shared notifier has a single
        import point and the 1-hour cooldown (defined in notify.py) has a
        deterministic call path to dedupe against.
        """
        try:
            from ragtools.service.notify import notify_scale_warning
            notify_scale_warning(self._settings, level=level, message=message)
        except Exception as e:
            logger.debug("scale-warning toast failed (non-fatal): %s", e)

    def get_projects(self, lock_timeout: float = 0.75) -> list[dict]:
        """Indexed projects with counts. Thread-safe and non-blocking.

        Reads only the state DB, so when the index holds the lock we serve the
        last snapshot rather than stalling the projects table (see get_status).
        """
        acquired = self._lock.acquire(timeout=lock_timeout)
        if not acquired:
            return list(self._projects_snapshot or [])
        try:
            projects = self._compute_projects()
            self._projects_snapshot = projects
            return projects
        finally:
            self._lock.release()

    def _compute_projects(self) -> list[dict]:
        """Caller must hold ``self._lock``."""
        if True:
            state_path = Path(self._settings.state_db)
            if not state_path.exists():
                return []

            state = IndexState(self._settings.state_db)
            summary = state.get_summary()
            projects = []
            for pid in summary["projects"]:
                records = state.get_all_for_project(pid)
                projects.append({
                    "project_id": pid,
                    "files": len(records),
                    "chunks": sum(r["chunk_count"] for r in records),
                })
            state.close()
            return projects

    #: How long an eligibility scan is trusted. The filesystem walk is the whole
    #: cost of this call, the dashboard polls it, and the answer only changes
    #: when files do — at which point the state DB moves the project out of the
    #: branch that needs the scan at all.
    _ELIGIBILITY_TTL_S = 60.0

    def get_status_projects(self, lock_timeout: float = 0.75) -> list[dict]:
        """One row per CONFIGURED project, each carrying the state that names
        its remedy. Thread-safe and **non-blocking**.

        ``get_projects`` answers a different question — it lists projects the
        state DB has rows for — and a project that is configured, enabled, and
        holds nothing appears there not at all. That silence is what the
        dashboard rendered as "14 projects" over a table of 15.

        Rows are ``{id, name, path, enabled, state, files, chunks, points,
        last_indexed, reason}``. ``points`` is the LIVE count and is ``None``
        when it could not be taken — never 0.
        """
        acquired = self._lock.acquire(timeout=lock_timeout)
        if not acquired:
            return [dict(row) for row in (self._status_projects_snapshot or [])]
        try:
            rows = self._compute_status_projects()
        finally:
            self._lock.release()
        # The eligibility scan walks the filesystem, so it runs with the lock
        # RELEASED. Holding it here is the mistake `get_status` documents: one
        # dashboard poll stalling every search on the machine.
        rows = self._explain_unindexed(rows)
        self._status_projects_snapshot = rows
        return rows

    def _compute_status_projects(self) -> list[dict]:
        """Caller must hold ``self._lock``.

        A row whose state cannot be decided without scanning the source is left
        with ``state=None`` for :meth:`_explain_unindexed` to finish. No ``None``
        escapes ``get_status_projects``.
        """
        settings = self._settings
        reachable, _detail = self.storage_reachable()
        per_collection = self._collection_points()[1] if reachable else []
        live = self._project_live_points(per_collection) if reachable else {}

        # Named in the pending rebuild intent = the last rebuild tried this
        # project and could not do it. Its previous index was left exactly as it
        # was, so the counts below may look fine; the remedy is still "read the
        # error", not "wait".
        try:
            from ragtools.service import destructive
            intent = destructive.pending_intent(settings) or {}
            failed = {str(p) for p in (intent.get("failed_projects") or [])}
        except Exception:  # noqa: BLE001 — a diagnostic must never fail the page
            failed = set()

        stale_after = getattr(settings, "stale_index_hours", 24)
        state_path = Path(settings.state_db)
        state = IndexState(settings.state_db) if state_path.exists() else None
        rows: list[dict] = []
        try:
            for project in (settings.projects or []):
                recorded = (state.get_project_summary(project.id) if state
                            else {"files": 0, "chunks": 0, "last_indexed": None})
                files = int(recorded.get("files") or 0)
                chunks = int(recorded.get("chunks") or 0)
                points, collection_points = live.get(project.id, (None, None))
                row = {
                    "id": project.id,
                    "name": getattr(project, "name", "") or project.id,
                    "path": str(getattr(project, "path", "") or ""),
                    "enabled": bool(getattr(project, "enabled", True)),
                    "mode": getattr(project, "mode", None),
                    "files": files,
                    "chunks": chunks,
                    "points": points,
                    "last_indexed": recorded.get("last_indexed"),
                    "state": None,
                    "reason": "",
                }
                rows.append(row)
                self._decide_project_state(
                    row, project=project, points=points,
                    collection_points=collection_points, reachable=reachable,
                    failed=failed, stale_after=stale_after)
        finally:
            if state is not None:
                state.close()
        return rows

    @staticmethod
    def _decide_project_state(row, *, project, points, collection_points,
                              reachable, failed, stale_after) -> None:
        """First match wins, in this order, because several are true at once and
        the REMEDY differs. Mutates ``row`` in place."""
        if not row["enabled"]:
            row["state"] = PROJECT_STATE_DISABLED
            row["reason"] = "Indexing is switched off for this project."
            return

        path = Path(row["path"]) if row["path"] else None
        if path is None or not path.is_dir():
            row["state"] = PROJECT_STATE_PATH_MISSING
            row["reason"] = (f"The configured folder is not there: {row['path']}"
                             if row["path"] else "No folder is configured.")
            return

        # UNKNOWN IS NOT ZERO. A store that could not be asked says nothing
        # about this project's data, and rendering that silence as "empty" is
        # how a dead engine came to look like an empty index.
        if not reachable or collection_points is None or points is None:
            row["state"] = PROJECT_STATE_STORAGE_UNAVAILABLE
            row["reason"] = ("The vector store could not be asked, so this "
                             "project's live count is unknown.")
            return

        if project.id in failed:
            row["state"] = PROJECT_STATE_FAILED
            row["reason"] = ("The last rebuild failed for this project; its "
                             "previous index was left in place.")
            return

        if row["files"] and points > 0:
            fresh = compute_index_freshness(row["last_indexed"], stale_after)
            if fresh.get("level") == "stale":
                row["state"] = PROJECT_STATE_INDEXED_STALE
                row["reason"] = fresh.get("message", "")
            else:
                row["state"] = PROJECT_STATE_INDEXED
            return

        if row["files"]:
            # THE 41,832-POINT CASE. Files and chunks are recorded, the store is
            # answering, and it holds nothing for this project.
            row["state"] = PROJECT_STATE_DRIFTED
            row["reason"] = (
                f"{row['files']:,} file(s) and {row['chunks']:,} chunk(s) are "
                f"recorded, but the store holds no vectors for this project — "
                f"re-index it.")
            return

        # Nothing recorded. Whether that is "there was nothing to index" or
        # "nothing ever landed" can only be answered by the SOURCE, and that
        # costs a filesystem walk. Deferred, deliberately, to outside the lock.
        row["state"] = None

    def _explain_unindexed(self, rows: list[dict]) -> list[dict]:
        """Finish the rows the source has to answer for. Never holds the lock.

        ``no_eligible_files`` and ``never_indexed`` look identical from the
        index — both are zero rows and zero points — and have opposite remedies:
        widen the project's Mode, or run an index. Only the scan tells them
        apart, and it is :func:`~ragtools.upgrade.relayout.indexable_file_count`
        that defines "eligible", so the dashboard and the migration cannot
        disagree about what an empty project is.
        """
        pending = [row["id"] for row in rows if row.get("state") is None]
        if not pending:
            return rows

        note = ""
        cached = self._eligibility_cache
        if (cached is not None
                and time.monotonic() - cached[0] < self._ELIGIBILITY_TTL_S
                and set(pending) <= set(cached[1])):
            counts = cached[1]
        else:
            try:
                from ragtools.upgrade.relayout import indexable_file_counts
                counts = indexable_file_counts(self, pending)
                self._eligibility_cache = (time.monotonic(), counts)
            except Exception as exc:  # noqa: BLE001 — a count nobody could take
                counts = {}
                note = (f"The folder could not be scanned "
                        f"({type(exc).__name__}: {exc}).")

        for row in rows:
            if row.get("state") is not None:
                continue
            count = counts.get(row["id"])
            if count is None:
                row["state"] = PROJECT_STATE_NEVER_INDEXED
                row["reason"] = note or "Nothing has been indexed for this project."
            elif count == 0:
                row["state"] = PROJECT_STATE_NO_ELIGIBLE_FILES
                row["reason"] = ("The folder was scanned and no file matched this "
                                 "project's Mode or its ignore rules.")
            else:
                row["state"] = PROJECT_STATE_NEVER_INDEXED
                row["reason"] = (f"{count:,} file(s) are waiting — this project has "
                                 f"never been indexed.")
        return rows

    def _project_live_points(self, per_collection) -> dict:
        """``{project_id: (project_points, collection_points)}``, both live.

        Two numbers, because only one of them is per-project and they answer
        different questions:

        * ``collection_points`` — what the project's routed collection holds.
          ``None`` means the store could not be asked, which is a verdict about
          STORAGE, not about the project's data.
        * ``project_points`` — what this project holds inside it. Under
          ``per_project`` the collection *is* the project, so they are the same
          number by construction. Under ``shared`` attribution costs a
          payload-filtered count — skipped when the collection is empty, since
          every project's share of nothing is nothing, which keeps the common
          case at one count per poll rather than one per project.
        """
        from ragtools.collection_router import UnknownProject

        by_name = {entry.get("name"): entry.get("points") for entry in per_collection}
        existing = self._existing_collections()
        out: dict = {}
        for project in (getattr(self._settings, "projects", None) or []):
            try:
                name = self._router.write_collection(project.id)
            except UnknownProject:
                # No registry record means no collection, which means there is
                # nothing that could be holding vectors. Provable zero.
                out[project.id] = (0, 0)
                continue
            except Exception:  # noqa: BLE001 — could not resolve => could not ask
                out[project.id] = (None, None)
                continue

            if existing is not None and name not in existing:
                # The store ANSWERED and the collection is not in it. That is a
                # provable zero, and it is how "this project's index was
                # dropped" reads — which must never be confused with "the store
                # could not be asked". `_count_points` cannot tell those apart:
                # both make it raise.
                out[project.id] = (0, 0)
                continue

            counted = by_name[name] if name in by_name else self._count_points(name)
            if counted is None:
                out[project.id] = (None, None)
            elif self._router.is_per_project or counted == 0:
                out[project.id] = (counted, counted)
            else:
                out[project.id] = (self._count_project_points(name, project.id),
                                   counted)
        return out

    def update_settings(self, **kwargs) -> None:
        """Hot-reload mutable settings in the running service. Thread-safe."""
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._settings, key):
                    object.__setattr__(self._settings, key, value)
            logger.info("Settings updated: %s", list(kwargs.keys()))
            from ragtools.service.activity import log_activity
            log_activity("info", "config", f"Settings updated: {', '.join(kwargs.keys())}")

    def update_projects(self, projects: list) -> None:
        """Hot-reload project configuration. Thread-safe."""
        with self._lock:
            from ragtools.config import CONFIG_VERSION

            object.__setattr__(self._settings, "projects", projects)
            # Hot-reloading projects does not change the schema version, and
            # asserting `2` here made the in-memory Settings disagree with a
            # freshly migrated file on disk.
            object.__setattr__(self._settings, "config_version", CONFIG_VERSION)
            logger.info("Projects updated: %d configured", len(projects))
            from ragtools.service.activity import log_activity
            log_activity("info", "config", f"Projects reloaded: {len(projects)} configured")

    def reindex_project(self, project_id: str) -> dict:
        """Drop a project's chunks + state rows and re-index from scratch.

        Composes ``delete_project_data`` (which backs up the state DB first)
        with ``run_full_index(project_id=X)``. The delete step is atomic per
        project — other projects are untouched.
        """
        # The inner calls both grab ``self._lock``; we don't hold it here.
        deleted = self.delete_project_data(project_id)
        stats = self.run_full_index(project_id=project_id)
        return {
            "project_id": project_id,
            "deleted_files": deleted.get("files_deleted", 0),
            **stats,
        }

    def delete_project_data(self, project_id: str) -> dict:
        """Delete all indexed data for a project from Qdrant and state DB. Thread-safe."""
        with self._lock:
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            # Snapshot the state DB before wiping this project's rows.
            try:
                from ragtools.backup import backup_state_db, prune_backups
                backup_state_db(self._settings, trigger="project_remove",
                                note=f"project={project_id}")
                prune_backups(self._settings)
            except Exception as e:
                logger.warning("Pre-remove backup failed (non-fatal): %s", e)

            # Delete from Qdrant. In per-project mode the project owns its whole
            # collection, so drop it outright — a payload-filtered delete would
            # leave an empty collection behind for every removed project. The
            # payload filter is still correct (and still needed) in shared mode.
            try:
                if self._router.is_per_project:
                    coll = self._router.write_collection(project_id)
                    try:
                        self._client.delete_collection(collection_name=coll)
                    finally:
                        self._ensured_collections.discard(coll)
                else:
                    self._client.delete(
                        collection_name=self._router.shared_collection,
                        points_selector=Filter(
                            must=[FieldCondition(key="project_id",
                                                 match=MatchValue(value=project_id))]
                        ),
                    )
            except Exception as e:
                logger.warning("Failed to delete Qdrant data for project %s: %s", project_id, e)

            # Delete from state DB
            deleted_files = 0
            state_path = Path(self._settings.state_db)
            if state_path.exists():
                state = IndexState(self._settings.state_db)
                records = state.get_all_for_project(project_id)
                for r in records:
                    state.remove(r["file_path"])
                    deleted_files += 1
                # The project's recorded mapping goes with its file rows. A
                # DELIBERATE removal must not later read as a registry that lost
                # a project — that is the rollback signal, and a routine removal
                # firing it would block swaps for an install with nothing wrong.
                from ragtools.index_identity import forget_project
                forget_project(state, project_id)
                state.close()

            self._invalidate_map_cache()

            from ragtools.service.activity import log_activity
            log_activity("warning", "indexer", f"Project data deleted: {project_id} ({deleted_files} files)")
            logger.info("Deleted data for project %s: %d files", project_id, deleted_files)
            return {"project_id": project_id, "files_deleted": deleted_files}

    def _scan_files(self, project_id: str | None = None) -> list[tuple[str, Path]]:
        """Scan files from configured projects.

        Always passes ALL projects to the scanner so nested path scoping
        works correctly (parent excludes child project files). Filters
        results by project_id afterward if requested.
        """
        if project_id and not any(p.id == project_id for p in self._settings.enabled_projects):
            raise ValueError(f"Project '{project_id}' not found in configuration")

        # Pass all projects (including disabled) so scanner detects nested path overlaps
        all_files = scan_configured_projects(
            self._settings.projects,
            global_ignore_patterns=self._settings.ignore_patterns,
            use_ragignore=self._settings.use_ragignore_files,
            secret_allowlist=self._settings.secret_allowlist,
            # Without the catalog, a project that LINKS a dependency (rather
            # than typing its path) has that tree scanned into its own
            # collection as well as the shared corpus — every hit twice.
            dependencies=self._settings.dependencies,
        )

        if project_id:
            return [(pid, fp) for pid, fp in all_files if pid == project_id]
        return all_files

    def _resolve_relative_path(self, project_id: str, file_path: Path) -> str:
        """Compute the storage-relative path for a file."""
        project = next(
            (p for p in self._settings.projects if p.id == project_id), None
        )
        if project:
            return get_project_relative_path(file_path, project.path, project.id)
        return f"{project_id}/{file_path.name}"

    def _request_map_refresh_if_stale(self) -> None:
        """Queue a background map recompute when the cache is stale.

        Never recomputes inline, never runs while another job holds the worker —
        the job engine serialises it behind any in-flight index.
        """
        try:
            from ragtools.service.map_data import is_map_cache_stale
            if not is_map_cache_stale(self._settings.state_db):
                return
            from ragtools.service.app import get_runtime
            get_runtime().submit("map_recompute", {}, idempotency_key="map-recompute")
        except Exception:
            pass  # best-effort; the map simply stays stale until next time

    def _invalidate_map_cache(self) -> None:
        """Invalidate the Semantic Map cache. Called after index changes."""
        try:
            from ragtools.service.map_data import invalidate_map_cache
            invalidate_map_cache(self._settings.state_db)
        except Exception:
            pass  # Non-critical

    def close(self):
        """Release the Qdrant client and the registry connections.

        The registries hold SQLite handles; on Windows an unclosed handle keeps
        the .db file locked, so skipping them leaves files that cannot be
        deleted or replaced on restart.
        """
        for registry in (self._registry, self._frameworks):
            if registry is not None:
                try:
                    registry.close()
                except Exception:  # noqa: BLE001
                    pass
        self._registry = None
        self._frameworks = None
        try:
            del self._client
        except Exception:
            pass
        logger.info("QdrantOwner closed")
