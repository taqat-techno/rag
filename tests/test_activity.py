"""Tests for the activity log system."""

from ragtools.service.activity import ActivityLog, ActivityEvent, log_activity, activity_log


def test_emit_creates_event():
    log = ActivityLog(maxlen=100)
    log.emit("info", "service", "Test message")
    events = log.get_recent()
    assert len(events) == 1
    assert events[0].level == "info"
    assert events[0].source == "service"
    assert events[0].message == "Test message"


def test_event_has_id_and_timestamp():
    log = ActivityLog(maxlen=100)
    log.emit("info", "test", "msg")
    event = log.get_recent()[0]
    assert event.id == 1
    assert "T" in event.timestamp  # ISO format


def test_events_ordered_by_id():
    log = ActivityLog(maxlen=100)
    log.emit("info", "a", "first")
    log.emit("info", "b", "second")
    log.emit("info", "c", "third")
    events = log.get_recent()
    assert len(events) == 3
    assert events[0].id < events[1].id < events[2].id


def test_ring_buffer_maxlen():
    log = ActivityLog(maxlen=5)
    for i in range(10):
        log.emit("info", "test", f"msg {i}")
    events = log.get_recent()
    assert len(events) == 5
    assert events[0].message == "msg 5"
    assert events[-1].message == "msg 9"


def test_get_recent_limit():
    log = ActivityLog(maxlen=100)
    for i in range(20):
        log.emit("info", "test", f"msg {i}")
    events = log.get_recent(limit=5)
    assert len(events) == 5
    assert events[-1].message == "msg 19"


def test_get_recent_after_id():
    log = ActivityLog(maxlen=100)
    for i in range(10):
        log.emit("info", "test", f"msg {i}")
    # Get events after ID 7
    events = log.get_recent(after_id=7)
    assert len(events) == 3
    assert events[0].id == 8


def test_count():
    log = ActivityLog(maxlen=100)
    assert log.count() == 0
    log.emit("info", "test", "msg")
    assert log.count() == 1


def test_latest_id():
    log = ActivityLog(maxlen=100)
    assert log.latest_id() == 0
    log.emit("info", "test", "msg")
    assert log.latest_id() == 1
    log.emit("info", "test", "msg2")
    assert log.latest_id() == 2


def test_event_levels():
    log = ActivityLog(maxlen=100)
    for level in ["info", "success", "warning", "error"]:
        log.emit(level, "test", f"{level} message")
    events = log.get_recent()
    assert [e.level for e in events] == ["info", "success", "warning", "error"]


def test_event_with_details():
    log = ActivityLog(maxlen=100)
    log.emit("info", "test", "msg", details="extra detail")
    event = log.get_recent()[0]
    assert event.details == "extra detail"


def test_event_without_details():
    log = ActivityLog(maxlen=100)
    log.emit("info", "test", "msg")
    event = log.get_recent()[0]
    assert event.details is None


def test_to_dict():
    log = ActivityLog(maxlen=100)
    log.emit("success", "indexer", "Done", details="3 files")
    d = log.get_recent()[0].to_dict()
    assert d["level"] == "success"
    assert d["source"] == "indexer"
    assert d["message"] == "Done"
    assert d["details"] == "3 files"
    assert "id" in d
    assert "timestamp" in d


def test_global_log_activity():
    """The convenience function works with the global singleton."""
    initial = activity_log.count()
    log_activity("info", "test", "global test")
    assert activity_log.count() == initial + 1


def test_thread_safety():
    """Basic thread safety — no crashes under concurrent access."""
    import threading
    log = ActivityLog(maxlen=100)

    def writer():
        for i in range(50):
            log.emit("info", "thread", f"msg {i}")

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert log.count() == 100  # maxlen caps at 100, 200 total emitted
