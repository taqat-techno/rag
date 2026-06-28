"""Tests for feature-intent detection and priority reranking."""

from ragtools.models import SearchResult
from ragtools.retrieval.feature_intent import detect_dev_intent, matched_triggers
from ragtools.retrieval.rerank import (
    merge_and_rerank,
    priority_bonus,
    rerank,
)


class TestFeatureIntent:
    def test_detects_implement(self):
        assert detect_dev_intent("please implement a caching layer")

    def test_detects_phrases(self):
        for q in [
            "add feature for exports",
            "create endpoint for users",
            "modify workflow to retry",
            "extend module with logging",
            "add API for billing",
            "enhance system performance",
            "architecture review of the indexer",
            "refactor the searcher",
            "bug fix for the watcher",
            "API modification needed",
        ]:
            assert detect_dev_intent(q), q

    def test_non_dev_request(self):
        assert not detect_dev_intent("what is the capital of France?")
        assert not detect_dev_intent("summarize the meeting notes")

    def test_empty(self):
        assert not detect_dev_intent("")
        assert matched_triggers("") == []

    def test_returns_triggers(self):
        hits = matched_triggers("implement and refactor the endpoint")
        assert "implement" in hits
        assert "refactor" in hits


def _r(score, chunk_type, file_path="x", headings=None, symbols=None):
    return SearchResult(
        chunk_id=f"{file_path}-{score}-{chunk_type}",
        score=score,
        text="t",
        raw_text="t",
        file_path=file_path,
        project_id="p",
        headings=headings or [],
        confidence="HIGH",
        chunk_type=chunk_type,
        symbols=symbols or [],
    )


class TestRerank:
    def test_code_boosted_over_doc_at_equal_score(self):
        doc = _r(0.60, "documentation", "guide.md")
        code = _r(0.60, "code", "service.py")
        out = rerank([doc, code])
        assert out[0].chunk_type == "code"

    def test_strong_doc_still_beats_weak_code(self):
        strong_doc = _r(0.90, "documentation", "guide.md")
        weak_code = _r(0.40, "code", "x.py")
        out = rerank([strong_doc, weak_code])
        assert out[0] is strong_doc

    def test_api_bonus(self):
        api = _r(0.5, "code", "api/routes.py", symbols=["router"])
        plain = _r(0.5, "code", "util.py")
        assert priority_bonus(api) > priority_bonus(plain)

    def test_architecture_doc_bonus(self):
        arch = _r(0.5, "documentation", "docs/architecture/overview.md")
        plain = _r(0.5, "documentation", "notes.md")
        assert priority_bonus(arch) > priority_bonus(plain)

    def test_merge_dedups_by_chunk_id(self):
        a = _r(0.5, "code", "x.py")
        dup = _r(0.7, "code", "x.py")  # same chunk_id pattern? no — score differs
        # force same id
        dup.chunk_id = a.chunk_id
        out = merge_and_rerank([a], [dup])
        assert len(out) == 1
        assert out[0].score == 0.7  # kept the higher-scoring duplicate

    def test_rerank_does_not_mutate_input(self):
        items = [_r(0.6, "documentation"), _r(0.6, "code")]
        original = list(items)
        rerank(items)
        assert items == original
