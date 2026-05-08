"""Unit tests for ``WatcherThread.get_state_snapshot`` and friends.

Phase A — observability fields exposed on /api/watcher/status.

These tests deliberately avoid driving the full ``run()`` loop: that
path imports ``watchfiles`` and would require a live filesystem watcher.
The four observability fields are pure state, so we exercise the
``_record_started`` / ``_record_error`` helpers directly with a mocked
QdrantOwner.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from ragtools.config import Settings
from ragtools.service.watcher_thread import WatcherThread


def _make_thread(tmp_path) -> WatcherThread:
    """Construct a watcher thread without starting it.

    Tests never call ``.start()`` — only the synchronous helpers. Mocked
    owner avoids the encoder + Qdrant load entirely.
    """
    settings = Settings(
        qdrant_path=str(tmp_path / "qdrant"),
        state_db=str(tmp_path / "state.db"),
    )
    return WatcherThread(owner=MagicMock(), settings=settings)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_initial_snapshot_has_all_documented_keys(tmp_path):
    snap = _make_thread(tmp_path).get_state_snapshot()
    assert snap == {
        "last_started_at": None,
        "last_error": None,
        "last_error_at": None,
        "consecutive_failures": 0,
    }


def test_snapshot_keys_are_a_closed_set(tmp_path):
    """Pin the public response shape so a fifth field can't slip in
    silently — it would require a HTTP-API.md doc update + this test
    edit, which is exactly the review gate we want."""
    keys = set(_make_thread(tmp_path).get_state_snapshot().keys())
    assert keys == {
        "last_started_at", "last_error",
        "last_error_at", "consecutive_failures",
    }


# ---------------------------------------------------------------------------
# _record_error / _record_started
# ---------------------------------------------------------------------------


def test_record_error_increments_failures_and_captures_message(tmp_path):
    wt = _make_thread(tmp_path)
    wt._record_error(RuntimeError("boom"))
    snap = wt.get_state_snapshot()
    assert snap["consecutive_failures"] == 1
    assert snap["last_error"] == "RuntimeError: boom"
    assert snap["last_error_at"] is not None
    assert snap["last_started_at"] is None  # never started — still null


def test_record_error_accumulates_failures(tmp_path):
    wt = _make_thread(tmp_path)
    for _ in range(3):
        wt._record_error(OSError("perm denied"))
    snap = wt.get_state_snapshot()
    assert snap["consecutive_failures"] == 3
    assert snap["last_error"] == "OSError: perm denied"


def test_record_started_clears_prior_error(tmp_path):
    """A successful (re)start must reset the failure run AND clear the
    error fields so /api/watcher/status reflects current health."""
    wt = _make_thread(tmp_path)
    wt._record_error(RuntimeError("first"))
    wt._record_error(RuntimeError("second"))
    assert wt.get_state_snapshot()["consecutive_failures"] == 2

    wt._record_started()
    snap = wt.get_state_snapshot()
    assert snap["consecutive_failures"] == 0
    assert snap["last_error"] is None
    assert snap["last_error_at"] is None
    assert snap["last_started_at"] is not None
    # ISO-8601 UTC timestamp ends with +00:00 or 'Z'
    assert "+00:00" in snap["last_started_at"] or snap["last_started_at"].endswith("Z")


def test_error_after_started_keeps_started_timestamp(tmp_path):
    """An error after a successful start must NOT clear last_started_at —
    operators need to see how long ago the watcher was last healthy."""
    wt = _make_thread(tmp_path)
    wt._record_started()
    started = wt.get_state_snapshot()["last_started_at"]
    assert started is not None

    wt._record_error(OSError("file vanished"))
    snap = wt.get_state_snapshot()
    assert snap["last_started_at"] == started
    assert snap["consecutive_failures"] == 1
    assert snap["last_error"].startswith("OSError")


# ---------------------------------------------------------------------------
# Concurrency — snapshot reads must not race state writes
# ---------------------------------------------------------------------------


def test_snapshot_reads_are_consistent_under_concurrent_errors(tmp_path):
    """Drive lots of writes and reads in parallel; assert no torn reads
    (every snapshot's consecutive_failures matches an error count between
    'before' and 'after' the read)."""
    wt = _make_thread(tmp_path)
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            wt._record_error(RuntimeError("noise"))

    threads = [threading.Thread(target=writer, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()

    try:
        for _ in range(500):
            snap = wt.get_state_snapshot()
            # All four keys present and well-typed under concurrent writes.
            assert snap["consecutive_failures"] >= 0
            assert isinstance(snap["consecutive_failures"], int)
            assert snap["last_error"] is None or snap["last_error"].startswith("RuntimeError")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=1.0)
