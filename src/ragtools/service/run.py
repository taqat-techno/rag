"""Service entry point — starts uvicorn with the FastAPI app.

Usage:
  python -m ragtools.service.run [--host HOST] [--port PORT] [--from-scheduler]
  rag service run  (CLI wrapper)
"""

import argparse
import logging
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
    """Run post-startup tasks: auto-watcher, browser open."""
    logger = logging.getLogger("ragtools.service")

    # Auto-start watcher if configured
    if settings.startup_watcher:
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

    settings = Settings()
    host = args.host or settings.service_host
    port = args.port or settings.service_port

    setup_logging(settings)

    # Write PID file
    pid_path = Path(settings.qdrant_path).parent / "service.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    import os
    pid_path.write_text(str(os.getpid()))

    logger = logging.getLogger("ragtools.service")
    logger.info("Starting uvicorn on %s:%d (PID %d)", host, port, os.getpid())

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

        if settings.startup_watcher or (args.from_scheduler and settings.startup_open_browser):
            threading.Thread(target=_delayed_post_startup, daemon=True).start()

        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        # Cleanup PID file on exit
        pid_path.unlink(missing_ok=True)
        logger.info("Service process exiting")


if __name__ == "__main__":
    main()
