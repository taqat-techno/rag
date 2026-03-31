"""Tests for retrieval evaluation logic."""

import json
from pathlib import Path

import pytest

# Import evaluation functions directly
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from eval_retrieval import (
    compute_aggregate_metrics,
    evaluate_single,
    load_questions,
)

from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.indexing.indexer import ensure_collection, index_file
from ragtools.indexing.scanner import scan_project, get_relative_path
from ragtools.retrieval.searcher import Searcher

FIXTURES = Path(__file__).parent / "fixtures"


# --- load_questions tests ---


class TestLoadQuestions:
    def test_loads_fixture_file(self):
        questions = load_questions(str(FIXTURES / "eval_questions.json"))
        assert len(questions) == 10
        assert all("query" in q for q in questions)
        assert all("expected_file" in q for q in questions)

    def test_missing_query_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text('[{"expected_file": "x.md"}]')
        with pytest.raises(AssertionError, match="missing 'query'"):
            load_questions(str(bad))

    def test_missing_file_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text('[{"query": "test"}]')
        with pytest.raises(AssertionError, match="missing 'expected_file'"):
            load_questions(str(bad))


# --- compute_aggregate_metrics tests ---


class TestAggregateMetrics:
    def test_perfect_results(self):
        results = [
            {
                "file_hit_at_1": True, "file_hit_at_5": True, "file_hit_at_10": True,
                "section_hit_at_5": True, "section_hit_at_10": True,
                "file_mrr": 1.0, "section_mrr": 1.0,
                "top_score": 0.9, "failure": None,
            },
            {
                "file_hit_at_1": True, "file_hit_at_5": True, "file_hit_at_10": True,
                "section_hit_at_5": True, "section_hit_at_10": True,
                "file_mrr": 1.0, "section_mrr": 1.0,
                "top_score": 0.85, "failure": None,
            },
        ]
        m = compute_aggregate_metrics(results)
        assert m["file_recall_at_1"] == 1.0
        assert m["file_recall_at_5"] == 1.0
        assert m["mean_file_mrr"] == 1.0
        assert m["failures"]["total_failures"] == 0

    def test_mixed_results(self):
        results = [
            {
                "file_hit_at_1": True, "file_hit_at_5": True, "file_hit_at_10": True,
                "section_hit_at_5": True, "section_hit_at_10": True,
                "file_mrr": 1.0, "section_mrr": 1.0,
                "top_score": 0.8, "failure": None,
            },
            {
                "file_hit_at_1": False, "file_hit_at_5": False, "file_hit_at_10": False,
                "section_hit_at_5": False, "section_hit_at_10": False,
                "file_mrr": 0.0, "section_mrr": 0.0,
                "top_score": 0.3, "failure": "WRONG_FILE",
            },
        ]
        m = compute_aggregate_metrics(results)
        assert m["file_recall_at_5"] == 0.5
        assert m["mean_file_mrr"] == 0.5
        assert m["failures"]["wrong_file"] == 1
        assert m["failures"]["total_failures"] == 1

    def test_empty_results(self):
        assert compute_aggregate_metrics([]) == {}

    def test_all_failures(self):
        results = [
            {
                "file_hit_at_1": False, "file_hit_at_5": False, "file_hit_at_10": False,
                "section_hit_at_5": False, "section_hit_at_10": False,
                "file_mrr": 0.0, "section_mrr": 0.0,
                "top_score": 0.0, "failure": "NO_RESULTS",
            },
        ]
        m = compute_aggregate_metrics(results)
        assert m["file_recall_at_5"] == 0.0
        assert m["failures"]["no_results"] == 1


# --- evaluate_single integration tests ---


@pytest.fixture(scope="module")
def eval_searcher():
    """Create a Searcher with indexed test fixtures for evaluation."""
    settings = Settings(content_root=str(FIXTURES))
    client = Settings.get_memory_client()
    encoder = Encoder(settings.embedding_model)
    ensure_collection(client, settings.collection_name, encoder.dimension)

    for pid, file_path in scan_project(str(FIXTURES)):
        rel = get_relative_path(file_path, str(FIXTURES))
        index_file(
            client=client, encoder=encoder,
            collection_name=settings.collection_name,
            project_id=pid, file_path=file_path, relative_path=rel,
        )

    return Searcher(client=client, encoder=encoder, settings=settings)


class TestEvaluateSingle:
    def test_correct_file_found(self, eval_searcher):
        q = {
            "query": "What database is used?",
            "project": "project_a",
            "expected_file": "project_a/README.md",
            "expected_section": "Database",
        }
        result = evaluate_single(eval_searcher, q)
        assert result["file_hit_at_10"] is True
        assert result["file_mrr"] > 0

    def test_returns_all_fields(self, eval_searcher):
        q = {
            "query": "testing",
            "project": "project_a",
            "expected_file": "project_a/guide.md",
            "expected_section": "Testing",
        }
        result = evaluate_single(eval_searcher, q)
        assert "query" in result
        assert "top_score" in result
        assert "file_hit_at_5" in result
        assert "section_hit_at_5" in result
        assert "failure" in result
        assert "file_mrr" in result

    def test_gibberish_query(self, eval_searcher):
        q = {
            "query": "xyzzy flurbo garbanzoid nonsense",
            "project": "project_a",
            "expected_file": "project_a/README.md",
            "expected_section": "Backend",
        }
        result = evaluate_single(eval_searcher, q, top_k=5)
        # Should likely fail to find the right file
        assert result["top_score"] < 0.5 or result["failure"] is not None
