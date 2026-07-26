"""The UI must stay responsive while an index is running.

Reported from real use: during indexing the whole UI lags, "especially the map".

Three compounding causes, all confirmed in source:

1. ``get_status`` / ``get_projects`` / ``get_map_points`` acquire the SAME
   ``QdrantOwner._lock`` that indexing holds across every encode/upsert batch.
   (``search`` deliberately does not — which is why search stayed usable.)
2. ``compute_map_points`` scrolls EVERY point in 500-row pages and runs PCA over
   the whole collection.
3. ``_invalidate_map_cache()`` fires at the end of every index (4 call sites), so
   the map recomputes from scratch each time.

Plan: docs/planning/RAG_MODERNIZATION_MASTER_PLAN.md  (defect fix F2/F3/F4)
"""

import tempfile
import threading
import time
from pathlib import Path

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.service.owner import QdrantOwner


@pytest.fixture
def owner():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj = root / "proj"
        proj.mkdir()
        for i in range(6):
            (proj / f"doc{i}.md").write_text(f"# Doc {i}\n\nSome content number {i}.\n",
                                             encoding="utf-8")
        settings = Settings(
            content_root=str(root),
            qdrant_path=str(root / "qdrant"),
            state_db=str(root / "state.db"),
            projects=[ProjectConfig(id="p", path=str(proj), mode="docs")],
        )
        o = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        o.run_full_index(project_id="p")
        yield o
        o.close()


# --- F2: status must not block behind the index lock --------------------


def test_status_returns_promptly_while_the_index_lock_is_held(owner):
    """The dashboard polls this; it must never hang for the length of an index."""
    holding = threading.Event()
    release = threading.Event()

    def hog():
        with owner._lock:
            holding.set()
            release.wait(10)

    t = threading.Thread(target=hog, daemon=True)
    t.start()
    assert holding.wait(5)
    try:
        t0 = time.perf_counter()
        status = owner.get_status()
        elapsed = time.perf_counter() - t0
        assert elapsed < 2.0, f"get_status blocked for {elapsed:.1f}s behind the index lock"
        assert status is not None
        # It must be honest about being a snapshot rather than pretending.
        assert status.get("stale") is True
    finally:
        release.set()
        t.join(timeout=5)


def test_status_is_fresh_and_not_stale_when_uncontended(owner):
    s = owner.get_status()
    assert s.get("stale", False) is False
    assert s["total_chunks"] > 0


def test_projects_listing_also_survives_a_held_lock(owner):
    holding = threading.Event()
    release = threading.Event()

    def hog():
        with owner._lock:
            holding.set()
            release.wait(10)

    t = threading.Thread(target=hog, daemon=True)
    t.start()
    assert holding.wait(5)
    try:
        t0 = time.perf_counter()
        owner.get_projects()
        assert time.perf_counter() - t0 < 2.0
    finally:
        release.set()
        t.join(timeout=5)


# --- F3: the map must be bounded and must not recompute constantly ------


def test_map_is_sampled_not_unbounded(owner):
    """PCA over every point does not scale; a scatter overview only needs a sample."""
    from ragtools.service import map_data
    assert hasattr(map_data, "MAP_MAX_POINTS")
    assert 500 <= map_data.MAP_MAX_POINTS <= 20000


def test_indexing_marks_the_map_stale_instead_of_destroying_the_cache(owner):
    """Hard invalidation forced a full recompute on the next view — during and
    right after indexing, exactly when the machine is busiest."""
    owner.get_map_points()                       # populate the cache
    owner.run_incremental_index(project_id="p")  # would previously nuke it

    from ragtools.service.map_data import load_cached_map
    cached = load_cached_map(owner._settings.state_db)
    assert cached is not None, "index destroyed the map cache instead of marking it stale"


def test_map_serves_the_cache_immediately_when_stale(owner):
    owner.get_map_points()
    owner.run_incremental_index(project_id="p")
    t0 = time.perf_counter()
    pts = owner.get_map_points()
    assert time.perf_counter() - t0 < 2.0
    assert isinstance(pts, list)


def test_map_recompute_can_still_be_forced(owner):
    pts = owner.get_map_points(force_recompute=True)
    assert isinstance(pts, list)
