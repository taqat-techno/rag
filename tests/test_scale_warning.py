"""Tests for Qdrant local-mode scale warnings.

Field-report context: Mahmoud's install hit 27,604 points, past Qdrant's
own "do not run local-mode above 20,000 points" threshold. These tests pin
the warning levels and the structure of the warning record exposed via
/api/status so the admin panel and rag doctor can react consistently.
"""

from __future__ import annotations

import pytest

from ragtools.service.owner import (
    compute_scale_warning,
    _QDRANT_LOCAL_SOFT_WARN,
    _QDRANT_LOCAL_HARD_WARN,
)


def test_thresholds_are_sensible():
    """Guard the policy: soft must be below hard, both must be below 100k."""
    assert 0 < _QDRANT_LOCAL_SOFT_WARN < _QDRANT_LOCAL_HARD_WARN < 100_000


def test_well_below_soft_limit_is_ok():
    r = compute_scale_warning(5_000)
    assert r["level"] == "ok"
    assert r["message"] == ""
    assert r["points_count"] == 5_000
    assert r["soft_limit"] == _QDRANT_LOCAL_SOFT_WARN
    assert r["hard_limit"] == _QDRANT_LOCAL_HARD_WARN


def test_just_below_soft_limit_is_ok():
    r = compute_scale_warning(_QDRANT_LOCAL_SOFT_WARN - 1)
    assert r["level"] == "ok"


def test_at_soft_limit_is_approaching():
    r = compute_scale_warning(_QDRANT_LOCAL_SOFT_WARN)
    assert r["level"] == "approaching"
    assert "approaching" in r["message"].lower()
    assert f"{_QDRANT_LOCAL_HARD_WARN:,}" in r["message"]


def test_between_soft_and_hard_is_approaching():
    r = compute_scale_warning(17_500)
    assert r["level"] == "approaching"


def test_just_below_hard_limit_is_approaching():
    r = compute_scale_warning(_QDRANT_LOCAL_HARD_WARN - 1)
    assert r["level"] == "approaching"


def test_at_hard_limit_is_over():
    r = compute_scale_warning(_QDRANT_LOCAL_HARD_WARN)
    assert r["level"] == "over"
    assert "above" in r["message"].lower()


def test_well_above_hard_limit_is_over():
    """Mahmoud's actual field value: 27,604 points."""
    r = compute_scale_warning(27_604)
    assert r["level"] == "over"
    assert "27,604" in r["message"]


def test_zero_points_is_ok():
    r = compute_scale_warning(0)
    assert r["level"] == "ok"


def test_record_has_stable_shape():
    """The keys of the returned dict are a public API used by /api/status."""
    keys = set(compute_scale_warning(10).keys())
    assert keys == {"level", "points_count", "soft_limit", "hard_limit", "message"}


def test_status_endpoint_includes_scale_field(tmp_path):
    """/api/status must include the scale record so admin UI can surface it."""
    from unittest.mock import MagicMock

    from ragtools.config import Settings
    from ragtools.service.owner import QdrantOwner

    fake_client = MagicMock()
    fake_client.get_collection.return_value = MagicMock(points_count=23_000)

    settings = Settings(
        qdrant_path=str(tmp_path / "qdrant"),
        state_db=str(tmp_path / "state.db"),
        collection_name="markdown_kb",
    )

    # Build an owner without actually loading the encoder
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "ragtools.service.owner.ensure_collection",
            lambda *a, **k: None,
        )
        mp.setattr(
            "ragtools.service.owner.Encoder",
            lambda *a, **k: MagicMock(dimension=384),
        )
        owner = QdrantOwner(settings=settings, client=fake_client)

    status = owner.get_status()
    assert "scale" in status
    assert status["scale"]["level"] == "over"
    assert status["scale"]["points_count"] == 23_000
