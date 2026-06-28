"""Retrieval-correctness regression tests (P1).

Two bugs from review:
  * the score threshold was applied per-layer BEFORE the rerank bonus, dropping
    borderline code the bonus was meant to surface;
  * results were ordered by adjusted score but the formatter displayed the raw
    score/confidence, hiding the rerank from the consuming agent.
"""

import numpy as np
import pytest

from ragtools.config import Settings
from ragtools.models import SearchResult
from ragtools.retrieval.dev_pipeline import dev_search
from ragtools.retrieval.formatter import format_dev_context
from ragtools.retrieval.searcher import Searcher


def _sr(score, chunk_type, file_path):
    return SearchResult(
        chunk_id=f"{file_path}-{score}-{chunk_type}",
        score=score, text="t", raw_text="t", file_path=file_path,
        project_id="p", headings=[], confidence="LOW", chunk_type=chunk_type,
    )


class _FakeSearcher:
    """Returns canned results per chunk_type; records the thresholds it saw."""

    def __init__(self, settings, by_type):
        self.settings = settings
        self._by_type = by_type
        self.thresholds_seen = []

    def search(self, query, project_id=None, project_ids=None, top_k=None,
               score_threshold=None, chunk_types=None):
        self.thresholds_seen.append(score_threshold)
        out = []
        for ct in (chunk_types or []):
            out.extend(self._by_type.get(ct, []))
        return out


def test_dev_search_disables_per_layer_threshold():
    fake = _FakeSearcher(Settings(score_threshold=0.3),
                         {"code": [_sr(0.9, "code", "a.py")]})
    dev_search(fake, "implement endpoint", project_id="p")
    assert fake.thresholds_seen, "searcher was never called"
    assert all(t == 0.0 for t in fake.thresholds_seen)


def test_dev_search_thresholds_after_rerank():
    fake = _FakeSearcher(
        Settings(score_threshold=0.3, top_k=10),
        {
            "code": [_sr(0.28, "code", "svc.py")],
            "documentation": [_sr(0.22, "documentation", "guide.md")],
        },
    )
    out = dev_search(fake, "implement endpoint", project_id="p")
    paths = [r.file_path for r in out.results]
    assert "svc.py" in paths           # 0.28 + 0.15 bonus = 0.43 >= 0.3 -> kept
    assert "guide.md" not in paths     # 0.22 + 0.0       = 0.22 <  0.3 -> dropped
    svc = next(r for r in out.results if r.file_path == "svc.py")
    assert svc.adjusted_score == pytest.approx(0.43)


def test_searchresult_adjusted_score_defaults_none():
    assert _sr(0.5, "code", "a.py").adjusted_score is None


def test_format_dev_context_exposes_reranked_score():
    r = _sr(0.28, "code", "svc.py")
    r.adjusted_score = 0.43
    out = format_dev_context([r], "implement token validation", ["implement"])
    assert "0.43" in out
    assert "reranked" in out.lower()


class _StubClient:
    def __init__(self):
        self.kwargs = None

    def query_points(self, **kw):
        self.kwargs = kw
        class _R:
            points = []
        return _R()


class _StubEncoder:
    def encode_query(self, q):
        return np.zeros(384, dtype="float32")


def test_searcher_passes_explicit_zero_threshold_through():
    stub = _StubClient()
    searcher = Searcher(client=stub, encoder=_StubEncoder(),
                        settings=Settings(score_threshold=0.3))
    searcher.search("q", project_id="p", score_threshold=0.0)
    assert stub.kwargs["score_threshold"] == 0.0   # not silently defaulted to 0.3


# --- Intent detector is load-bearing: it selects the ranking strategy (P2) ---

def _two_layer_searcher():
    return _FakeSearcher(
        Settings(score_threshold=0.3, top_k=10),
        {
            "code": [_sr(0.50, "code", "svc.py")],
            "documentation": [_sr(0.55, "documentation", "guide.md")],
        },
    )


def test_dev_query_uses_codebase_first_strategy():
    out = dev_search(_two_layer_searcher(), "implement an endpoint", project_id="p")
    assert out.is_dev_request is True
    assert out.strategy == "codebase-first"
    # code 0.50 + 0.15 bonus = 0.65 outranks doc 0.55
    assert out.results[0].file_path == "svc.py"


def test_non_dev_query_uses_flat_strategy():
    out = dev_search(_two_layer_searcher(), "what does the auth module store", project_id="p")
    assert out.is_dev_request is False
    assert out.strategy == "flat"
    # no codebase-first bonus -> raw order, doc 0.55 above code 0.50
    assert out.results[0].file_path == "guide.md"
    # flat must not apply the rerank bonus at all
    assert all(r.adjusted_score == r.score for r in out.results)


def test_format_dev_context_confidence_band_reflects_rerank():
    # A code chunk reranked from raw MODERATE (0.55) up to HIGH (0.72) should
    # display the HIGH band, not the stale raw-derived one.
    r = _sr(0.55, "code", "svc.py")
    r.adjusted_score = 0.72
    out = format_dev_context([r], "implement token validation", ["implement"])
    assert "HIGH" in out
    assert "0.55→0.72 reranked" in out
