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


def get_owner() -> QdrantOwner:
    """Get the QdrantOwner singleton. Raises if not initialized."""
    if _owner is None:
        raise RuntimeError("Service not initialized")
    return _owner


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
    global _owner, _settings

    if _owner is not None:
        # Already initialized (e.g., by test injection)
        logger.info("Service using pre-initialized owner")
        yield
        return

    _settings = Settings()
    logger.info("Starting RAGTools service on %s:%d", _settings.service_host, _settings.service_port)

    # This takes 5-10 seconds (encoder loading)
    logger.info("Loading encoder model: %s", _settings.embedding_model)
    _owner = QdrantOwner(_settings)
    logger.info("Service ready")
    from ragtools.service.activity import log_activity
    log_activity("success", "service", f"Service ready on {_settings.service_host}:{_settings.service_port}")

    yield

    # Shutdown
    logger.info("Shutting down service")
    log_activity("info", "service", "Service shutting down")
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

    from ragtools.service.routes import router
    app.include_router(router)

    from ragtools.service.pages import page_router
    app.include_router(page_router)

    # Static files
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app
