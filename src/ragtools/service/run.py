"""Service entry point — starts uvicorn with the FastAPI app.

Usage:
  python -m ragtools.service.run [--host HOST] [--port PORT] [--from-scheduler]
  rag service run  (CLI wrapper)
"""

import argparse
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ragtools.config import Settings


def setup_logging(settings: Settings) -> None:
    """Configure service logging with rotating file handler."""
    log_dir = Path(settings.data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "service.log"

    handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=3,
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
    )

    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    # Also log to stderr for foreground mode
    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
    )
    root.addHandler(stderr_handler)


def _post_startup(settings: Settings, from_scheduler: bool) -> None:
    """Run post-startup tasks: auto-register startup/watchdog, toast, sync, browser.

    The file watcher is NOT started here. As of M3 it is owned by the service
    lifecycle (the FastAPI lifespan calls ``autostart_watcher()``), replacing the
    old delayed HTTP self-POST that could miss the readiness window and leave the
    watcher silently inactive.
    """
    logger = logging.getLogger("ragtools.service")

    # Auto-register Windows startup task (idempotent — skips if already installed)
    # Only the packaged/installed app should write to the Windows Startup folder.
    # Dev/source mode must never overwrite the installed launcher with a venv path.
    try:
        import sys as _sys
        from ragtools.config import is_packaged
        # Self-register autostart on first packaged boot, on every platform.
        if is_packaged():
            from ragtools.service.startup import install_task, is_task_installed

            if not is_task_installed():
                install_task(settings, delay_seconds=settings.startup_delay)
                logger.info("Auto-registered Windows startup task (delay=%ds)", settings.startup_delay)
                from ragtools.service.activity import log_activity
                log_activity("success", "startup", "Auto-registered Windows login startup task")
        else:
            logger.info("Startup auto-registration skipped: running from source (dev mode)")
    except Exception as e:
        logger.warning("Failed to auto-register startup task (non-fatal): %s", e)

    # The watchdog task is gone: restart-on-failure is a native capability of
    # Task Scheduler, systemd and launchd, so a bespoke polling task was one
    # more thing to install, flash a console, and leave behind on upgrade.

    # Fire a "service is running" desktop toast — once per OS boot, so
    # routine restarts inside the same session don't spam the user.
    # Dedup via psutil.boot_time() + a persistent boot_marker.json in data
    # dir. Any failure is strictly non-fatal.
    try:
        from ragtools.service.notify import notify_service_started
        notify_service_started(settings)
    except Exception as e:
        logger.warning("Failed to send service-started toast (non-fatal): %s", e)

    # Startup sync: check all projects for offline changes (non-blocking)
    import threading
    def _startup_sync():
        try:
            from ragtools.service.app import get_owner, get_settings
            from ragtools.service.activity import log_activity

            s = get_settings()

            # A pending layout migration takes priority over an incremental
            # sync, and completes it before anything else runs.
            #
            # Resumed rather than restarted: `run_pending` asks the plan what is
            # left, so a reboot four hours into an eight-hour rebuild continues
            # from where it stopped. Until it finishes, /health reports
            # `migrating` and searches refuse rather than answering from a
            # half-built index.
            from ragtools.upgrade import relayout

            plan = relayout.active_plan(s)
            if plan is not None:
                log_activity("warning", "indexer",
                             "Layout migration in progress — rebuilding every "
                             "project and framework under the new collection layout")
                logger.warning("Relayout plan %s is active; rebuilding", plan)
                report = relayout.run_pending(get_owner(), s, plan_id=plan)
                if report.complete:
                    log_activity("success", "indexer", report.describe())
                    logger.info("%s", report.describe())
                else:
                    log_activity("error", "indexer",
                                 f"{report.describe()} — retry with `rag upgrade --resume`")
                    for kind, unit_id, error in report.failures:
                        logger.error("relayout failed for %s %s: %s",
                                     kind, unit_id, error)
                return

            # Guard: do not run sync if no projects loaded (may be config load failure)
            if not s.projects:
                log_activity("warning", "indexer", "Startup sync skipped: no projects configured (check config path)")
                logger.warning("Startup sync skipped — no projects in config. Config may not have loaded correctly.")
                return

            log_activity("info", "indexer", "Startup sync: checking for offline changes...")
            owner = get_owner()
            stats = owner.run_incremental_index()
            indexed = stats.get("indexed", 0)
            deleted = stats.get("deleted", 0)
            skipped = stats.get("skipped", 0)
            if indexed > 0 or deleted > 0:
                log_activity("success", "indexer",
                    f"Startup sync: {indexed} indexed, {deleted} deleted, {skipped} unchanged")
            else:
                log_activity("info", "indexer",
                    f"Startup sync: all {skipped} files up to date")
        except Exception as e:
            logger.warning("Startup sync failed (non-fatal): %s", e)
    threading.Timer(5.0, _startup_sync).start()

    # Open browser if configured and launched from scheduler
    if from_scheduler and settings.startup_open_browser:
        try:
            import webbrowser
            url = f"http://{settings.service_host}:{settings.service_port}"
            webbrowser.open(url)
            logger.info("Opened browser to %s", url)
        except Exception as e:
            logger.warning("Failed to open browser: %s", e)


def main():
    """Entry point for the service process."""
    parser = argparse.ArgumentParser(description="RAGTools Service")
    parser.add_argument("--host", default=None, help="Bind host")
    parser.add_argument("--port", type=int, default=None, help="Bind port")
    parser.add_argument("--from-scheduler", action="store_true",
                        help="Indicates launch from Task Scheduler (enables browser-open if configured)")
    args = parser.parse_args()

    # Set model cache path for frozen (PyInstaller) executables
    import sys as _sys
    if getattr(_sys, "frozen", False):
        bundle_dir = os.path.dirname(_sys.executable)
        model_cache = os.path.join(bundle_dir, "model_cache")
        if os.path.exists(model_cache):
            os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", model_cache)

    # Before Settings() is read: see ragtools.bootstrap for why the seam is
    # here rather than after the owner exists.
    from ragtools.bootstrap import ensure_config_current_once
    ensure_config_current_once()

    settings = Settings()
    host = args.host or settings.service_host
    port = args.port or settings.service_port

    setup_logging(settings)

    # Write PID file
    pid_path = Path(settings.data_dir) / "service.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))

    logger = logging.getLogger("ragtools.service")
    from ragtools.config import is_packaged
    mode = "installed" if is_packaged() else "dev (source)"
    logger.info("Starting uvicorn on %s:%d (PID %d) [mode=%s]", host, port, os.getpid(), mode)
    logger.info("Data directory: %s", Path(settings.data_dir).resolve())

    try:
        import uvicorn
        import threading
        from ragtools.service.app import create_app

        app = create_app()

        # Record the ACTUAL launched bind so GET /identity reports the real
        # port (§27.1) — the route's fallback only knows the configured value,
        # so a CLI --port override would otherwise be misreported. A `0`
        # (ephemeral) bind needs the uvicorn.Server-object pattern to read the
        # assigned socket port; recorded here for the fixed-port case.
        if port:
            from ragtools.service.routes import set_bound_address
            set_bound_address(host, port)

        # Schedule post-startup tasks after uvicorn is ready
        # (run in a thread that waits for health endpoint)
        def _delayed_post_startup():
            import time
            import httpx
            for _ in range(30):  # Wait up to 30s for service to be ready
                time.sleep(1)
                try:
                    r = httpx.get(f"http://{host}:{port}/health", timeout=2.0)
                    if r.status_code == 200:
                        _post_startup(settings, args.from_scheduler)
                        return
                except Exception:
                    pass
            logger.warning("Post-startup tasks skipped — service did not become ready in time")

        threading.Thread(target=_delayed_post_startup, daemon=True).start()

        uvicorn.run(app, host=host, port=port, log_level="warning")
    except BaseException as exc:
        # Capture ANYTHING that would kill the process — exceptions, system exits,
        # keyboard interrupts — so the log and crash marker survive a hard death.
        # Rationale: field reports showed the service vanishing with no trace in
        # service.log. The empty log made post-mortem analysis impossible.
        _record_fatal_crash(settings, exc, host, port)
        raise
    finally:
        # Cleanup PID file on exit
        pid_path.unlink(missing_ok=True)
        logger.info("Service process exiting")


#: Which subsystem a failure belongs to, keyed by the module that raised it.
#: A crash banner that says "encoder" sends someone to the model; one that says
#: `SystemExit: 3` sends them to the storage engine, which is where the last
#: three hours of the `LAKOSHA-TAQAT` investigation went.
_SUBSYSTEM_BY_MODULE = (
    ("ragtools.embedding", "encoder"),
    ("sentence_transformers", "encoder"),
    ("huggingface_hub", "encoder"),
    ("transformers", "encoder"),
    ("ragtools.upgrade", "migration"),
    ("ragtools.service.engine", "storage"),
    ("ragtools.storage", "storage"),
    ("qdrant_client", "storage"),
    ("ragtools.config", "config"),
)


def _causal_chain(exc: BaseException, limit: int = 8) -> list:
    """Every exception behind ``exc``, outermost first.

    ``SystemExit`` is the reason this exists. Uvicorn catches a lifespan failure
    and calls ``sys.exit(3)``, so the exception the operator gets has no
    ``__cause__`` and no ``__context__`` pointing at what actually broke. The
    chain is walked anyway — it is free, and it is correct for every OTHER fatal
    path — while :func:`_startup_cause` supplies the link uvicorn destroys.
    """
    import traceback

    chain: list = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(chain) < limit and id(current) not in seen:
        seen.add(id(current))
        frame = ""
        module = ""
        tb = getattr(current, "__traceback__", None)
        if tb is not None:
            try:
                last = traceback.extract_tb(tb)[-1]
                frame = f"{Path(last.filename).name}:{last.lineno}"
                module = last.filename
            except (IndexError, ValueError, OSError):
                pass
        chain.append({
            "type": type(current).__name__,
            "message": str(current)[:500],
            "frame": frame,
            "module": module,
        })
        current = current.__cause__ or current.__context__
    return chain


def _classify(chain: list) -> str:
    """Name the subsystem from the deepest frame that matches a known module."""
    for entry in reversed(chain):
        haystack = f"{entry.get('module', '')}|{entry.get('type', '')}"
        for needle, subsystem in _SUBSYSTEM_BY_MODULE:
            if needle.replace(".", "\\") in haystack or needle in haystack:
                return subsystem
    return "unknown"


def _startup_cause():
    """The exception the lifespan recorded, which uvicorn then flattened."""
    try:
        from ragtools.service.app import startup_failure

        return startup_failure()
    except Exception:  # noqa: BLE001 — the recorder must never be the crash
        return None


def _runtime_context() -> dict:
    """Engine and migration state at the moment of the crash.

    A fatal exit with the engine `restart_exhausted` and one with a healthy
    engine have nothing in common except that the process stopped. Recording
    which it was is the difference between a diagnosis and a guess.
    """
    context: dict = {}
    try:
        from ragtools.service.app import engine_status

        context["engine"] = engine_status()
    except Exception:  # noqa: BLE001
        context["engine"] = None
    try:
        from ragtools.service.app import get_settings
        from ragtools.upgrade import relayout

        settings = get_settings()
        plan = relayout.active_plan(settings)
        if plan is not None:
            report = relayout.progress(settings, plan)
            context["migration"] = (
                {"plan": plan, "done": report.done, "total": report.total,
                 "blocked": report.blocked, "failed": report.failed}
                if report is not None else {"plan": plan})
        else:
            context["migration"] = None
    except Exception:  # noqa: BLE001
        context["migration"] = None
    return context


def _record_fatal_crash(settings: Settings, exc: BaseException, host: str, port: int) -> None:
    """Persist a structured record of a fatal service exit.

    Writes:
      - full traceback + memory snapshot to service.log at CRITICAL level
      - a small last_crash.json file next to the log for the admin panel to
        surface a "previous session crashed" banner on next startup

    **The record names the cause, not only the exit.** v3.3.0 wrote
    ``{"exception_type": "SystemExit", "message": "3"}`` for a crash whose real
    cause was a DNS failure loading the embedding model, and the only way to find
    that out was to read the four log lines above it by hand.
    """
    import json
    import traceback
    from datetime import datetime, timezone

    logger = logging.getLogger("ragtools.service")
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    # The lifespan captured the real exception before uvicorn turned it into an
    # exit code. Prefer it, and keep the outer one so the record still shows how
    # the process actually ended.
    cause = _startup_cause()
    chain = _causal_chain(exc)
    if cause is not None and cause is not exc:
        chain = chain + _causal_chain(cause)
        tb += ("\n--- startup failure that produced this exit ---\n"
               + "".join(traceback.format_exception(
                   type(cause), cause, cause.__traceback__)))
    root = chain[-1] if chain else {"type": type(exc).__name__, "message": str(exc)}

    # Memory snapshot helps correlate crashes with OOM / large indexing batches.
    mem_info: dict = {}
    try:
        import resource  # type: ignore[import-not-found]
        ru = resource.getrusage(resource.RUSAGE_SELF)
        mem_info["max_rss_bytes"] = ru.ru_maxrss
    except Exception:
        pass
    try:
        import psutil  # type: ignore[import-untyped]
        vm = psutil.virtual_memory()
        mem_info.update({
            "system_mem_total": vm.total,
            "system_mem_available": vm.available,
            "system_mem_percent_used": vm.percent,
        })
        proc_mem = psutil.Process().memory_info()
        mem_info["process_rss_bytes"] = proc_mem.rss
    except Exception:
        pass

    logger.critical(
        "Service crashed: %s\nHost: %s  Port: %d\nMemory: %s\n%s",
        exc, host, port, mem_info or "unavailable", tb,
    )

    try:
        log_dir = Path(settings.data_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        marker = log_dir / "last_crash.json"
        from ragtools import __version__ as _version

        context = _runtime_context()
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": _version,
            "process_role": "service",
            "host": host,
            "port": port,
            "pid": os.getpid(),
            # PRESERVED KEYS. The admin panel's crash banner reads these, and
            # `last_crash.json` is a contract with a shipped UI — additions are
            # safe, renames are not.
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "traceback": tb,
            "memory": mem_info,
            # New: the part that makes the record diagnosable on its own.
            "outer": {"type": type(exc).__name__, "message": str(exc)[:500]},
            "cause_chain": chain,
            "root_cause": {"type": root.get("type"), "message": root.get("message")},
            "subsystem": _classify(chain),
            "engine": context.get("engine"),
            "migration": context.get("migration"),
            "logs": {"service": "service.log", "engine": "qdrant.log"},
        }
        marker.write_text(json.dumps(payload, indent=2, default=str))
    except Exception as write_err:
        # Last-resort: never let the crash recorder cause a second crash
        logger.error("Failed to write last_crash.json: %s", write_err)

    try:
        from ragtools.service.notify import notify_service_crashed
        notify_service_crashed(settings, str(exc))
    except Exception as notify_err:
        logger.error("Failed to send crash toast: %s", notify_err)


if __name__ == "__main__":
    main()
