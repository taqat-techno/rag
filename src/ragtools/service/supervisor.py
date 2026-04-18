"""Supervisor for the RAG service process.

When enabled, `rag service start` launches a small supervisor process that
in turn spawns the real service. If the real service exits with a non-zero
code (crash, kill, OOM) the supervisor respawns it with exponential backoff.
After a configurable number of failures inside a rolling window the
supervisor gives up and leaves a marker file for post-mortem.

The restart policy is deliberately isolated from the process-spawning code
so it can be unit-tested without subprocesses. See `SupervisorPolicy`.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Deque, Optional

logger = logging.getLogger("ragtools.supervisor")


# ---------------------------------------------------------------------------
# Pure policy layer — no subprocesses, no clock, no filesystem. Testable.
# ---------------------------------------------------------------------------


@dataclass
class SupervisorPolicy:
    """Decides when to respawn the child and for how long to wait.

    Args:
        max_failures: Maximum crashes allowed inside `window_seconds` before
            the supervisor gives up. Default: 5.
        window_seconds: Rolling window over which failures are counted.
            Crashes older than this are forgotten. Default: 300 (5 min).
        base_backoff: Initial backoff in seconds. Default: 2.
        max_backoff: Cap on the backoff. Default: 32.
    """

    max_failures: int = 5
    window_seconds: float = 300.0
    base_backoff: float = 2.0
    max_backoff: float = 32.0

    # In-memory timestamps of recent failures (seconds since epoch-ish).
    # A deque so old entries are easy to evict.
    _failures: Deque[float] = field(default_factory=deque)

    def record_failure(self, now: float) -> None:
        """Register a child crash at the given moment."""
        self._failures.append(now)
        self._evict_old(now)

    def _evict_old(self, now: float) -> None:
        """Drop failure timestamps outside the rolling window."""
        cutoff = now - self.window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    def recent_failures(self, now: float) -> int:
        self._evict_old(now)
        return len(self._failures)

    def should_give_up(self, now: float) -> bool:
        return self.recent_failures(now) >= self.max_failures

    def next_backoff(self, now: float) -> float:
        """Compute the sleep duration before the next respawn.

        Uses the number of recent failures as the exponent so the backoff
        grows with repeated crashes inside the window and resets when the
        window clears out.
        """
        n = self.recent_failures(now)
        if n <= 0:
            return 0.0
        delay = self.base_backoff * (2 ** (n - 1))
        return min(delay, self.max_backoff)


# ---------------------------------------------------------------------------
# The supervisor loop — spawns subprocesses, applies the policy
# ---------------------------------------------------------------------------


def _write_gave_up_marker(marker_path: Path, policy: SupervisorPolicy, reason: str) -> None:
    """Persist a structured 'supervisor gave up' record for post-mortem."""
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "reason": reason,
                    "max_failures": policy.max_failures,
                    "window_seconds": policy.window_seconds,
                },
                indent=2,
            )
        )
    except Exception as e:
        logger.error("Could not write gave_up marker to %s: %s", marker_path, e)


def run_supervisor_loop(
    spawn_child: Callable[[], subprocess.Popen],
    policy: SupervisorPolicy,
    marker_path: Path,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    on_start: Optional[Callable[[subprocess.Popen], None]] = None,
    on_crash: Optional[Callable[[int, int], None]] = None,
    on_give_up: Optional[Callable[[str], None]] = None,
) -> int:
    """Spawn the child, respawn on crash, give up per policy.

    Returns:
        The final exit code:
        - 0 if the child exited cleanly and the supervisor shut down with it.
        - 1 if the supervisor gave up after exhausting the restart budget.

    Args:
        spawn_child: zero-arg callable that starts the child and returns the
            live Popen handle. Called once per restart.
        policy: the restart policy.
        marker_path: path to write `supervisor_gave_up.json` when we give up.
        clock: injectable clock (for testing). Defaults to time.monotonic.
        sleeper: injectable sleep (for testing).
        on_start: optional hook called after each successful spawn with the
            Popen handle (used to write service.pid info, logs, etc.).
        on_crash: optional hook called after a crash, receiving
            (exit_code, recent_failures). Used to dispatch a toast the FIRST
            time a failure window opens so the user learns about it early.
        on_give_up: optional hook called when the supervisor exhausts its
            budget, receiving the reason string. Used to escalate the toast
            to "auto-restart disabled".
    """
    while True:
        child = spawn_child()
        if on_start is not None:
            try:
                on_start(child)
            except Exception as e:
                logger.warning("on_start hook raised: %s", e)
        logger.info("Supervisor: child spawned (PID %d)", child.pid)

        exit_code = child.wait()
        logger.info("Supervisor: child exited with code %d", exit_code)

        if exit_code == 0:
            # Clean shutdown — graceful /api/shutdown or equivalent.
            # The supervisor mirrors that and exits too.
            return 0

        # Non-zero → treat as crash
        now = clock()
        policy.record_failure(now)
        fails = policy.recent_failures(now)
        logger.warning(
            "Supervisor: child crashed (code=%d, failure %d/%d in last %.0fs)",
            exit_code, fails, policy.max_failures, policy.window_seconds,
        )

        if on_crash is not None:
            try:
                on_crash(exit_code, fails)
            except Exception as e:
                logger.warning("on_crash hook raised: %s", e)

        if policy.should_give_up(now):
            reason = (
                f"Child process exceeded {policy.max_failures} failures in "
                f"the last {policy.window_seconds:.0f} seconds; supervisor "
                f"is not going to respawn any further."
            )
            logger.error("Supervisor: giving up. %s", reason)
            _write_gave_up_marker(marker_path, policy, reason)
            if on_give_up is not None:
                try:
                    on_give_up(reason)
                except Exception as e:
                    logger.warning("on_give_up hook raised: %s", e)
            return 1

        delay = policy.next_backoff(now)
        logger.info("Supervisor: restarting in %.1fs", delay)
        sleeper(delay)


# ---------------------------------------------------------------------------
# The production entrypoint — wires together subprocess spawning + PID files
# ---------------------------------------------------------------------------


def run_supervisor(
    host: str,
    port: int,
    data_dir: Path,
    child_command: list[str],
    max_failures: int = 5,
    window_seconds: float = 300.0,
) -> int:
    """Run the supervisor in the foreground.

    This is the function `rag service supervise` calls. It:
      - Writes its own PID to `{data_dir}/supervisor.pid`.
      - Loops spawning `child_command` and respawning on crash.
      - Writes `{data_dir}/logs/supervisor_gave_up.json` if it gives up.
      - Cleans up its PID file on exit.
    """
    data_dir = Path(data_dir)
    supervisor_pid_path = data_dir / "supervisor.pid"
    marker_path = data_dir / "logs" / "supervisor_gave_up.json"

    supervisor_pid_path.parent.mkdir(parents=True, exist_ok=True)
    import os as _os
    supervisor_pid_path.write_text(str(_os.getpid()))
    logger.info(
        "Supervisor started (PID %d) for command: %s",
        _os.getpid(), " ".join(child_command),
    )

    # Detached-child creation flags on Windows to avoid the supervisor
    # and child sharing a console that could close out from under them.
    popen_kwargs: dict = {}
    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        popen_kwargs["creationflags"] = CREATE_NO_WINDOW

    # Stream child stdout/stderr into the same service.log the real service
    # writes to, so all diagnostics stay in one place.
    log_path = data_dir / "logs" / "service.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def _spawn() -> subprocess.Popen:
        log_fh = open(log_path, "a", encoding="utf-8")
        return subprocess.Popen(
            child_command,
            stdout=log_fh,
            stderr=log_fh,
            **popen_kwargs,
        )

    policy = SupervisorPolicy(
        max_failures=max_failures,
        window_seconds=window_seconds,
    )

    # Build toast hooks that delegate to CrashNotifier. Cooldown in the notifier
    # collapses a crash burst into a single toast per kind, so wiring the hook
    # on every crash is safe.
    from ragtools.config import Settings as _Settings
    from ragtools.service.notify import (
        CrashNotifier,
        notify_service_crashed,
        notify_supervisor_gave_up,
    )

    settings = _Settings()
    notifier = CrashNotifier(settings=settings)

    def _on_crash(exit_code: int, fails: int) -> None:
        msg = f"Exit code {exit_code}. Auto-restart attempt {fails}/{policy.max_failures}."
        notify_service_crashed(settings, msg, notifier=notifier)

    def _on_give_up(reason: str) -> None:
        notify_supervisor_gave_up(settings, reason, notifier=notifier)

    try:
        return run_supervisor_loop(
            spawn_child=_spawn,
            policy=policy,
            marker_path=marker_path,
            on_crash=_on_crash,
            on_give_up=_on_give_up,
        )
    finally:
        supervisor_pid_path.unlink(missing_ok=True)
        logger.info("Supervisor exiting")
