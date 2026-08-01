"""Closing the runtime store while a thread is writing to it segfaulted CI.

`Fatal Python error: Segmentation fault`, exit 139, on a Linux runner:

    Thread A: watcher_thread.run -> log_activity -> app._sink
              -> RuntimeStore.append_event -> _emit -> self.conn.execute(...)
    Thread B: lifespan teardown -> stop_runtime -> RuntimeStore.close
              -> self.conn.close()

`close()` was the ONLY method on the class that did not take ``self._lock`` —
twelve others hold it across their ``self.conn`` calls. So it could free the
sqlite3 connection underneath a C-level ``execute`` running in another thread,
which is a use-after-free, not a Python exception.

Two things had to be true for this to be reachable, and both were:

* the watcher is a daemon thread that logs activity continuously, and
* the lifespan started it (M3) but never stopped it, so it was still running
  during teardown.

Both are fixed. These tests pin the store's own contract; the ordering fix is
asserted in `test_service.py`'s lifespan coverage.
"""

from __future__ import annotations

import logging
import sqlite3
import threading

import pytest

from ragtools.runtime_store import JobState, RuntimeStore


@pytest.fixture
def store(tmp_path):
    s = RuntimeStore(str(tmp_path / "runtime.db"), instance_id="test")
    yield s
    s.close()


# --- the contract -----------------------------------------------------------


def test_a_write_after_close_is_a_no_op_not_a_crash(store):
    """The watcher does not stop because we decided to shut down."""
    store.close()

    event = store.append_event("activity.info", "watcher", {"message": "late"})

    assert event.id == 0, "a post-close write claimed to have been persisted"
    assert event.type == "activity.info"


def test_closing_twice_is_safe(store):
    store.close()
    store.close()


def test_close_holds_the_same_lock_every_writer_takes():
    """Asserted structurally, because the race window is too small to hit
    reliably and the property is exact: `close` must not be the one method that
    touches `self.conn` outside the lock."""
    import ast
    import inspect

    source = inspect.getsource(RuntimeStore.close)
    tree = ast.parse(source.lstrip() if source.startswith(" ") else source)

    withs = [n for n in ast.walk(tree) if isinstance(n, ast.With)]
    guarded = any(
        isinstance(item.context_expr, ast.Attribute)
        and item.context_expr.attr == "_lock"
        for w in withs for item in w.items
    )
    assert guarded, (
        "RuntimeStore.close does not hold self._lock; closing can free the "
        "sqlite3 connection underneath an in-flight execute in another thread"
    )


# --- the race itself --------------------------------------------------------


def test_hammering_writes_while_closing_never_raises(tmp_path):
    """The shape of the crash, run for real.

    Not a proof — a use-after-free is timing-dependent and a green run does not
    guarantee the window was hit. It is here because it FAILS loudly against the
    unguarded version (ProgrammingError from the closed connection, on the way
    to the segfault the C layer produces under real contention), and because a
    future change that reintroduces unsynchronised close has a decent chance of
    being caught by it.
    """
    store = RuntimeStore(str(tmp_path / "runtime.db"), instance_id="test")
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            try:
                store.append_event("activity.info", "watcher", {"message": "tick"})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return

    threads = [threading.Thread(target=writer, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()

    for _ in range(200):
        store.append_event("activity.info", "service", {"message": "warm"})

    store.close()
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f"a writer raised while the store was closing: {errors[:3]}"


def test_the_store_still_works_normally(store):
    """The guard must not turn a healthy store into a silent no-op."""
    first = store.append_event("activity.info", "service", {"message": "a"})
    second = store.append_event("activity.info", "service", {"message": "b"})

    assert first.id > 0 and second.id > first.id
    assert len(store.events_after(0)) == 2


# --- the JOB half of the same contract --------------------------------------
#
# `_emit` was made safe after close and nothing else was, because the watcher
# was the only thread anybody had in mind. The job worker is the other one that
# legitimately outlives a shutdown, and it calls `is_cancel_requested` on every
# progress tick and `finish` exactly once. Both went straight at a closed
# connection and raised `sqlite3.ProgrammingError`, the second one from inside
# the handler for the first — so it escaped the worker thread and killed it,
# with its traceback holding every frame beneath, including the indexer's open
# `IndexState`. On Windows that pinned `state.db` and failed a release build.


def test_a_running_job_asking_whether_to_stop_is_told_yes_after_close(store):
    """The most-called store method from inside a handler.

    "Yes" is the honest answer: the store is gone because the service is going
    away. It also routes the handler into the `Cancelled` path it already has,
    so it unwinds instead of crashing on the next call.
    """
    job = store.submit("index", {})
    assert store.is_cancel_requested(job.id) is False

    store.close()

    assert store.is_cancel_requested(job.id) is True


def test_finishing_a_job_after_close_is_reported_not_raised(store, caplog):
    """A terminal state that could not be written down must be SAID."""
    job = store.submit("index", {})
    store.close()

    with caplog.at_level(logging.ERROR, logger="ragtools.runtime_store"):
        recorded = store.finish(job.id, JobState.FAILED, error="qdrant exploded")

    assert recorded is None, "a closed store claimed to have recorded the outcome"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert job.id in logged, f"the lost job was not named in the log: {logged!r}"
    assert "qdrant exploded" in logged, (
        "the error the job carried was dropped silently instead of reported")


def test_submitting_to_a_closed_store_refuses_rather_than_lying(store):
    """The one operation that must NOT answer. An accepted job id that nothing
    will ever run is worse than an error.

    Asserted on the exception's TYPE HIERARCHY rather than by importing the
    class, so this fails behaviourally on a build that has no such class — an
    ImportError would fail the whole module and prove nothing about behaviour.
    Three of the four `submit` call sites already catch `RuntimeError` (the one
    `get_runtime()` raises when there is no store at all) and fall back to a
    thread; a raw sqlite error is invisible to every one of them.
    """
    store.close()

    with pytest.raises(Exception) as caught:      # noqa: PT011 — see docstring
        store.submit("index", {})

    assert isinstance(caught.value, RuntimeError), (
        "submit raised a raw sqlite error, which the callers that fall back to "
        f"a thread on RuntimeError cannot see: {caught.value!r}")
    assert "closed" in str(caught.value).lower(), caught.value


def test_no_method_a_running_job_can_call_raises_after_close(store):
    """Behavioural sweep, not a source scan. Every method is CALLED.

    The bug was not "one method was missed"; it was that the post-close rule
    had been applied to exactly one method out of thirteen. So the assertion is
    over the whole surface, and a method added later without a guard fails
    here.
    """
    job = store.submit("index", {})
    store.close()

    calls = {
        "claim_next": lambda: store.claim_next(),
        "update_progress": lambda: store.update_progress(job.id, done=1, total=2),
        "finish": lambda: store.finish(job.id, JobState.FAILED, error="x"),
        "request_cancel": lambda: store.request_cancel(job.id),
        "is_cancel_requested": lambda: store.is_cancel_requested(job.id),
        "get_job": lambda: store.get_job(job.id),
        "list_jobs": lambda: store.list_jobs(),
        "active_jobs": lambda: store.active_jobs(),
        "recover_interrupted": lambda: store.recover_interrupted(),
        "append_event": lambda: store.append_event("activity.info", "watcher", {}),
        "events_after": lambda: store.events_after(0),
        "latest_event_id": lambda: store.latest_event_id(),
        "prune_events": lambda: store.prune_events(),
    }
    raised = {}
    for name, call in calls.items():
        try:
            call()
        except sqlite3.ProgrammingError as exc:
            raised[name] = str(exc)

    assert not raised, (
        "these methods raise on a closed store, so a background thread that "
        f"outlives shutdown dies instead of unwinding: {raised}")


def test_the_answers_are_empty_rather_than_invented(store):
    """A closed store must not report state it cannot read."""
    store.submit("index", {})
    store.append_event("activity.info", "service", {"message": "a"})
    store.close()

    assert store.claim_next() is None
    assert store.get_job("anything") is None
    assert store.list_jobs() == []
    assert store.active_jobs() == []
    assert store.recover_interrupted() == []
    assert store.events_after(0) == []
    assert store.latest_event_id() == 0
    assert store.prune_events() == 0
    assert store.closed is True


# --- the ordering fix -------------------------------------------------------


def test_every_shutdown_path_stops_the_watcher_first():
    """`autostart_watcher` had no counterpart, and `lifespan` has TWO shutdown
    paths — the injected-owner branch every service test takes, and the real
    one. Only the second was obvious, and the crash was observed on the first.

    So the assertion is not "the main path is correct" but "no path calls
    `stop_runtime` directly": the ordering lives in one function or it will be
    forgotten in one branch.
    """
    import ast
    import inspect

    from ragtools.service import app as app_module

    source = inspect.getsource(app_module.lifespan)
    called = [n.func.id for n in ast.walk(ast.parse(source.lstrip()))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]

    assert "stop_background_writers" in called, (
        "the lifespan does not stop the watcher, so it keeps writing to the "
        "runtime store while that store is being closed")
    assert "stop_runtime" not in called, (
        "a shutdown path closes the runtime store directly, skipping the "
        "watcher stop that has to happen first")


def test_the_helper_stops_the_watcher_before_the_store():
    import ast
    import inspect

    from ragtools.service import app as app_module

    source = inspect.getsource(app_module.stop_background_writers)
    # BY LINE NUMBER, not by walk order. `ast.walk` is breadth-first, so the
    # obvious `called.index(...)` compares tree-traversal positions and answers
    # a question nobody asked — it reported the calls in the wrong order for
    # correct code.
    calls = sorted(
        ((n.lineno, n.func.id) for n in ast.walk(ast.parse(source.lstrip()))
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)))
    order = [name for _, name in calls]

    assert order.index("stop_watcher_for_shutdown") < order.index("stop_runtime"), (
        f"the watcher is stopped AFTER the store it writes to is closed: {order}")


def test_stopping_a_watcher_that_is_not_running_is_fine():
    from ragtools.service.routes import stop_watcher_for_shutdown

    assert stop_watcher_for_shutdown()["status"] in {"not_running", "stopped"}
