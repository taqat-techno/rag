"""Tests for retrieval pipeline — searcher and formatter."""

from pathlib import Path

import pytest

from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.indexing.indexer import ensure_collection, index_file
from ragtools.models import SearchResult
from ragtools.retrieval.searcher import Searcher, _score_to_confidence
from ragtools.retrieval.formatter import format_context, format_context_brief

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def indexed_env():
    """Create an in-memory Qdrant with indexed test fixtures.

    Shared across all tests in this module (expensive setup).
    Returns (client, encoder, settings).
    """
    settings = Settings(content_root=str(FIXTURES))
    client = Settings.get_memory_client()
    encoder = Encoder(settings.embedding_model)

    ensure_collection(client, settings.collection_name, encoder.dimension)

    # Index all fixture files
    from ragtools.indexing.scanner import scan_project, get_relative_path

    for pid, file_path in scan_project(str(FIXTURES)):
        rel = get_relative_path(file_path, str(FIXTURES))
        index_file(
            client=client,
            encoder=encoder,
            collection_name=settings.collection_name,
            project_id=pid,
            file_path=file_path,
            relative_path=rel,
        )

    return client, encoder, settings


@pytest.fixture
def searcher(indexed_env):
    client, encoder, settings = indexed_env
    return Searcher(client=client, encoder=encoder, settings=settings)


# --- Confidence Mapping ---


class TestScoreToConfidence:
    def test_high(self):
        assert _score_to_confidence(0.85) == "HIGH"
        assert _score_to_confidence(0.70) == "HIGH"

    def test_moderate(self):
        assert _score_to_confidence(0.60) == "MODERATE"
        assert _score_to_confidence(0.50) == "MODERATE"

    def test_low(self):
        assert _score_to_confidence(0.49) == "LOW"
        assert _score_to_confidence(0.10) == "LOW"
        assert _score_to_confidence(0.0) == "LOW"


# --- Searcher Tests ---


class TestSearcher:
    def test_basic_search(self, searcher):
        results = searcher.search("backend architecture")
        assert len(results) > 0
        assert all(isinstance(r, SearchResult) for r in results)

    def test_results_sorted_by_score(self, searcher):
        results = searcher.search("database PostgreSQL")
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_result_fields_populated(self, searcher):
        results = searcher.search("authentication JWT")
        assert len(results) > 0
        r = results[0]
        assert r.chunk_id  # non-empty
        assert r.score > 0
        assert r.text  # non-empty
        assert r.file_path  # non-empty
        assert r.project_id  # non-empty
        assert r.confidence in ("HIGH", "MODERATE", "LOW")

    def test_project_filter(self, searcher):
        results = searcher.search("documentation", project_id="project_a")
        assert len(results) > 0
        assert all(r.project_id == "project_a" for r in results)

    def test_project_filter_excludes_other(self, searcher):
        results = searcher.search("decision log", project_id="project_b")
        assert len(results) > 0
        assert all(r.project_id == "project_b" for r in results)

    def test_top_k_limits_results(self, searcher):
        results = searcher.search("project", top_k=2)
        assert len(results) <= 2

    def test_high_threshold_fewer_results(self, searcher):
        all_results = searcher.search("Python", score_threshold=0.0)
        strict_results = searcher.search("Python", score_threshold=0.8)
        assert len(strict_results) <= len(all_results)

    def test_gibberish_query_low_or_no_results(self, searcher):
        results = searcher.search("xyzzy flurbo garbanzoid", score_threshold=0.5)
        # Should return few or no results above 0.5
        assert len(results) <= 3

    def test_headings_in_results(self, searcher):
        results = searcher.search("backend FastAPI Python")
        # At least one result should have headings
        has_headings = [r for r in results if r.headings]
        assert len(has_headings) > 0


# --- Formatter Tests ---


class TestFormatContext:
    def test_basic_format(self, searcher):
        results = searcher.search("architecture")
        output = format_context(results, "architecture")
        assert "[RAG CONTEXT" in output
        assert "architecture" in output
        assert "Source:" in output
        assert "Score:" in output

    def test_empty_results(self):
        output = format_context([], "nonexistent topic")
        assert "[RAG NOTICE]" in output
        assert "No relevant local content found" in output
        assert "nonexistent topic" in output

    def test_confidence_label_in_output(self, searcher):
        results = searcher.search("PostgreSQL database schema")
        output = format_context(results, "PostgreSQL")
        assert "CONFIDENCE" in output  # HIGH, MODERATE, or LOW

    def test_source_attribution(self, searcher):
        results = searcher.search("deployment Docker")
        output = format_context(results, "deployment")
        assert "project_a/" in output or "project_b/" in output

    def test_heading_hierarchy_in_output(self, searcher):
        results = searcher.search("backend authentication")
        output = format_context(results, "backend authentication")
        # Should contain heading path like "Architecture > Backend"
        assert ">" in output or "N/A" in output

    def test_numbered_results(self, searcher):
        results = searcher.search("testing pytest")
        output = format_context(results, "testing")
        assert "[1]" in output


class TestFormatContextBrief:
    def test_basic_brief(self, searcher):
        results = searcher.search("architecture")
        output = format_context_brief(results, "architecture")
        assert "project_a/" in output or "project_b/" in output

    def test_empty_brief(self):
        output = format_context_brief([], "nothing")
        assert "No results" in output

    def test_truncates_long_text(self, searcher):
        results = searcher.search("getting started")
        output = format_context_brief(results, "getting started")
        # Should contain score in parentheses
        assert "(" in output
