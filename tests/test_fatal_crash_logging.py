"""Tests for the fatal-crash recorder added in run.py.

The field report showed the service dying without any line in service.log
and no artifact to examine. These tests verify that any exception that
reaches the uvicorn-wrapping try-block is captured in both the log and a
last_crash.json marker.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from ragtools.config import Settings
from ragtools.service.run import _record_fatal_crash


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        qdrant_path=str(tmp_path / "data" / "qdrant"),
        state_db=str(tmp_path / "data" / "index_state.db"),
    )


def test_records_crash_to_log_at_critical(tmp_path, caplog):
    settings = _make_settings(tmp_path)
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        with caplog.at_level(logging.CRITICAL, logger="ragtools.service"):
            _record_fatal_crash(settings, exc, "127.0.0.1", 21420)

    critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert critical, "expected at least one CRITICAL log record"
    msg = critical[0].getMessage()
    assert "Service crashed" in msg
    assert "boom" in msg
    assert "RuntimeError" in msg


def test_writes_last_crash_marker(tmp_path):
    settings = _make_settings(tmp_path)
    try:
        raise ValueError("qdrant exploded")
    except ValueError as exc:
        _record_fatal_crash(settings, exc, "127.0.0.1", 21420)

    marker = tmp_path / "data" / "logs" / "last_crash.json"
    assert marker.exists(), "last_crash.json must be written next to service.log"

    data = json.loads(marker.read_text())
    assert data["exception_type"] == "ValueError"
    assert data["message"] == "qdrant exploded"
    assert data["port"] == 21420
    assert data["host"] == "127.0.0.1"
    assert "traceback" in data and "ValueError" in data["traceback"]
    assert "timestamp" in data
    assert isinstance(data["memory"], dict)


def test_recorder_survives_keyboard_interrupt_class(tmp_path):
    """KeyboardInterrupt is a BaseException, not Exception. Recorder must handle it."""
    settings = _make_settings(tmp_path)
    try:
        raise KeyboardInterrupt("user pressed ctrl-c")
    except KeyboardInterrupt as exc:
        _record_fatal_crash(settings, exc, "127.0.0.1", 21420)

    marker = tmp_path / "data" / "logs" / "last_crash.json"
    assert marker.exists()
    data = json.loads(marker.read_text())
    assert data["exception_type"] == "KeyboardInterrupt"


def test_recorder_is_idempotent_on_missing_log_dir(tmp_path):
    """Recorder must create the log directory if it doesn't exist yet."""
    settings = _make_settings(tmp_path)
    log_dir = tmp_path / "data" / "logs"
    assert not log_dir.exists()  # fresh machine scenario

    try:
        raise RuntimeError("first crash on a fresh install")
    except RuntimeError as exc:
        _record_fatal_crash(settings, exc, "127.0.0.1", 21420)

    assert log_dir.exists()
    assert (log_dir / "last_crash.json").exists()


def test_recorder_does_not_raise_when_marker_write_fails(tmp_path, monkeypatch):
    """A crash during crash recording must not propagate."""
    settings = _make_settings(tmp_path)

    real_write_text = Path.write_text

    def explode(self, *args, **kwargs):
        if self.name == "last_crash.json":
            raise OSError("disk full, naturally")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", explode)

    try:
        raise RuntimeError("primary crash")
    except RuntimeError as exc:
        # Must not raise a second time
        _record_fatal_crash(settings, exc, "127.0.0.1", 21420)
