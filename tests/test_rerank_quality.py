"""Ranking quality: source-class down-weight + per-file cap (P1-B / W8/W9).

Generic: an external dependency / generated chunk must not outrank project-owned
content of comparable relevance, and one file must not dominate a result set.
"""

from collections import Counter

from ragtools.models import SearchResult
from ragtools.retrieval.rerank import adjusted_score, cap_per_file, rerank


def _sr(score, chunk_type="code", file_path="a.py", source_class="owned"):
    return SearchResult(
        chunk_id=f"{file_path}:{score}:{source_class}",
        score=score, text="t", raw_text="t", file_path=file_path,
        project_id="p", confidence="LOW", chunk_type=chunk_type, source_class=source_class,
    )


# --- 5a: source-class down-weight -------------------------------------------

def test_dependency_downweighted_vs_owned_same_type():
    owned = _sr(0.50, "documentation", "own.md", "owned")
    dep = _sr(0.50, "documentation", "dep.md", "dependency")
    assert adjusted_score(owned) > adjusted_score(dep)


def test_generated_downweighted_vs_owned_same_type():
    owned = _sr(0.50, "code", "own.py", "owned")
    gen = _sr(0.50, "code", "gen.js", "generated")
    assert adjusted_score(owned) > adjusted_score(gen)


def test_owned_doc_outranks_slightly_higher_dependency_doc():
    owned = _sr(0.50, "documentation", "own.md", "owned")
    dep = _sr(0.57, "documentation", "dep.md", "dependency")  # higher raw, but vendor
    out = rerank([dep, owned])
    assert out[0].file_path == "own.md"


def test_owned_chunk_unchanged_baseline():
    # owned is the neutral baseline: its adjusted score is its category bonus only.
    owned_doc = _sr(0.50, "documentation", "x.md", "owned")
    assert adjusted_score(owned_doc) == 0.50  # DOC_BONUS = 0, owned penalty = 0


# --- 5b: per-file cap + diversity -------------------------------------------

def test_cap_per_file_limits_one_file():
    results = [_sr(0.9 - i * 0.01, "code", "big.py") for i in range(6)]
    results.append(_sr(0.5, "code", "other.py"))
    capped = cap_per_file(results, max_per_file=3)
    counts = Counter(r.file_path for r in capped)
    assert counts["big.py"] == 3
    assert counts["other.py"] == 1


def test_cap_per_file_preserves_rank_order():
    results = [
        _sr(0.9, "code", "a.py"), _sr(0.89, "code", "a.py"), _sr(0.88, "code", "a.py"),
        _sr(0.87, "code", "a.py"), _sr(0.40, "code", "b.py"),
    ]
    capped = cap_per_file(results, max_per_file=2)
    assert [r.file_path for r in capped] == ["a.py", "a.py", "b.py"]


# --- 5b integration: dev_search applies the cap -----------------------------

def test_identifier_match_bonus_fires_on_symbol():
    from ragtools.retrieval.rerank import identifier_match_bonus, query_tokens
    r = _sr(0.3, "code", "m.py")
    r.symbols = ["DisasterDisaster"]
    r.exports = ["disaster.disaster"]
    qt = query_tokens("find the disaster.disaster model definition and fields")
    assert identifier_match_bonus(r, qt) > 0


def test_identifier_match_bonus_no_false_positive():
    from ragtools.retrieval.rerank import identifier_match_bonus, query_tokens
    r = _sr(0.3, "code", "x.py")
    r.symbols = ["UnrelatedThing"]
    assert identifier_match_bonus(r, query_tokens("frobnicate the widget pipeline")) == 0.0


def test_dev_search_surfaces_exact_identifier_match():
    """A low-scoring chunk whose symbol matches the query is lifted over threshold."""
    from ragtools.config import Settings
    from ragtools.retrieval.dev_pipeline import dev_search

    hit = _sr(0.12, "code", "models/disaster.py")  # 0.12 + code 0.15 = 0.27 < 0.30
    hit.symbols = ["disaster.disaster"]

    class _FakeSearcher:
        def __init__(self, settings):
            self.settings = settings
        def search(self, query=None, project_id=None, project_ids=None, top_k=None,
                   score_threshold=None, chunk_types=None):
            return [hit] if "code" in (chunk_types or []) else []

    out = dev_search(_FakeSearcher(Settings(score_threshold=0.30, top_k=10)),
                     "implement a change to the disaster.disaster model", project_id="p")
    assert any(r.file_path == "models/disaster.py" for r in out.results)  # boosted over 0.30


def test_dev_search_caps_chunks_per_file():
    from ragtools.config import Settings
    from ragtools.retrieval.dev_pipeline import dev_search

    class _FakeSearcher:
        def __init__(self, settings, results):
            self.settings = settings
            self._results = results
        def search(self, query, project_id=None, project_ids=None, top_k=None,
                   score_threshold=None, chunk_types=None):
            if "code" in (chunk_types or []):
                return list(self._results)
            return []

    many = [_sr(0.9 - i * 0.01, "code", "dominant.py") for i in range(8)]
    many.append(_sr(0.6, "code", "other.py"))
    out = dev_search(_FakeSearcher(Settings(score_threshold=0.3, top_k=10), many),
                     "implement the handler", project_id="p")
    counts = Counter(r.file_path for r in out.results)
    assert counts["dominant.py"] <= 3        # capped
    assert "other.py" in counts              # diversity preserved
