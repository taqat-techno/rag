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

import threading

import pytest

from ragtools.runtime_store import RuntimeStore


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
