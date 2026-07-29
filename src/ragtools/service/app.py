"""FastAPI application factory with lifecycle management."""

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ragtools.config import Settings
from ragtools.service.owner import QdrantOwner

logger = logging.getLogger("ragtools.service")

# Module-level state — set during lifespan
_owner: QdrantOwner | None = None
_settings: Settings | None = None
_shutdown_event: threading.Event = threading.Event()
_runtime = None          # RuntimeStore — durable jobs + events
_engine = None           # EngineLifecycle — owns the managed engine for its whole life
_job_worker = None       # JobWorker — drains the job queue
_maintenance = None      # MaintenanceScheduler — the periodic table, finally running

#: Why the configured engine is not the one actually running, or "" when it is.
#:
#: This existed as a local variable that was assigned, logged once, and dropped.
#: So a machine configured for `managed` that silently fell back to `embedded`
#: reported `storage_backend: "embedded"` on /health — indistinguishable from a
#: machine deliberately configured that way, with an index that looks simply
#: empty. The fallback is now a MORE likely path (an occupied port is refused
#: rather than adopted), which makes saying why load-bearing.
_storage_degraded: str = ""


def storage_degradation() -> str:
    """Why the running engine differs from the configured one ("" if it doesn't)."""
    return _storage_degraded


def get_engine():
    """The managed-engine lifecycle, or None when storage is embedded/external."""
    return _engine


def engine_status() -> dict | None:
    """The cached engine snapshot every status surface reads.

    ``None`` means "no managed engine in this configuration", which is a
    different fact from "the engine is down" and must not render as one.
    """
    engine = _engine
    if engine is None:
        return None
    return engine.status.as_dict()


def storage_is_down() -> str:
    """Why storage is unusable right now, or "" when it is usable.

    The ONE question every destructive operation and every status page asks.
    Answered from cached lifecycle state, never by reaching for the engine — a
    guard that itself blocks on a dead engine is not a guard.
    """
    engine = _engine
    if engine is None:
        return ""
    from ragtools.service.engine_lifecycle import DOWN_STATES

    status = engine.status
    if status.state in DOWN_STATES:
        return status.detail or f"the managed engine is {status.state}"
    return ""


def _on_engine_state(status) -> None:
    """React to an engine transition: park the migration, resume it on recovery.

    Registered by the lifespan so the lifecycle component stays free of product
    policy — it reports state; what the product does about it lives here.
    """
    from ragtools.service.engine_lifecycle import CRASHED, READY

    if status.state == CRASHED:
        _park_migration(status.detail or "the managed engine exited")
    elif status.state == READY and status.restart_attempt:
        resume_migration()


def _park_migration(reason: str) -> None:
    """Persist "storage went away" into the migration plan, immediately.

    Durable and immediate, because the alternative is what v3.2.0 did: keep
    scanning, chunking and embedding a project whose every write will be refused,
    then rediscover the outage one exception at a time.
    """
    try:
        from ragtools.upgrade import relayout

        settings = _settings
        if settings is None:
            return
        plan = relayout.active_plan(settings)
        if plan is None:
            return
        parked = relayout.block_all(settings, plan, reason)
        logger.error("Relayout plan %s: %d unit(s) parked because storage went "
                     "away (%s)", plan, parked, reason)
    except Exception:  # noqa: BLE001
        logger.exception("could not park the migration after an engine crash")


def resume_migration(*, reset: bool = False) -> bool:
    """Continue a parked migration. Returns whether one was started.

    On its own thread: a rebuild can run for hours, and it must not execute
    inside the engine-watcher thread whose whole job is to notice the next crash,
    nor inside an HTTP request.

    Public because the SERVICE is the only process that can do this on a managed
    installation — the engine belongs to it. ``rag upgrade --resume`` refuses
    while the service is up and cannot construct a client while it is down, so
    without this entry point the remedy the product advertises has nowhere to run.
    """
    settings, owner = _settings, _owner
    if settings is None or owner is None:
        return False
    try:
        from ragtools.upgrade import relayout

        if relayout.active_plan(settings) is None:
            return False
    except Exception:  # noqa: BLE001
        return False

    def _run():
        try:
            from ragtools.upgrade import relayout

            plan = relayout.active_plan(settings)
            if plan is None:
                return
            logger.info("Relayout plan %s: resuming (reset=%s)", plan, reset)
            report = relayout.run_pending(owner, settings, plan_id=plan,
                                          reset=reset)
            logger.info("Relayout after resume: %s", report.describe())
        except Exception:  # noqa: BLE001
            logger.exception("could not resume the migration")

    threading.Thread(target=_run, name="relayout-resume", daemon=True).start()
    return True


def migration_remedy(settings) -> str:
    """The retry instruction that actually works on THIS installation.

    ``/health`` advertised ``rag upgrade --resume`` unconditionally. On a managed
    installation that command cannot run in either state: it refuses while the
    service is up ("the service owns the store"), and with the service down the
    engine is down too, so building a client raises — ``storage_url`` is only ever
    set in-process at service startup and is never persisted. Telling every user
    to run a command that cannot work is worse than telling them nothing.
    """
    backend = (getattr(settings, "storage_backend", "embedded") or "embedded").lower()
    if backend == "embedded":
        return "rag upgrade --resume"
    return ("rag upgrade --resume (forwarded to the service), or restart the "
            "service — it resumes automatically on start")


def get_owner() -> QdrantOwner:
    """Get the QdrantOwner singleton. Raises if not initialized."""
    if _owner is None:
        raise RuntimeError("Service not initialized")
    return _owner


def get_runtime():
    """The durable job/event store. Raises if the service is not initialized."""
    if _runtime is None:
        raise RuntimeError("Runtime store not initialized")
    return _runtime


def get_job_worker():
    return _job_worker


def start_runtime(settings, instance_id: str = ""):
    """Open `runtime.db`, recover interrupted jobs, and start the worker.

    Recovery runs BEFORE the worker starts, so jobs left active by a previous
    process are marked `interrupted` rather than being silently forgotten —
    this is what makes "the window was closed mid-index" answerable.
    """
    global _runtime, _job_worker
    from pathlib import Path as _P

    from ragtools.job_worker import JobWorker
    from ragtools.runtime_store import RuntimeStore
    from ragtools.service.job_handlers import make_handlers

    _runtime = RuntimeStore(str(_P(settings.data_dir) / "runtime.db"),
                            instance_id=instance_id)
    recovered = _runtime.recover_interrupted()
    if recovered:
        from ragtools.service.activity import log_activity
        log_activity("warning", "service",
                     f"{len(recovered)} job(s) were interrupted by a restart")

    # Make every existing log_activity() call site durable — zero call-site churn.
    from ragtools.service.activity import set_event_sink

    def _sink(level, source, message, details):
        store = _runtime
        if store is None:
            return
        store.append_event(f"activity.{level}", source,
                           {"message": message, "details": details})

    set_event_sink(_sink)

    _job_worker = JobWorker(_runtime, make_handlers(get_owner))
    _job_worker.start()
    return _runtime


def stop_runtime():
    global _runtime, _job_worker
    try:
        from ragtools.service.activity import set_event_sink
        set_event_sink(None)
    except Exception:
        pass
    if _job_worker is not None:
        try:
            _job_worker.stop()
        except Exception:
            logger.exception("job worker stop failed")
        _job_worker = None
    if _runtime is not None:
        try:
            _runtime.close()
        except Exception:
            pass
        _runtime = None


def get_settings() -> Settings:
    """Get the service Settings."""
    if _settings is None:
        raise RuntimeError("Service not initialized")
    return _settings


def get_shutdown_event() -> threading.Event:
    """Get the shutdown event."""
    return _shutdown_event


#: How often the maintenance table is examined. Tasks decide their own
#: intervals; this only bounds how late one can be.
MAINTENANCE_TICK_SECONDS = 30.0


def get_maintenance():
    return _maintenance


def start_maintenance(owner, runtime=None):
    """Run the maintenance table. It has never run before.

    `MaintenanceScheduler` and `build_default_tasks` shipped with thirteen
    passing tests and were referenced from nowhere in `src/` — the storage probe,
    the stale-job recovery and the count reconciliation were all dead code with
    full coverage. That is the failure mode this project's own lessons warn
    about, and it is why a structural test has to be shown to fail before it is
    trusted.

    Every task is exception-isolated by the scheduler, and index-locking tasks
    skip rather than wait, so a long rebuild cannot be blocked by maintenance.
    """
    global _maintenance
    import time as _time

    from ragtools.service.maintenance import MaintenanceScheduler, build_default_tasks

    scheduler = MaintenanceScheduler(
        build_default_tasks(owner, runtime=runtime),
        clock=_time.monotonic,
        lock_held=lambda: owner.indexing,
    )

    def _loop():
        while not _shutdown_event.wait(MAINTENANCE_TICK_SECONDS):
            try:
                scheduler.tick()
            except Exception:  # noqa: BLE001 — one bad tick must not end the loop
                logger.exception("maintenance tick failed")

    threading.Thread(target=_loop, name="maintenance", daemon=True).start()
    _maintenance = scheduler
    return scheduler


def stop_background_writers() -> None:
    """Stop the watcher, THEN the store it writes to. Order is the whole point.

    The watcher is a daemon thread that logs activity continuously, and every
    `log_activity` reaches the runtime store. Closing the store while it is
    mid-write freed the sqlite3 connection underneath a C-level `execute` —
    `Fatal Python error: Segmentation fault`, exit 139, on a Linux CI runner.

    It lives in one function because `lifespan` has TWO shutdown paths and only
    one of them was ever going to be remembered. The injected-owner branch —
    the one every service test takes — called `stop_runtime()` alone, which is
    precisely the path the crash was observed on.
    """
    try:
        from ragtools.service.routes import stop_watcher_for_shutdown

        stop_watcher_for_shutdown()
    except Exception:
        logger.exception("watcher stop during shutdown failed (non-fatal)")
    stop_runtime()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: load encoder + open Qdrant. Shutdown: close client."""
    global _owner, _settings, _engine, _storage_degraded

    if _owner is not None:
        # Already initialized (e.g., by test injection). The job engine still
        # starts, so injected-owner tests exercise the same code path the
        # service does.
        logger.info("Service using pre-initialized owner")
        try:
            start_runtime(_settings or _owner.settings)
        except Exception:
            logger.exception("runtime store startup failed (non-fatal)")
        try:
            yield
        finally:
            stop_background_writers()
        return

    # Bring the configuration to the current schema BEFORE anything reads it.
    # Ordering is the whole point: QdrantOwner opens the store and creates
    # collections while constructing itself, so a migration that ran later
    # would already be looking at a store built to the previous layout.
    from ragtools.bootstrap import ensure_config_current_once
    _bootstrap = ensure_config_current_once()

    _settings = Settings()
    logger.info("Starting RAGTools service on %s:%d", _settings.service_host, _settings.service_port)
    logger.info("Config: %s", _bootstrap.describe())

    # Managed Qdrant must be up BEFORE the owner connects, because the owner
    # resolves its client through the storage backend. If it cannot start
    # (unsupported platform, no binary), we fall back to embedded and say why —
    # the service always boots.
    try:
        from ragtools.service.managed_qdrant import plan_managed_startup, start_managed_qdrant

        plan = plan_managed_startup(_settings)
        if plan.should_start:
            # The engine is owned by a lifecycle component, not by a variable
            # nobody reads again. It starts the process, blocks a thread on the
            # child, logs the exit code the instant it exists, invalidates the
            # ownership manifest, parks the migration and restarts with bounded
            # backoff. v3.2.0 had the handle and none of the behaviour.
            from ragtools.service.engine_lifecycle import EngineLifecycle

            _engine = EngineLifecycle(
                _settings,
                starter=lambda s: start_managed_qdrant(s, plan_managed_startup(s)),
                on_state_change=_on_engine_state,
            )
            supervisor, url = _engine.start()
            if supervisor is not None:
                object.__setattr__(_settings, "storage_url", url)
                # The engine credential must reach the client, or every request
                # to our own authenticated engine is rejected. This is the other
                # half of the ownership proof: the key makes "is this engine
                # mine?" an authenticated fact rather than an inference.
                if getattr(plan, "api_key", None):
                    object.__setattr__(_settings, "storage_api_key", plan.api_key)
                logger.info("Storage: managed Qdrant at %s", url)
            else:
                # DROP THE LIFECYCLE when we fall back. It is still parked in
                # `stopped`, and `storage_is_down()` reads that state — so
                # keeping it would report the EMBEDDED store as unavailable and
                # refuse every rebuild on a machine whose storage is perfectly
                # fine. `None` means "no managed engine here", which is the
                # truth after a fallback.
                _engine = None
                object.__setattr__(_settings, "storage_backend", "embedded")
                _storage_degraded = (
                    "configured for managed storage, but the engine could not be "
                    "started or was not ours; using embedded. See the service log "
                    "for the specific refusal.")
                logger.warning("Storage degraded to embedded: %s", _storage_degraded)
        elif plan.fallback_to_embedded:
            object.__setattr__(_settings, "storage_backend", "embedded")
            _storage_degraded = plan.reason
            logger.warning("Storage degraded to embedded: %s", plan.reason)
    except Exception as exc:
        logger.exception("managed storage startup failed; continuing with embedded")
        _engine = None          # see the fallback branch above
        _storage_degraded = f"managed storage startup raised: {exc}"
        try:
            object.__setattr__(_settings, "storage_backend", "embedded")
        except Exception:
            pass

    # This takes 5-10 seconds (encoder loading)
    logger.info("Loading encoder model: %s", _settings.embedding_model)
    _owner = QdrantOwner(_settings)
    # Stop indexing the moment storage goes away, rather than one refused write
    # at a time. Checked at progress boundaries, so it costs nothing when the
    # engine is healthy and loses at most one committed window when it is not.
    #
    # `getattr`, because a test may inject an owner double. Requiring every
    # double to grow a new method is how a diagnostic becomes the reason the
    # service will not boot.
    _gate = getattr(_owner, "set_storage_gate", None)
    if callable(_gate):
        _gate(storage_is_down)
    logger.info("Service ready")
    from ragtools.service.activity import log_activity
    log_activity("success", "service", f"Service ready on {_settings.service_host}:{_settings.service_port}")

    # Durable job/event store + worker. Interrupted jobs are recovered here.
    try:
        start_runtime(_settings)
    except Exception:
        logger.exception("runtime store startup failed (non-fatal)")

    # M3: own the watcher's startup here, in the service lifecycle, rather than
    # relying solely on run.py's delayed HTTP self-POST (which could miss the
    # readiness window and leave the watcher silently inactive). Idempotent and
    # never fatal — a construct/start failure is recorded and surfaced via
    # /health degraded + /api/watcher/status state, not raised.
    try:
        from ragtools.service.routes import autostart_watcher
        result = autostart_watcher()
        logger.info("Watcher autostart: %s", result.get("status"))
    except Exception:
        logger.exception("Watcher autostart call failed (non-fatal)")

    try:
        start_maintenance(_owner, runtime=_runtime)
        logger.info("Maintenance scheduler started")
    except Exception:
        logger.exception("maintenance scheduler failed to start (non-fatal)")

    yield

    # Shutdown
    logger.info("Shutting down service")
    log_activity("info", "service", "Service shutting down")

    stop_background_writers()
    if _engine is not None:
        # Attributed teardown, and — decisively — INTENT-FIRST. `request_stop`
        # sets the stopping flag before it signals anything, so the watcher
        # thread reads this exit as one we asked for rather than as a crash. Get
        # that ordering wrong and shutting the service down starts a restart
        # storm against an engine that is on its way out.
        #
        # `release` still refuses to signal anything the manifest does not vouch
        # for: killing on a port, or on an image name, is how one installation
        # kills another installation's database.
        try:
            logger.info("Managed Qdrant shutdown: %s", _engine.request_stop())
        except Exception:
            logger.exception("managed Qdrant stop failed")
        _engine = None
    if _owner:
        _owner.close()
    _owner = None
    _settings = None


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    app = FastAPI(
        title="RAGTools Service",
        description="Local Markdown RAG service",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Guarantee JSON on an uncaught error. The HTTP-API contract documents
    # non-200 bodies as JSON; without this, Starlette returns plain-text
    # "Internal Server Error" on an unhandled 500. Explicit HTTPException
    # responses already render JSON via FastAPI's default handler.
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.exception_handler(Exception)
    async def _json_error_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

    from ragtools.service.routes import router
    app.include_router(router)

    from ragtools.service.pages import page_router
    app.include_router(page_router)

    # Static files
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app
