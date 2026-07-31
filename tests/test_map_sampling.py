"""The Semantic Map must represent every eligible project.

v3.4 spent ONE global point budget walking collections in registry order and
``break``-ing out of the collection loop when it ran dry. Collections 3..15
received zero Qdrant calls, the footer asserted "385 files across 2 projects"
as fact while 5,383 files existed across 15, and ``?project=rag`` answered
``count: 0`` for a project holding 1,716 chunks.

The suite could not see any of it: the largest multi-project fixture indexed
TWO projects and no test ever crossed the 5,000-point budget, so the sampling
branch had zero coverage. These tests are built at N=15 with more points than
the budget, because that is the only shape in which the defect exists.
"""

from __future__ import annotations

import numpy as np
import pytest

from ragtools.service import map_data
from ragtools.service.map_data import compute_map_points


class _Point:
    __slots__ = ("id", "payload", "vector")

    def __init__(self, pid, payload, vector):
        self.id, self.payload, self.vector = pid, payload, vector


class FakeClient:
    """A Qdrant stand-in that pages, filters, and can fail one collection.

    Deliberately NOT a MagicMock: the defect under test is about paging and
    loop control, which a mock would happily pretend to do correctly.
    """

    def __init__(self, collections: dict[str, list[_Point]], broken: set[str] | None = None):
        self._collections = collections
        self._broken = broken or set()
        self.scrolled: list[str] = []

    def scroll(self, collection_name, limit=10, offset=None,
               with_payload=None, with_vectors=False, scroll_filter=None, **kw):
        if collection_name in self._broken:
            raise RuntimeError("collection unavailable")
        self.scrolled.append(collection_name)
        points = self._collections[collection_name]

        if scroll_filter is not None:
            wanted = set()
            for cond in scroll_filter.must:
                wanted.update(cond.match.any)
            points = [p for p in points if p.payload["file_path"] in wanted]

        start = int(offset or 0)
        window = points[start:start + limit]
        nxt = start + limit if start + limit < len(points) else None

        out = []
        for p in window:
            payload = dict(p.payload)
            out.append(_Point(p.id, payload, p.vector if with_vectors else None))
        return out, nxt


def _corpus(n_projects=15, files_per_project=40, chunks_per_file=10, dim=8):
    """N projects, each with the same shape, so imbalance can only come from the sampler."""
    rng = np.random.default_rng(1234)
    collections: dict[str, list[_Point]] = {}
    for p in range(n_projects):
        name = f"proj_{p:032x}"
        pid = f"project-{p:02d}"
        pts = []
        for f in range(files_per_project):
            fp = f"{pid}/dir{f % 7}/file_{f:03d}.md"
            for c in range(chunks_per_file):
                pts.append(_Point(
                    f"{p}-{f}-{c}",
                    {"file_path": fp, "project_id": pid, "headings": [f"# {fp}"]},
                    rng.normal(size=dim).astype(np.float32).tolist(),
                ))
        collections[name] = pts
    return collections


@pytest.fixture
def settings():
    from ragtools.config import Settings
    return Settings()


def test_every_eligible_project_is_represented(settings):
    """The headline regression: 15 projects in, 15 projects on the map.

    Total points here (6,000) exceed the old 5,000 budget, which is exactly the
    condition under which v3.4 dropped 13 of 15 collections.
    """
    collections = _corpus()
    client = FakeClient(collections)

    result = compute_map_points(client, settings, list(collections))

    represented = {p["project_id"] for p in result["points"]}
    expected = {f"project-{i:02d}" for i in range(15)}
    missing = expected - represented
    assert not missing, f"map omitted projects: {sorted(missing)}"
    assert result["coverage"]["projects_represented"] == 15


def test_no_project_is_starved_by_enumeration_order(settings):
    """Balance, not just presence.

    Raising the cap alone would make the previous test pass while leaving the
    real defect — visibility decided by registry order — untouched. Equal-sized
    projects must receive comparable sampling.
    """
    collections = _corpus()
    result = compute_map_points(FakeClient(collections), settings, list(collections))

    from collections import Counter
    per = Counter(p["project_id"] for p in result["points"])
    assert len(per) == 15
    assert min(per.values()) > 0
    # Equal-sized projects: the spread must not favour the first-enumerated.
    assert max(per.values()) <= 3 * min(per.values()), f"unbalanced sampling: {per}"


def test_a_plotted_file_is_positioned_from_all_of_its_chunks(settings):
    """Coordinates must be true, not merely present.

    v3.4 averaged whatever chunks fell inside the prefix, so 347 of 375
    displayed files were plotted from an incomplete mean. chunk_count is the
    observable proxy: it must equal the file's real chunk total.
    """
    collections = _corpus(n_projects=3, files_per_project=5, chunks_per_file=10)
    result = compute_map_points(FakeClient(collections), settings, list(collections))

    assert result["points"], "expected a non-empty map"
    for point in result["points"]:
        assert point["chunk_count"] == 10, (
            f"{point['file_path']} plotted from {point['chunk_count']} of 10 chunks"
        )


def test_one_unavailable_collection_does_not_hide_the_others(settings):
    """A failing collection is reported, not silently dropped.

    The v3.4 loop caught every exception and moved on with no log, no counter
    and no field in the response, so "this project has no files" and "Qdrant
    refused this collection" were the same answer.
    """
    collections = _corpus(n_projects=5, files_per_project=10, chunks_per_file=4)
    broken = sorted(collections)[2]
    result = compute_map_points(FakeClient(collections, broken={broken}), settings,
                                list(collections))

    represented = {p["project_id"] for p in result["points"]}
    assert len(represented) == 4, "a broken collection took the others down"
    reasons = {e["collection"]: e["reason"] for e in result["excluded"]}
    assert broken in reasons, f"broken collection not reported: {result['excluded']}"
    assert "unreadable" in reasons[broken]


def test_coverage_is_reported_honestly(settings):
    """The map states what it drew and what it did not.

    The footer previously asserted its sample as the whole truth. Coverage must
    expose eligible-vs-sampled so the UI can say "showing N of M".
    """
    collections = _corpus(n_projects=15, files_per_project=40, chunks_per_file=10)
    result = compute_map_points(FakeClient(collections), settings, list(collections))

    cov = result["coverage"]
    assert cov["projects_total"] == 15
    assert cov["files_eligible"] == 15 * 40
    assert cov["files_sampled"] == len(result["points"])
    assert cov["files_sampled"] <= cov["files_eligible"]
    assert cov["truncated"] is (cov["files_sampled"] < cov["files_eligible"])
    assert len(cov["per_project"]) == 15
    for entry in cov["per_project"]:
        assert entry["eligible"] == 40
        assert entry["sampled"] > 0


def test_sampling_is_deterministic(settings):
    """Two computations of an unchanged index must agree.

    Points that migrate across the canvas without their content changing make
    the map unreadable as a reference.
    """
    collections = _corpus(n_projects=4, files_per_project=30, chunks_per_file=3)
    first = compute_map_points(FakeClient(collections), settings, list(collections))
    second = compute_map_points(FakeClient(collections), settings, list(collections))

    assert [p["file_path"] for p in first["points"]] == \
           [p["file_path"] for p in second["points"]]


def test_no_collections_means_no_legacy_fallback(settings):
    """An empty routed set must not fall back to the configured collection name.

    Under per_project that name identifies nothing that exists; querying it is
    the v2 question and it returned a confidently empty map.
    """
    client = FakeClient({})
    result = compute_map_points(client, settings, [])

    assert result["points"] == []
    assert client.scrolled == [], "fell back to a collection nobody asked for"


def test_a_small_project_survives_a_large_neighbour(settings):
    """Size must not decide visibility.

    In production a 32-point project was invisible while a 6,341-point
    neighbour consumed the budget — and a 2-point project vanished entirely in
    the isolated reproduction.
    """
    collections = _corpus(n_projects=1, files_per_project=400, chunks_per_file=10)
    tiny = dict(_corpus(n_projects=1, files_per_project=2, chunks_per_file=1))
    # rename so the tiny project sorts/enumerates AFTER the large one
    (tiny_name, tiny_points), = tiny.items()
    for p in tiny_points:
        p.payload["project_id"] = "tiny"
        p.payload["file_path"] = "tiny/" + p.payload["file_path"].split("/")[-1]
    collections["zzz_tiny"] = tiny_points

    result = compute_map_points(FakeClient(collections), settings, list(collections))

    assert "tiny" in {p["project_id"] for p in result["points"]}, \
        "the small project was squeezed out by its large neighbour"


def test_map_max_files_bounds_the_result(settings):
    """The map stays an overview.

    Representing every project must not turn into scrolling every point — the
    cost that made the UI crawl during indexing in the first place.
    """
    collections = _corpus(n_projects=10, files_per_project=500, chunks_per_file=2)
    result = compute_map_points(FakeClient(collections), settings, list(collections))

    assert len(result["points"]) <= map_data.MAP_MAX_FILES * 1.1
    assert result["coverage"]["truncated"] is True
