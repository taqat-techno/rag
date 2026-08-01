"""The Semantic Map must represent every eligible project — and only ever
project vectors that are points in the same space.

Two defects, one file, because they are the same mistake at two altitudes: the
map asserting things about collections it never actually looked at.

**v3.4 sampling.** ONE global point budget was spent walking collections in
registry order, ``break``-ing out of the collection loop when it ran dry.
Collections 3..15 received zero Qdrant calls, the footer asserted "385 files
across 2 projects" as fact while 5,383 files existed across 15, and
``?project=rag`` answered ``count: 0`` for a project holding 1,716 chunks.

**v3.5.0 vector space.** Every routed collection's vectors were stacked into
ONE PCA with no check that they were comparable. A collection of a different
dimension raised an uncaught ``ValueError`` (HTTP 500 — the whole map gone,
including the projects that were fine); a named-vector collection raised
``TypeError`` the same way; and a same-dimension / different-model mix produced
a projection that separated ENCODERS rather than meaning, silently.

The suite could not see the first: the largest multi-project fixture indexed
TWO projects and no test ever crossed the 5,000-point budget. It could not see
the second either: every fixture used one dimension, one model and unnamed
vectors, so the only shape in which the defect exists was never built. Both
sets of tests below therefore construct the shape first.
"""

from __future__ import annotations

import sqlite3
import types

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

    Deliberately NOT a MagicMock: the defects under test are about paging, loop
    control and the SHAPE of what comes back, all of which a mock would happily
    pretend to do correctly.

    ``configs`` supplies ``get_collection`` answers — the declared half of a
    collection's identity (dimension, distance metric, vector type). A
    collection with no entry raises, which is the realistic case for an engine
    that cannot introspect: unknown, not wrong.
    """

    def __init__(self, collections: dict[str, list[_Point]],
                 broken: set[str] | None = None,
                 configs: dict[str, object] | None = None):
        self._collections = collections
        self._broken = broken or set()
        self._configs = configs or {}
        self.scrolled: list[str] = []

    def get_collection(self, collection_name):
        if collection_name in self._broken:
            raise RuntimeError("collection unavailable")
        if collection_name not in self._configs:
            raise RuntimeError(f"no collection info for {collection_name}")
        return types.SimpleNamespace(
            config=types.SimpleNamespace(params=self._configs[collection_name])
        )

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


def _params(size=8, distance="Cosine", datatype=None, named=None,
            sparse_vectors=None, multivector=False):
    """A ``get_collection().config.params`` stand-in."""
    vectors = types.SimpleNamespace(
        size=size, distance=distance, datatype=datatype,
        multivector_config=types.SimpleNamespace() if multivector else None,
    )
    if named is not None:
        vectors = {named: vectors}
    return types.SimpleNamespace(vectors=vectors, sparse_vectors=sparse_vectors)


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


def _collection(name, project_id, *, files=4, chunks=3, dim=8,
                vector_name=None, norm=None, seed=7):
    """ONE collection whose vector-space properties are stated, not incidental.

    ``dim`` / ``vector_name`` / ``norm`` are the three things that make two
    collections' vectors incomparable while every payload still looks alike —
    which is precisely why the old fixtures could not express the defect.
    """
    rng = np.random.default_rng(seed)
    points = []
    for f in range(files):
        file_path = f"{project_id}/file_{f:03d}.md"
        for c in range(chunks):
            vec = rng.normal(size=dim).astype(np.float32)
            if norm is not None:
                vec = (vec / np.linalg.norm(vec) * norm).astype(np.float32)
            raw = vec.tolist()
            points.append(_Point(
                f"{name}-{f}-{c}",
                {"file_path": file_path, "project_id": project_id,
                 "headings": [f"# {file_path}"]},
                {vector_name: raw} if vector_name else raw,
            ))
    return name, points


def _state_db(tmp_path, recorded=None, files=(("a.md", "h1"),)):
    """A state DB holding file hashes and per-project index identities.

    ``recorded`` is ``{collection_name: (model_name, dimension)}`` — the only
    place a model NAME is written down. Qdrant records a dimension and nothing
    else, so two collections embedded by different models at the same dimension
    are indistinguishable from the store alone.
    """
    from ragtools.index_identity import ProjectIdentity, project_meta_key

    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE file_state (file_path TEXT PRIMARY KEY, file_hash TEXT)")
    conn.executemany("INSERT INTO file_state VALUES (?, ?)", list(files))
    conn.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
    for i, (collection, (model, dim)) in enumerate(sorted((recorded or {}).items())):
        identity = ProjectIdentity(
            project_uuid=f"{i:032x}", collection_name=collection,
            generation=1, model_name=model, dimension=dim,
        )
        conn.execute("INSERT INTO index_meta VALUES (?, ?)",
                     (project_meta_key(f"project-{i}"), identity.to_json()))
    conn.commit()
    conn.close()
    return str(path)


def _settings_with_state(state_db):
    from ragtools.config import Settings
    return Settings(state_db=state_db)


def _reasons(result):
    return {e["collection"]: e.get("reason", "") for e in result["excluded"]}


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


# --- WP-R07: one projection means one vector space ------------------------


def test_a_collection_of_another_dimension_does_not_kill_the_map(settings):
    """The headline defect: one odd collection took EVERYTHING down.

    Stacking a 16-dim mean beside 8-dim means raises ``ValueError`` out of
    ``np.array``, which reaches the blanket handler as HTTP 500 — so the two
    projects that were perfectly fine disappeared because a third was not. The
    odd collection must be excluded WITH A REASON and the rest still drawn.
    """
    collections = dict([
        _collection("proj_alpha", "alpha"),
        _collection("proj_beta", "beta"),
        _collection("proj_gamma", "gamma", dim=16),
    ])
    result = compute_map_points(FakeClient(collections), settings, sorted(collections))

    assert {p["project_id"] for p in result["points"]} == {"alpha", "beta"}, \
        "an incompatible collection took the compatible ones down with it"
    reasons = _reasons(result)
    assert "proj_gamma" in reasons, f"excluded silently: {result['excluded']}"
    assert "dimension" in reasons["proj_gamma"].lower(), reasons["proj_gamma"]
    assert result["coverage"].get("collections_incompatible") == 1


def test_a_named_vector_collection_does_not_raise(settings):
    """``record.vector`` is a ``dict`` when the collection uses named vectors.

    ``np.array({"text": [...]}, dtype=np.float32)`` raises ``TypeError`` — the
    same 500, from a collection layout Qdrant supports and the map never
    considered. Named throughout is a perfectly coherent space: it must be
    PROJECTED, not merely survived.
    """
    collections = dict([
        _collection("proj_alpha", "alpha", vector_name="text"),
        _collection("proj_beta", "beta", vector_name="text"),
    ])
    result = compute_map_points(FakeClient(collections), settings, sorted(collections))

    assert {p["project_id"] for p in result["points"]} == {"alpha", "beta"}
    assert result["excluded"] == [], result["excluded"]


def test_named_and_unnamed_vectors_are_not_mixed_into_one_projection(settings):
    """Named vs unnamed is a space difference, not a formatting detail."""
    collections = dict([
        _collection("proj_alpha", "alpha"),
        _collection("proj_beta", "beta"),
        _collection("proj_gamma", "gamma", vector_name="text"),
    ])
    result = compute_map_points(FakeClient(collections), settings, sorted(collections))

    assert {p["project_id"] for p in result["points"]} == {"alpha", "beta"}
    reasons = _reasons(result)
    assert "proj_gamma" in reasons, f"excluded silently: {result['excluded']}"
    assert "vector name" in reasons["proj_gamma"].lower(), reasons["proj_gamma"]


def test_same_dimension_different_model_is_excluded_not_silently_mixed(tmp_path):
    """The silent failure, and the worse of the two.

    Nothing raises: the dimensions agree, the PCA succeeds, and the picture
    separates ENCODERS rather than meaning. Qdrant records a dimension and no
    model name, so the only place the difference is written down is the state
    DB's per-project index identity.
    """
    collections = dict([
        _collection("proj_alpha", "alpha"),
        _collection("proj_beta", "beta"),
        _collection("proj_gamma", "gamma"),
    ])
    settings = _settings_with_state(_state_db(tmp_path, recorded={
        "proj_alpha": ("all-MiniLM-L6-v2", 8),
        "proj_beta": ("all-MiniLM-L6-v2", 8),
        "proj_gamma": ("some-other-encoder", 8),
    }))

    result = compute_map_points(FakeClient(collections), settings, sorted(collections))

    assert {p["project_id"] for p in result["points"]} == {"alpha", "beta"}, \
        "a differently-encoded collection was mixed into the projection"
    reasons = _reasons(result)
    assert "proj_gamma" in reasons, f"excluded silently: {result['excluded']}"
    assert "model" in reasons["proj_gamma"].lower(), reasons["proj_gamma"]


def test_a_differently_normalized_collection_is_excluded(settings):
    """Normalization is not recorded anywhere, so it is MEASURED.

    Cosine geometry over unnormalized vectors is a different space; mixing the
    two lets magnitude masquerade as meaning and the PCA reads it as structure.
    """
    collections = dict([
        _collection("proj_alpha", "alpha", norm=3.0),
        _collection("proj_beta", "beta", norm=3.0),
        _collection("proj_gamma", "gamma", norm=1.0),
    ])
    result = compute_map_points(FakeClient(collections), settings, sorted(collections))

    assert {p["project_id"] for p in result["points"]} == {"alpha", "beta"}
    reasons = _reasons(result)
    assert "proj_gamma" in reasons, f"excluded silently: {result['excluded']}"
    assert "normal" in reasons["proj_gamma"].lower(), reasons["proj_gamma"]


@pytest.mark.parametrize("field, odd_params, expected", [
    ("distance", _params(distance="Euclid"), "distance metric"),
    ("datatype", _params(datatype="uint8"), "vector type"),
])
def test_a_declared_space_difference_is_excluded(settings, field, odd_params, expected):
    """Distance metric and vector type come from the collection config.

    They never touch the payload and never change a vector's length, so nothing
    downstream would ever notice — the projection would simply be wrong.
    """
    collections = dict([
        _collection("proj_alpha", "alpha"),
        _collection("proj_beta", "beta"),
        _collection("proj_gamma", "gamma"),
    ])
    configs = {"proj_alpha": _params(), "proj_beta": _params(),
               "proj_gamma": odd_params}
    result = compute_map_points(FakeClient(collections, configs=configs),
                                settings, sorted(collections))

    assert {p["project_id"] for p in result["points"]} == {"alpha", "beta"}
    reasons = _reasons(result)
    assert "proj_gamma" in reasons, f"excluded silently ({field}): {result['excluded']}"
    assert expected in reasons["proj_gamma"].lower(), reasons["proj_gamma"]


def test_an_unavailable_collection_is_counted_as_failed_not_incompatible(settings):
    """"Could not read it" and "will not draw it" are different answers.

    Both end up in ``excluded``, so without a kind they are one undifferentiated
    pile and an operator cannot tell a Qdrant fault from a deliberate refusal.
    """
    collections = dict([
        _collection("proj_alpha", "alpha"),
        _collection("proj_beta", "beta"),
    ])
    result = compute_map_points(FakeClient(collections, broken={"proj_beta"}),
                                settings, sorted(collections))

    assert {p["project_id"] for p in result["points"]} == {"alpha"}
    entry = next(e for e in result["excluded"] if e["collection"] == "proj_beta")
    assert entry.get("kind") == "failed", entry
    assert "unreadable" in entry["reason"]
    cov = result["coverage"]
    assert cov.get("collections_failed") == 1
    assert cov.get("collections_incompatible") == 0
    assert cov.get("collections_excluded") == 1


def test_a_project_filtered_request_survives_an_incompatible_neighbour(settings):
    """A scoped request operates on the project the caller asked for.

    A project's routed set is its own collection PLUS the framework corpora it
    links, so "scoped" does not mean "one collection" — and one incompatible
    corpus took the request down with the identical 500. The project's own
    content must still be drawn.
    """
    collections = dict([
        _collection("proj_beta", "beta"),
        _collection("fw_odoo_9", "odoo-18", dim=16),
    ])
    # Exactly what CollectionRouter.read_collections("beta") returns: own first.
    result = compute_map_points(FakeClient(collections), settings,
                                ["proj_beta", "fw_odoo_9"])

    assert {p["project_id"] for p in result["points"]} == {"beta"}, \
        "the selected project was collateral damage of its framework corpus"
    reasons = _reasons(result)
    assert "fw_odoo_9" in reasons, f"excluded silently: {result['excluded']}"
    assert "dimension" in reasons["fw_odoo_9"].lower(), reasons["fw_odoo_9"]


def test_the_cache_identity_changes_when_the_recorded_model_changes(tmp_path):
    """Re-embedding leaves every file hash untouched.

    The cached map was keyed on file hashes alone, so switching model rewrote
    every vector, moved every point, and the cache went on serving the previous
    model's projection as if it were current.
    """
    from ragtools.service.map_data import get_cache_version_hash

    before = get_cache_version_hash(
        _state_db(tmp_path / "a", recorded={"proj_alpha": ("all-MiniLM-L6-v2", 8)}))
    after = get_cache_version_hash(
        _state_db(tmp_path / "b", recorded={"proj_alpha": ("some-other-encoder", 8)}))

    assert before and after
    assert before != after, "a model change left the map cache looking current"


def _weird_shapes():
    """Every input shape that reached ``np.array`` and turned into a 500."""
    ok = dict([_collection("proj_alpha", "alpha")])

    mixed_dim = dict(ok)
    mixed_dim.update([_collection("proj_gamma", "gamma", dim=16)])

    named = dict(ok)
    named.update([_collection("proj_gamma", "gamma", vector_name="text")])

    two_names = dict(ok)
    _, pts = _collection("proj_gamma", "gamma")
    for p in pts:
        p.vector = {"text": p.vector, "code": p.vector}
    two_names["proj_gamma"] = pts

    multivector = dict(ok)
    _, pts = _collection("proj_gamma", "gamma")
    for p in pts:
        p.vector = [p.vector, p.vector]
    multivector["proj_gamma"] = pts

    no_vectors = dict(ok)
    _, pts = _collection("proj_gamma", "gamma")
    for p in pts:
        p.vector = None
    no_vectors["proj_gamma"] = pts

    ragged_inside = dict(ok)
    _, pts = _collection("proj_gamma", "gamma")
    for i, p in enumerate(pts):
        p.vector = p.vector[: 4 if i % 2 else 8]
    ragged_inside["proj_gamma"] = pts

    empty = dict(ok)
    empty["proj_gamma"] = []

    return {
        "mixed_dimension": (mixed_dim, set()),
        "named_vectors": (named, set()),
        "two_named_vectors": (two_names, set()),
        "multivector": (multivector, set()),
        "no_vectors": (no_vectors, set()),
        "ragged_within_one_collection": (ragged_inside, set()),
        "empty_collection": (empty, set()),
        "every_collection_broken": (ok, {"proj_alpha"}),
    }


@pytest.mark.parametrize("shape", sorted(_weird_shapes()))
def test_no_input_shape_produces_an_error(settings, shape):
    """The acceptance criterion, stated as a test.

    Every one of these is a legitimate thing a Qdrant collection can hold. The
    map's job is to draw what it can and SAY what it could not; raising is how
    one collection's oddity became every project's outage.
    """
    collections, broken = _weird_shapes()[shape]
    result = compute_map_points(FakeClient(collections, broken=broken),
                                settings, sorted(collections))

    assert isinstance(result, dict)
    assert isinstance(result["points"], list)
    assert isinstance(result["excluded"], list)
    cov = result["coverage"]
    for counter in ("collections_excluded", "collections_incompatible",
                    "collections_failed"):
        assert isinstance(cov.get(counter), int), f"{counter} missing from coverage"
    # Whatever was not drawn is named. Silence is the failure mode this replaces.
    reported = {e["collection"] for e in result["excluded"]}
    represented = {p["project_id"] for p in result["points"]}
    assert reported or represented, f"{shape}: drew nothing and explained nothing"
    assert reported <= set(collections)


def test_an_unknown_project_is_a_404_not_a_500():
    """``/api/map/points?project=<typo>`` is the last input shape.

    ``CollectionRouter`` raises ``UnknownProject`` deliberately — falling back
    to the shared collection would answer one project's request out of
    another's vectors — but nothing registered it, so a designed refusal
    reached the blanket handler and came back as an anonymous 500.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ragtools.collection_router import UnknownProject
    from ragtools.service.errors import install_domain_handlers

    app = FastAPI()
    install_domain_handlers(app)

    @app.get("/api/map/points")
    def _points():
        raise UnknownProject("no registered project 'typo'")

    response = TestClient(app, raise_server_exceptions=False).get("/api/map/points")

    assert response.status_code == 404, response.status_code
    body = response.json()
    assert body["error"] == "UNKNOWN_PROJECT"
    assert "typo" in body["message"], body["message"]
