"""Tests for index-freshness detection (report finding A-008).

`compute_index_freshness` is a pure function (mirrors compute_scale_warning) so
/api/status, /health and `rag doctor` can warn when the index is stale — the gap
the investigation flagged: last_indexed was displayed but never age-evaluated.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ragtools.service.owner import compute_index_freshness


def test_never_indexed():
    r = compute_index_freshness(None)
    assert r["level"] == "never"
    assert r["age_seconds"] is None


def test_fresh_within_threshold():
    now = datetime(2026, 6, 29, 12, 0, 0)
    li = (now - timedelta(hours=2)).isoformat()
    r = compute_index_freshness(li, stale_after_hours=24, now=now)
    assert r["level"] == "fresh"
    assert 7100 < r["age_seconds"] < 7300
    assert r["message"] == ""


def test_stale_past_threshold():
    now = datetime(2026, 6, 29, 12, 0, 0)
    li = (now - timedelta(hours=48)).isoformat()
    r = compute_index_freshness(li, stale_after_hours=24, now=now)
    assert r["level"] == "stale"
    assert r["age_seconds"] > 24 * 3600
    assert r["message"]  # carries an advisory


def test_exactly_at_threshold_is_fresh():
    now = datetime(2026, 6, 29, 12, 0, 0)
    li = (now - timedelta(hours=24)).isoformat()
    r = compute_index_freshness(li, stale_after_hours=24, now=now)
    assert r["level"] == "fresh"  # boundary inclusive


def test_unparseable_timestamp():
    r = compute_index_freshness("not-a-timestamp")
    assert r["level"] == "unknown"
    assert r["age_seconds"] is None


def test_tz_aware_last_indexed_does_not_crash():
    now = datetime(2026, 6, 29, 12, 0, 0)
    li = "2026-06-29T10:00:00+00:00"  # tz-aware vs naive now
    r = compute_index_freshness(li, stale_after_hours=24, now=now)
    assert r["level"] in ("fresh", "stale")
    assert r["age_seconds"] is not None


def test_future_timestamp_clamps_to_zero():
    now = datetime(2026, 6, 29, 12, 0, 0)
    li = (now + timedelta(hours=1)).isoformat()
    r = compute_index_freshness(li, stale_after_hours=24, now=now)
    assert r["age_seconds"] == 0.0
    assert r["level"] == "fresh"


def test_record_has_stable_shape():
    keys = set(compute_index_freshness(None).keys())
    assert keys == {"level", "last_indexed", "age_seconds", "stale_after_hours", "message"}


def test_config_default_threshold():
    from ragtools.config import Settings
    assert Settings().stale_index_hours == 24


def test_get_status_includes_freshness(tmp_path):
    from unittest.mock import MagicMock
    from ragtools.config import Settings
    from ragtools.service.owner import QdrantOwner

    fake_client = MagicMock()
    fake_client.get_collection.return_value = MagicMock(points_count=10)
    settings = Settings(
        qdrant_path=str(tmp_path / "q"),
        state_db=str(tmp_path / "s.db"),
        collection_name="markdown_kb",
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ragtools.service.owner.ensure_collection", lambda *a, **k: None)
        mp.setattr("ragtools.service.owner.Encoder", lambda *a, **k: MagicMock(dimension=384))
        owner = QdrantOwner(settings=settings, client=fake_client)
    status = owner.get_status()
    assert "freshness" in status
    assert status["freshness"]["level"] == "never"  # empty state DB
