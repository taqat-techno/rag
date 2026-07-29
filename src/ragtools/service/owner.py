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
    recreate_collection,
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

#: Shape returned when status is requested while indexing owns the lock and no
#: snapshot has been taken yet. Never blocks the caller.
_EMPTY_STATUS = {
    "points_count": 0, "collection_name": "", "total_files": 0, "total_chunks": 0,
    "projects": [], "last_indexed": None,
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


def _availability(live_points: int | None, summary: dict, migration: dict | None) -> str:
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
        return AVAILABILITY_READY
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

    def _check_index_identity(self, state) -> tuple[bool, list[str]]:
        """Whether ``state``'s file hashes may be trusted for skipping."""
        from ragtools.index_identity import current_identity, reconcile

        try:
            identity = current_identity(self._settings, self._encoder.dimension)
            return reconcile(state, identity)
        except Exception:  # noqa: BLE001 — never block indexing on the guard
            logger.exception("index-identity check failed; assuming trustworthy")
            return True, []

    def _stamp_index_identity(self) -> None:
        """Record the current store on the state DB, after a run has written it."""
        from ragtools.index_identity import current_identity, stamp

        try:
            state = IndexState(self._settings.state_db)
            try:
                stamp(state, current_identity(self._settings, self._encoder.dimension))
            finally:
                state.close()
        except Exception:  # noqa: BLE001
            logger.exception("could not record the index identity")

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

    def get_map_points(self, force_recompute: bool = False) -> list[dict]:
        """Get 2D map coordinates for all indexed files. Uses cache when valid. Thread-safe."""
        with self._lock:
            from ragtools.service.map_data import (
                compute_map_points, load_cached_map, save_map_cache, invalidate_map_cache,
            )

            if not force_recompute:
                # Serve whatever we have IMMEDIATELY (including a stale map) and
                # let a background job refresh it. Recomputing on the request
                # path meant scrolling every point + PCA while the machine was
                # already busy indexing.
                cached = load_cached_map(self._settings.state_db, allow_stale=True)
                if cached is not None:
                    self._request_map_refresh_if_stale()
                    return cached

            points = compute_map_points(
                self._client, self._settings, self._router.all_collections()
            )
            save_map_cache(self._settings.state_db, points)
            return points

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
        self._stamp_index_identity()
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
                # `trustworthy` is False when the state DB describes a different
                # store: the hash may match while the vectors do not exist.
                if trustworthy and not read_state.file_changed(relative_path, current_hash):
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
        # migration look complete on the next run.
        self._stamp_index_identity()
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

        # Snapshot the state DB before we drop it. Best-effort — failures
        # here must not block the rebuild itself (disk full, etc.).
        try:
            from ragtools.backup import backup_state_db, prune_backups
            backup_state_db(self._settings, trigger="rebuild")
            prune_backups(self._settings)
        except Exception as e:
            logger.warning("Pre-rebuild backup failed (non-fatal): %s", e)

        destructive.record_intent(self._settings, {
            "operation": "rebuild", "collections": targets,
            "state_db": str(state_path),
        })
        try:
            # Force-drop and recreate EVERY collection (clean slate). In
            # per-project mode a rebuild that only cleared the shared collection
            # would leave every project's vectors in place and silently double
            # them on the re-index that follows.
            for name in targets:
                recreate_collection(self._client, name, self._encoder.dimension)
            self._ensured_collections = set(targets)

            # THE IRREVERSIBLE STEP IS GATED ON PROOF, NOT ON THE ABSENCE OF AN
            # EXCEPTION. "recreate_collection did not raise" is a weaker claim
            # than "the collection is there", and the state DB is the only
            # record of what was indexed — once it is gone, a half-recreated
            # store cannot even be diagnosed.
            existing = {c.name for c in self._client.get_collections().collections}
            missing = [n for n in targets if n not in existing]
            if missing:
                raise RuntimeError(
                    f"refusing to delete the index state: {len(missing)} "
                    f"collection(s) were not recreated ({', '.join(missing[:5])})")

            if state_path.exists():
                state_path.unlink()

            log_activity("info", "indexer", "Rebuild started — all data dropped")
            stats = self._run_full_index_inner()
            self._invalidate_map_cache()
            self._stamp_index_identity()
            logger.info("Rebuild complete: %s", stats)
            return stats
        finally:
            destructive.clear_intent(self._settings)

    def _run_full_index_inner(self, project_id: str | None = None) -> dict:
        """Full index without acquiring lock (called from within locked context)."""
        state = IndexState(self._settings.state_db)

        files = self._scan_files(project_id)
        stats = {"files_indexed": 0, "chunks_indexed": 0, "projects": set()}

        for pid, file_path in files:
            relative_path = self._resolve_relative_path(pid, file_path)
            file_hash = IndexState.hash_file(file_path)
            count = index_file(
                client=self._client,
                encoder=self._encoder,
                collection_name=self._write_collection(pid),
                project_id=pid,
                file_path=file_path,
                relative_path=relative_path,
                chunk_size=self._settings.chunk_size,
                chunk_overlap=self._settings.chunk_overlap,
            )
            state.update(relative_path, pid, file_hash, count)
            stats["files_indexed"] += 1
            stats["chunks_indexed"] += count
            stats["projects"].add(pid)

        state.close()
        stats["projects"] = sorted(stats["projects"])
        logger.info("Full index: %d files, %d chunks", stats["files_indexed"], stats["chunks_indexed"])
        return stats

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

    def _collection_points(self) -> tuple[int, list[dict]]:
        """Point count per collection, and the total across all of them.

        Status must aggregate: with one collection per project, reading only
        ``settings.collection_name`` would report 0 points on a fully indexed
        install.
        """
        per: list[dict] = []
        total = 0
        for entry in self._router.describe():
            name = entry["name"]
            total += (count := self._count_points(name))
            per.append({**entry, "points": count})
        return total, per

    def _count_points(self, collection: str) -> int:
        """Exact point count for one collection.

        Uses ``count(exact=True)`` rather than ``get_collection().points_count``:
        the latter is an optimizer-maintained estimate that lags recent
        upserts and deletes, so status could report an unchanged total right
        after a file shrank — and it can be ``None`` on a fresh collection,
        which reads as "empty" rather than "unknown".
        """
        try:
            return int(self._client.count(collection_name=collection, exact=True).count)
        except Exception:  # noqa: BLE001 — a missing collection is 0, not fatal
            try:
                return int(self._client.get_collection(collection).points_count or 0)
            except Exception:  # noqa: BLE001
                return 0

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
        return {
            "points_count": points_count,
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
            "index_availability": _availability(points_count, summary, migration),
            "migration": migration,
            "index_activity": self.index_activity(),
            "collection_name": self._settings.collection_name,
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
            "freshness": compute_index_freshness(
                summary.get("last_indexed"),
                getattr(self._settings, "stale_index_hours", 24),
            ),
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
