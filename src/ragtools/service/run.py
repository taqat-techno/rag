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
    log_dir = Path(settings.qdrant_path).parent / "logs"
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
    """Run post-startup tasks: watcher, auto-register startup, optional browser."""
    logger = logging.getLogger("ragtools.service")

    # Always start the file watcher
    try:
        import httpx
        r = httpx.post(
            f"http://{settings.service_host}:{settings.service_port}/api/watcher/start",
            timeout=5.0,
        )
        if r.status_code == 200:
            logger.info("Watcher auto-started")
        else:
            logger.warning("Watcher auto-start returned %d", r.status_code)
    except Exception as e:
        logger.warning("Failed to auto-start watcher: %s", e)

    # Auto-register Windows startup task (idempotent — skips if already installed)
    # Only the packaged/installed app should write to the Windows Startup folder.
    # Dev/source mode must never overwrite the installed launcher with a venv path.
    try:
        import sys as _sys
        from ragtools.config import is_packaged
        if _sys.platform == "win32" and is_packaged():
            from ragtools.service.startup import is_task_installed, install_task
            if not is_task_installed():
                install_task(settings, delay_seconds=settings.startup_delay)
                logger.info("Auto-registered Windows startup task (delay=%ds)", settings.startup_delay)
                from ragtools.service.activity import log_activity
                log_activity("success", "startup", "Auto-registered Windows login startup task")
        elif _sys.platform == "win32":
            logger.info("Startup auto-registration skipped: running from source (dev mode)")
    except Exception as e:
        logger.warning("Failed to auto-register startup task (non-fatal): %s", e)

    # Auto-register the watchdog task alongside the login task. Same guard —
    # only in packaged mode. Idempotent: schtasks /create /f overwrites.
    try:
        import sys as _sys
        from ragtools.config import is_packaged
        if _sys.platform == "win32" and is_packaged():
            from ragtools.service.watchdog import (
                is_watchdog_installed,
                install_watchdog_task,
            )
            if not is_watchdog_installed():
                if install_watchdog_task(settings):
                    logger.info("Auto-registered Task Scheduler watchdog (every 15 min)")
                    from ragtools.service.activity import log_activity
                    log_activity("success", "startup", "Auto-registered service watchdog")
    except Exception as e:
        logger.warning("Failed to auto-register watchdog (non-fatal): %s", e)

    # Startup sync: check all projects for offline changes (non-blocking)
    import threading
    def _startup_sync():
        try:
            from ragtools.service.app import get_owner, get_settings
            from ragtools.service.activity import log_activity

            # Guard: do not run sync if no projects loaded (may be config load failure)
            s = get_settings()
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

    settings = Settings()
    host = args.host or settings.service_host
    port = args.port or settings.service_port

    setup_logging(settings)

    # Write PID file
    pid_path = Path(settings.qdrant_path).parent / "service.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))

    logger = logging.getLogger("ragtools.service")
    from ragtools.config import is_packaged
    mode = "installed" if is_packaged() else "dev (source)"
    logger.info("Starting uvicorn on %s:%d (PID %d) [mode=%s]", host, port, os.getpid(), mode)
    logger.info("Data directory: %s", Path(settings.qdrant_path).parent.resolve())

    try:
        import uvicorn
        import threading
        from ragtools.service.app import create_app

        app = create_app()

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


def _record_fatal_crash(settings: Settings, exc: BaseException, host: str, port: int) -> None:
    """Persist a structured record of a fatal service exit.

    Writes:
      - full traceback + memory snapshot to service.log at CRITICAL level
      - a small last_crash.json file next to the log for the admin panel to
        surface a "previous session crashed" banner on next startup
    """
    import json
    import traceback
    from datetime import datetime, timezone

    logger = logging.getLogger("ragtools.service")
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

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
        log_dir = Path(settings.qdrant_path).parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        marker = log_dir / "last_crash.json"
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "host": host,
            "port": port,
            "pid": os.getpid(),
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "traceback": tb,
            "memory": mem_info,
        }
        marker.write_text(json.dumps(payload, indent=2))
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
