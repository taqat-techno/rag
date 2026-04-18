"""System-tray icon for RAG Tools.

Runs as an independent process so it survives service crashes. Polls the
service /health endpoint every few seconds and paints a colour-coded
badge that summarises service state at a glance.

Architecture
------------
- A pure core — ``classify_state``, ``poll_loop``, ``build_menu_items`` —
  has no dependency on pystray or Pillow and is fully unit-tested.
- The ``TrayApp`` class glues that core to pystray, which is imported
  lazily so ``ragtools`` is still importable without the ``[tray]`` extra.
- A ``tray.pid`` file in the data directory enforces single-instance.

The tray is intentionally independent of the service process. If the
service crashes, the tray keeps running, turns red, and lets the user
click "Restart service". A subprocess or service-thread model would
vanish exactly when the user most needs visibility.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Literal, Optional, Sequence

logger = logging.getLogger("ragtools.tray")


StateKind = Literal["healthy", "starting", "down", "unreachable", "unknown"]


# ---------------------------------------------------------------------------
# Pure core — no pystray, no Pillow, no network
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """Single /health probe outcome."""
    ok: bool
    collection: str = ""
    error: str = ""


@dataclass
class TrayState:
    """What the tray should show right now."""
    kind: StateKind
    detail: str


# Cold-start grace period. If the PID is alive and we've never (or not recently)
# seen /health return 200, show "starting" for this long before switching the
# tooltip to "unreachable".
_STARTING_GRACE_SECONDS = 45.0


def classify_state(
    probe: ProbeResult,
    service_pid_alive: bool,
    seconds_since_last_healthy: Optional[float],
) -> TrayState:
    """Decide the tray state from the last probe + PID info.

    Transitions:
        /health 200                 → healthy
        PID alive, no health yet    → starting (encoder loading ~20-40s)
        PID alive, /health > grace  → unreachable (process hung)
        no PID / dead PID           → down
    """
    if probe.ok:
        detail = f"ready · {probe.collection}" if probe.collection else "ready"
        return TrayState("healthy", detail)

    if service_pid_alive:
        if seconds_since_last_healthy is None or seconds_since_last_healthy < _STARTING_GRACE_SECONDS:
            return TrayState("starting", "starting up…")
        return TrayState("unreachable", "process alive but not responding")

    return TrayState("down", "service is not running")


# ---------------------------------------------------------------------------
# Poll loop — takes probe + pid + clock by injection so tests never touch HTTP
# ---------------------------------------------------------------------------


def poll_loop(
    probe_fn: Callable[[], ProbeResult],
    pid_fn: Callable[[], bool],
    on_state_change: Callable[[TrayState], None],
    stop_event: threading.Event,
    interval: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Run the tray poll loop until ``stop_event`` is set.

    ``on_state_change`` is called with the new ``TrayState`` ONLY when
    the state (kind or detail) changes — we don't repaint the tray on
    every tick if nothing changed.
    """
    last_healthy_at: Optional[float] = None
    prev: Optional[TrayState] = None
    while not stop_event.is_set():
        probe = probe_fn()
        pid_alive = pid_fn()
        now = clock()
        if probe.ok:
            last_healthy_at = now
        seconds_since = (now - last_healthy_at) if last_healthy_at is not None else None
        state = classify_state(probe, pid_alive, seconds_since)
        if prev is None or state.kind != prev.kind or state.detail != prev.detail:
            try:
                on_state_change(state)
            except Exception as e:
                logger.warning("Tray state-change handler raised: %s", e)
            prev = state
        stop_event.wait(interval)


# ---------------------------------------------------------------------------
# Menu assembly — neutral data structure, converted to pystray's API at runtime
# ---------------------------------------------------------------------------


@dataclass
class MenuCallbacks:
    on_open_admin: Callable[[], None]
    on_copy_url: Callable[[], None]
    on_restart: Callable[[], None]
    on_stop: Callable[[], None]
    on_open_logs: Callable[[], None]
    on_open_backups: Callable[[], None]
    on_quit: Callable[[], None]


@dataclass
class MenuItem:
    """Neutral menu entry — pystray-independent so tests don't need pystray."""
    label: str = ""
    action: Optional[Callable[[], None]] = None
    enabled: bool = True
    default: bool = False
    separator: bool = False


def build_menu_items(state: TrayState, callbacks: MenuCallbacks) -> List[MenuItem]:
    """Return the menu for the current state.

    Items are disabled in a state-aware way: e.g. "Stop service" is grayed
    out when the service is already down. The default item (activated on
    left-click / double-click) is "Open admin panel" when healthy.
    """
    healthy = state.kind == "healthy"
    has_process = state.kind in ("healthy", "starting", "unreachable")

    return [
        MenuItem(label=f"RAGTools · {state.detail}", enabled=False),
        MenuItem(separator=True),
        MenuItem(label="Open admin panel", action=callbacks.on_open_admin,
                 enabled=healthy, default=True),
        MenuItem(label="Copy admin URL", action=callbacks.on_copy_url),
        MenuItem(separator=True),
        MenuItem(label="Restart service", action=callbacks.on_restart),
        MenuItem(label="Stop service", action=callbacks.on_stop, enabled=has_process),
        MenuItem(separator=True),
        MenuItem(label="View logs folder", action=callbacks.on_open_logs),
        MenuItem(label="View backups folder", action=callbacks.on_open_backups),
        MenuItem(separator=True),
        MenuItem(label="Quit tray", action=callbacks.on_quit),
    ]


# ---------------------------------------------------------------------------
# TrayApp — glues the pure core to pystray + the service control helpers
# ---------------------------------------------------------------------------


def _tray_pid_path(settings) -> Path:
    """Where the tray's own PID file lives (sibling of service.pid)."""
    return Path(settings.qdrant_path).parent / "tray.pid"


def _admin_url(settings) -> str:
    return f"http://{settings.service_host}:{settings.service_port}/"


class TrayApp:
    """The full tray runtime. Construct once; call ``run()`` to block."""

    def __init__(self, settings, poll_interval: float = 5.0):
        self.settings = settings
        self.poll_interval = poll_interval
        self.state = TrayState("unknown", "initializing")
        self._stop_event = threading.Event()
        self._icon = None  # created in run() after pystray import
        self._pid_written = False

    # --- probes --------------------------------------------------------

    def _probe(self) -> ProbeResult:
        """One /health probe. Never raises."""
        try:
            import httpx
            r = httpx.get(
                f"http://{self.settings.service_host}:{self.settings.service_port}/health",
                timeout=2.0,
            )
            if r.status_code == 200:
                collection = ""
                try:
                    collection = r.json().get("collection", "")
                except Exception:
                    pass
                return ProbeResult(ok=True, collection=collection)
            return ProbeResult(ok=False, error=f"http {r.status_code}")
        except Exception as e:
            return ProbeResult(ok=False, error=str(e))

    def _pid_alive(self) -> bool:
        """Is any service process currently alive (per its PID file)?"""
        try:
            from ragtools.service.process import _read_pid
            return _read_pid(self.settings) is not None
        except Exception:
            return False

    # --- menu actions --------------------------------------------------

    def _on_open_admin(self) -> None:
        try:
            webbrowser.open(_admin_url(self.settings))
        except Exception as e:
            logger.warning("Open admin failed: %s", e)

    def _on_copy_url(self) -> None:
        try:
            import shutil
            import subprocess
            url = _admin_url(self.settings)
            if sys.platform == "win32":
                subprocess.run(["clip"], input=url.encode("utf-8"), check=False)
                return
            if sys.platform == "darwin":
                subprocess.run(["pbcopy"], input=url.encode("utf-8"), check=False)
                return
            # Linux / other Unix — try a portable fallback chain covering
            # Wayland (wl-copy), X11 with xclip, X11 with xsel. Each candidate
            # is checked with shutil.which first so missing tools don't raise.
            for tool, args in (
                ("wl-copy", []),
                ("xclip",   ["-selection", "clipboard"]),
                ("xsel",    ["--clipboard", "--input"]),
            ):
                if shutil.which(tool):
                    subprocess.run([tool, *args],
                                   input=url.encode("utf-8"), check=False)
                    return
            logger.warning(
                "Copy URL: no clipboard tool found on PATH "
                "(tried wl-copy, xclip, xsel). URL: %s", url,
            )
        except Exception as e:
            logger.warning("Copy URL failed: %s", e)

    def _on_restart(self) -> None:
        threading.Thread(target=self._restart_worker, daemon=True).start()

    def _restart_worker(self) -> None:
        try:
            from ragtools.service.process import start_service, stop_service
            try:
                stop_service(self.settings)
            except Exception as e:
                logger.info("Tray restart: stop step raised (continuing): %s", e)
            time.sleep(0.5)  # let PID files clear
            try:
                start_service(self.settings, supervise=True)
            except RuntimeError as e:
                # Already running — someone raced us. That's fine.
                logger.info("Tray restart: start skipped: %s", e)
        except Exception as e:
            logger.error("Tray restart failed: %s", e)

    def _on_stop(self) -> None:
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self) -> None:
        try:
            from ragtools.service.process import stop_service
            stop_service(self.settings)
        except Exception as e:
            logger.error("Tray stop failed: %s", e)

    def _on_open_logs(self) -> None:
        self._open_folder(Path(self.settings.qdrant_path).parent / "logs")

    def _on_open_backups(self) -> None:
        self._open_folder(Path(self.settings.state_db).parent / "backups")

    def _open_folder(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", str(path)])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            logger.warning("Open folder failed (%s): %s", path, e)

    def _on_quit(self) -> None:
        logger.info("Tray quit requested from menu")
        self._stop_event.set()
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass

    # --- pystray glue --------------------------------------------------

    def _callbacks(self) -> MenuCallbacks:
        return MenuCallbacks(
            on_open_admin=self._on_open_admin,
            on_copy_url=self._on_copy_url,
            on_restart=self._on_restart,
            on_stop=self._on_stop,
            on_open_logs=self._on_open_logs,
            on_open_backups=self._on_open_backups,
            on_quit=self._on_quit,
        )

    def _build_pystray_menu(self):
        """Convert the neutral menu items to pystray.Menu."""
        from pystray import Menu, MenuItem as PyItem  # type: ignore[import-not-found]

        items = build_menu_items(self.state, self._callbacks())
        out = []

        def _noop_action(_icon, _item):
            """Silent stand-in for header / informational rows. pystray
            requires a callable even when enabled=False."""

        def _make_handler(action):
            """Return a two-arg callable matching pystray's expected signature.

            Defined via a factory so the resulting function has exactly two
            positional parameters — pystray counts default-valued params in
            ``__code__.co_argcount`` and rejects anything above two.
            """
            def _handler(icon, item):
                try:
                    action()
                except Exception as e:
                    logger.warning("Tray menu action raised: %s", e)
            return _handler

        for it in items:
            if it.separator:
                out.append(Menu.SEPARATOR)
                continue
            handler = _noop_action if it.action is None else _make_handler(it.action)
            out.append(PyItem(
                it.label, handler,
                enabled=it.enabled, default=it.default,
            ))
        return Menu(*out)

    def _on_state_change(self, new_state: TrayState) -> None:
        """Called by the poll thread whenever the visual state needs updating."""
        logger.info(
            "Tray state: %s → %s (%s)",
            self.state.kind, new_state.kind, new_state.detail,
        )
        self.state = new_state
        if self._icon is None:
            return
        try:
            from ragtools.tray_icons import generate_icon
            self._icon.icon = generate_icon(new_state.kind)
            self._icon.title = f"RAGTools · {new_state.detail}"
            self._icon.menu = self._build_pystray_menu()
        except Exception as e:
            logger.warning("Tray refresh failed: %s", e)

    # --- lifecycle -----------------------------------------------------

    def _acquire_single_instance(self) -> bool:
        """Return True if we became the single tray instance, False otherwise."""
        pid_path = _tray_pid_path(self.settings)
        if pid_path.exists():
            try:
                existing = int(pid_path.read_text().strip())
                from ragtools.service.process import _process_alive
                if _process_alive(existing):
                    logger.warning("Tray already running (PID %d).", existing)
                    return False
            except Exception:
                pass  # stale / unreadable → we'll overwrite
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()))
        self._pid_written = True
        return True

    def _release_single_instance(self) -> None:
        if self._pid_written:
            _tray_pid_path(self.settings).unlink(missing_ok=True)
            self._pid_written = False

    def run(self) -> int:
        """Block until the user quits the tray. Returns process exit code."""
        if not self._acquire_single_instance():
            return 1

        try:
            try:
                import pystray  # type: ignore[import-not-found]
                from ragtools.tray_icons import generate_icon
            except ImportError as e:
                logger.error("Tray requires the [tray] extra: %s", e)
                print(
                    "RAG Tools tray requires optional dependencies.\n"
                    "Install with:  pip install 'ragtools[tray]'",
                    file=sys.stderr,
                )
                return 2

            self._icon = pystray.Icon(
                name="ragtools",
                icon=generate_icon("unknown"),
                title="RAGTools · initializing",
                menu=self._build_pystray_menu(),
            )

            poll_thread = threading.Thread(
                target=poll_loop,
                kwargs=dict(
                    probe_fn=self._probe,
                    pid_fn=self._pid_alive,
                    on_state_change=self._on_state_change,
                    stop_event=self._stop_event,
                    interval=self.poll_interval,
                ),
                daemon=True,
            )
            poll_thread.start()

            # Blocks until _icon.stop() is called from _on_quit.
            self._icon.run()
            return 0
        finally:
            self._stop_event.set()
            self._release_single_instance()
