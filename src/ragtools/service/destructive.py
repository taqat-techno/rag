"""One gate in front of every operation that can destroy an index.

v3.2.0 had four independent doors into ``owner.rebuild()`` — the UI fragment, the
JSON API, the CLI and the job worker — and not one of them asked whether the
operation could succeed. The service already *knew* storage was down; ``/health``
was reporting ``storage_unreachable`` at that moment. The rebuild took a backup,
started dropping collections, and surfaced the refusal as ``HTTP 500``.

The rule this module enforces: **a destructive operation must prove its
preconditions before it mutates anything at all** — before the backup, which was
previously the first thing to happen.

It is deliberately ONE function consulted by every caller rather than a check
copied four times. Four copies is how three of them stay correct.

Refusal is a *conflict*, not an error: the request was well-formed and the server
is temporarily unable to honour it. So HTTP gets 409, the CLI gets a categorised
exit code, MCP gets a structured error, and the UI gets a sentence a person can
act on. None of them gets a traceback.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ragtools.service")

#: Written before an irreversible rebuild step and removed after. Its presence at
#: startup means a rebuild was interrupted partway, which is a fact worth
#: surfacing — an empty index with no explanation is how a user concludes the
#: product lost their data.
INTENT_NAME = "rebuild-intent.json"

#: Process-wide. Two destructive operations must never interleave, whatever
#: doors they came through.
_operation_lock = threading.Lock()


class OperationRefused(RuntimeError):
    """A destructive operation cannot run right now, and why.

    Carries a machine-readable ``code`` so each surface can render its own
    idiom without re-deriving the reason from a message string.
    """

    def __init__(self, reason: str, code: str = "unavailable"):
        self.reason = reason
        self.code = code
        super().__init__(reason)


def blocking_reason(owner, *, operation: str = "rebuild") -> tuple[str, str]:
    """``(code, reason)`` if ``operation`` must be refused, else ``("", "")``.

    Cheap by construction: it reads cached lifecycle state and one already-cached
    storage probe. A guard that itself blocks on a dead engine is not a guard.
    """
    # 1. The engine, from cached lifecycle state — never by reaching for it.
    try:
        from ragtools.service.app import storage_is_down

        down = storage_is_down()
        if down:
            return "engine_down", down
    except Exception:  # noqa: BLE001 — no app context (CLI/tests); fall through
        pass

    # 2. The store itself. `storage_reachable` is TTL-cached, so this is free on
    #    any path that has already rendered a status page.
    try:
        ok, detail = owner.storage_reachable()
        if not ok:
            return "storage_unreachable", (
                f"the vector store is not reachable ({detail}). "
                f"{operation} would drop collections it could not rebuild.")
    except Exception as exc:  # noqa: BLE001
        return "storage_unreachable", f"the vector store could not be probed: {exc}"

    # 3. A migration owns the index until it finishes. Rebuilding underneath it
    #    destroys the work it has already done and orphans its plan.
    try:
        from ragtools.upgrade import relayout

        plan = relayout.active_plan(owner.settings)
        if plan is not None:
            report = relayout.progress(owner.settings, plan)
            if report is not None and not report.complete:
                return "migration_active", (
                    f"a layout migration is in progress ({report.describe()}). "
                    f"Wait for it to finish before running {operation}.")
    except Exception:  # noqa: BLE001 — no migration store means no migration
        pass

    # 4. Another indexing run. `rebuild` drops every collection and deletes the
    #    state DB; doing that under a running index leaves two writers, one of
    #    them destructive.
    try:
        if owner.indexing:
            return "index_busy", (
                f"an indexing run is in progress. {operation} would drop the "
                f"collections it is writing into.")
    except Exception:  # noqa: BLE001
        pass

    return "", ""


def assert_allowed(owner, *, operation: str = "rebuild") -> None:
    """Raise :class:`OperationRefused` unless ``operation`` may proceed."""
    code, reason = blocking_reason(owner, operation=operation)
    if code:
        raise OperationRefused(reason, code=code)


@contextmanager
def destructive_operation(owner, *, operation: str = "rebuild"):
    """Hold the destructive lock for the whole operation, or refuse.

    Non-blocking on purpose: a caller who waits behind a half-hour rebuild has
    almost certainly clicked twice, and telling them so is better than queueing
    a second index-destroying operation behind the first.
    """
    if not _operation_lock.acquire(blocking=False):
        raise OperationRefused(
            f"another destructive operation is already running; {operation} was "
            f"not started", code="operation_in_progress")
    try:
        assert_allowed(owner, operation=operation)
        yield
    finally:
        _operation_lock.release()


# --- interrupted-rebuild marker ------------------------------------------


def _intent_path(settings) -> Path:
    return Path(settings.data_dir) / INTENT_NAME


def record_intent(settings, payload: dict) -> None:
    """Persist what a rebuild is about to do, before it becomes irreversible."""
    try:
        path = _intent_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**payload, "at": time.time()}, indent=2),
                        encoding="utf-8")
    except OSError as exc:
        logger.warning("could not record rebuild intent: %s", exc)


def clear_intent(settings) -> None:
    try:
        _intent_path(settings).unlink(missing_ok=True)
    except OSError:
        pass


def pending_intent(settings) -> Optional[dict]:
    """A rebuild that started and never finished, or None."""
    try:
        path = _intent_path(settings)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
