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
_managed_qdrant = None   # QdrantSupervisor — the managed engine process, if any
_job_worker = None       # JobWorker — drains the job queue


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: load encoder + open Qdrant. Shutdown: close client."""
    global _owner, _settings, _managed_qdrant

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
            stop_runtime()
        return

    _settings = Settings()
    logger.info("Starting RAGTools service on %s:%d", _settings.service_host, _settings.service_port)

    # Managed Qdrant must be up BEFORE the owner connects, because the owner
    # resolves its client through the storage backend. If it cannot start
    # (unsupported platform, no binary), we fall back to embedded and say why —
    # the service always boots.
    try:
        from ragtools.service.managed_qdrant import plan_managed_startup, start_managed_qdrant

        plan = plan_managed_startup(_settings)
        if plan.should_start:
            supervisor, url = start_managed_qdrant(_settings, plan)
            if supervisor is not None:
                _managed_qdrant = supervisor
                object.__setattr__(_settings, "storage_url", url)
                logger.info("Storage: managed Qdrant at %s", url)
            else:
                object.__setattr__(_settings, "storage_backend", "embedded")
                log_degraded = "managed Qdrant failed to start; using embedded storage"
                logger.warning(log_degraded)
        elif plan.fallback_to_embedded:
            object.__setattr__(_settings, "storage_backend", "embedded")
            logger.warning("Storage degraded to embedded: %s", plan.reason)
    except Exception:
        logger.exception("managed storage startup failed; continuing with embedded")
        try:
            object.__setattr__(_settings, "storage_backend", "embedded")
        except Exception:
            pass

    # This takes 5-10 seconds (encoder loading)
    logger.info("Loading encoder model: %s", _settings.embedding_model)
    _owner = QdrantOwner(_settings)
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

    yield

    # Shutdown
    logger.info("Shutting down service")
    log_activity("info", "service", "Service shutting down")
    stop_runtime()
    if _managed_qdrant is not None:
        try:
            _managed_qdrant.stop()
            logger.info("Managed Qdrant stopped")
        except Exception:
            logger.exception("managed Qdrant stop failed")
        _managed_qdrant = None
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
