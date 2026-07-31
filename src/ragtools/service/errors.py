"""Domain conditions, rendered as HTTP a caller can act on.

``/api/search`` returned **HTTP 500 `{"detail": "Internal Server Error"}``** on a
machine whose migration was still running. Not because anything was wrong — the
service understood the situation completely, raised a purpose-built
``MigrationInProgress`` carrying a full progress report, and then let it fall
through to the blanket handler in ``create_app`` that turns anything unhandled
into a 500 with the text stripped off.

The same condition was already handled correctly one interface over
(``mcp_server`` catches it), so an agent got a readable explanation and the HTTP
API got "Internal Server Error". Two interfaces, one condition, opposite answers.

Handlers are registered per exception TYPE rather than per route on purpose. A
per-route ``try`` is a thing to forget on the next route, and the route that
mattered here is the one nobody remembered.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("ragtools.service")

#: HTTP semantics, chosen once.
#:
#: 409 for "the state of this resource forbids it, and you can see why" —
#: a migration in flight, a conflicting operation. 503 for "a dependency is
#: down, come back" — storage gone, engine restarting, model missing. Neither is
#: a 500: a 500 says the server has no idea what happened, and in every case
#: here it knows exactly.
#:
#: 404 for "you named something that does not exist" — the caller's input, not
#: the server's failure. ``UnknownProject`` is raised deliberately by the
#: collection router (falling back to the shared collection would be a
#: cross-project read), so it is a designed refusal reaching the wire, and
#: ``/api/map/points?project=<typo>`` answered 500 for it.
MIGRATION_IN_PROGRESS = "MIGRATION_IN_PROGRESS"
MIGRATION_BLOCKED = "MIGRATION_BLOCKED"
OPERATION_CONFLICT = "OPERATION_CONFLICT"
STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
UNKNOWN_PROJECT = "UNKNOWN_PROJECT"


def _engine_snapshot() -> dict | None:
    try:
        from ragtools.service.app import engine_status

        return engine_status()
    except Exception:  # noqa: BLE001 — an error response must never raise
        return None


def _remedy() -> str:
    try:
        from ragtools.service.app import get_settings, migration_remedy

        return migration_remedy(get_settings())
    except Exception:  # noqa: BLE001
        # Deliberately not "restart the service". The rebuild is resumed by the
        # service on a timer while attempts remain, and a restart adds nothing a
        # retry has not already done — it only discards the running diagnosis.
        return "rag upgrade --resume — the service also retries automatically"


def migration_payload(exc) -> dict:
    """The body for a migration condition, built from the report it carries."""
    report = getattr(exc, "report", None)
    body: dict = {
        "error": MIGRATION_IN_PROGRESS,
        "message": str(exc),
        "remediation": _remedy(),
    }
    if report is not None:
        blocked = int(getattr(report, "blocked", 0) or 0)
        done = int(getattr(report, "done", 0) or 0)
        body.update({
            "error": MIGRATION_BLOCKED if blocked and not done else MIGRATION_IN_PROGRESS,
            "plan": getattr(report, "plan_id", None),
            "done": done,
            "total": int(getattr(report, "total", 0) or 0),
            "blocked": blocked,
            "failed": int(getattr(report, "failed", 0) or 0),
            "pending": int(getattr(report, "pending", 0) or 0),
            # Reported as what it is: the reason recorded WHEN the block was
            # written, which may no longer describe the world. Presenting a
            # two-hour-old `WinError 10061` as current state is how a health
            # payload loses its credibility.
            "blocked_reason_recorded": getattr(report, "blocked_reason", "") or "",
            "stalled": bool(getattr(report, "stalled", False)),
        })
    try:
        from ragtools.service.app import get_owner

        activity = getattr(get_owner(), "index_activity", lambda: None)()
        if activity:
            body["current"] = activity
    except Exception:  # noqa: BLE001
        pass
    return body


def install_domain_handlers(app) -> None:
    """Register one handler per domain condition. Idempotent per app."""
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from ragtools.collection_router import UnknownProject
    from ragtools.embedding.encoder import ModelUnavailable
    from ragtools.service.destructive import OperationRefused
    from ragtools.service.owner import StorageWentAway
    from ragtools.upgrade.relayout import MigrationInProgress

    @app.exception_handler(MigrationInProgress)
    async def _migration(request: Request, exc: MigrationInProgress):
        body = migration_payload(exc)
        logger.info("%s %s refused: %s", request.method, request.url.path,
                    body.get("error"))
        return JSONResponse(status_code=409, content=body)

    @app.exception_handler(OperationRefused)
    async def _refused(request: Request, exc: OperationRefused):
        return JSONResponse(status_code=409, content={
            "error": getattr(exc, "code", None) or OPERATION_CONFLICT,
            "message": str(exc),
            "engine": _engine_snapshot(),
        })

    @app.exception_handler(StorageWentAway)
    async def _storage(request: Request, exc: StorageWentAway):
        return JSONResponse(status_code=503, headers={"Retry-After": "15"},
                            content={
                                "error": STORAGE_UNAVAILABLE,
                                "message": str(exc),
                                "engine": _engine_snapshot(),
                                "remediation": "the service restarts the engine "
                                               "automatically; watch /health",
                            })

    @app.exception_handler(UnknownProject)
    async def _unknown_project(request: Request, exc: UnknownProject):
        # ``UnknownProject`` subclasses KeyError, so ``str(exc)`` is the repr of
        # the message. Unwrap it — the refusal explains itself, and a caller
        # reading escaped quotes learns nothing.
        message = exc.args[0] if exc.args else "unknown project"
        return JSONResponse(status_code=404, content={
            "error": UNKNOWN_PROJECT,
            "message": str(message),
            "remediation": "check the project id against /api/projects",
        })

    @app.exception_handler(ModelUnavailable)
    async def _model(request: Request, exc: ModelUnavailable):
        return JSONResponse(status_code=503, content={
            "error": MODEL_UNAVAILABLE,
            "message": str(exc),
            "model": getattr(exc, "model", ""),
            "searched": getattr(exc, "searched", []),
            "remediation": "the embedding model is missing from this "
                           "installation; reinstall or restore model_cache",
        })
