"""Retrieval quality evaluation harness.

Loads benchmark questions, runs retrieval for each, and computes metrics.

Usage:
    python scripts/eval_retrieval.py [--questions PATH] [--content-root PATH] [--top-k N] [--json]

The script expects an indexed knowledge base. Run `rag index <path>` first.
"""

import json
import sys
from pathlib import Path
from typing import Any

# Add src to path for direct script execution
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.models import SearchResult
from ragtools.retrieval.searcher import Searcher


# --- Data Structures ---


def load_questions(path: str) -> list[dict]:
    """Load benchmark questions from a JSON file.

    Each entry must have: query, project, expected_file, expected_section.
    Optional: notes.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for i, q in enumerate(data):
        assert "query" in q, f"Question {i} missing 'query'"
        assert "expected_file" in q, f"Question {i} missing 'expected_file'"
    return data


# --- Evaluation Logic ---


def evaluate_single(
    searcher: Searcher,
    question: dict,
    top_k: int = 10,
) -> dict:
    """Evaluate a single question against the retrieval pipeline.

    Returns a result dict with match info and scores.
    """
    results = searcher.search(
        query=question["query"],
        project_id=question.get("project"),
        top_k=top_k,
    )

    expected_file = question["expected_file"]
    expected_section = question.get("expected_section", "")

    # Check file match at various k
    file_ranks = []
    section_ranks = []

    for i, r in enumerate(results):
        if expected_file in r.file_path:
            file_ranks.append(i + 1)  # 1-indexed rank
        if expected_section and any(expected_section.lower() in h.lower() for h in r.headings):
            section_ranks.append(i + 1)

    file_hit_at_1 = bool(file_ranks and file_ranks[0] == 1)
    file_hit_at_5 = bool(file_ranks and file_ranks[0] <= 5)
    file_hit_at_10 = bool(file_ranks and file_ranks[0] <= 10)
    section_hit_at_5 = bool(section_ranks and section_ranks[0] <= 5)
    section_hit_at_10 = bool(section_ranks and section_ranks[0] <= 10)

    # MRR: reciprocal rank of first correct file
    file_mrr = 1.0 / file_ranks[0] if file_ranks else 0.0
    section_mrr = 1.0 / section_ranks[0] if section_ranks else 0.0

    # Top result info
    top_score = results[0].score if results else 0.0
    top_confidence = results[0].confidence if results else "NONE"

    # Failure categories
    failure = None
    if not results:
        failure = "NO_RESULTS"
    elif not file_ranks:
        failure = "WRONG_FILE"
    elif not section_ranks and expected_section:
        failure = "WRONG_SECTION"
    elif top_score < 0.5:
        failure = "LOW_CONFIDENCE"

    return {
        "query": question["query"],
        "project": question.get("project", ""),
        "expected_file": expected_file,
        "expected_section": expected_section,
        "num_results": len(results),
        "top_score": round(top_score, 4),
        "top_confidence": top_confidence,
        "file_hit_at_1": file_hit_at_1,
        "file_hit_at_5": file_hit_at_5,
        "file_hit_at_10": file_hit_at_10,
        "section_hit_at_5": section_hit_at_5,
        "section_hit_at_10": section_hit_at_10,
        "file_mrr": round(file_mrr, 4),
        "section_mrr": round(section_mrr, 4),
        "failure": failure,
    }


def compute_aggregate_metrics(eval_results: list[dict]) -> dict:
    """Compute aggregate metrics across all evaluation results."""
    n = len(eval_results)
    if n == 0:
        return {}

    return {
        "total_questions": n,
        "file_recall_at_1": sum(r["file_hit_at_1"] for r in eval_results) / n,
        "file_recall_at_5": sum(r["file_hit_at_5"] for r in eval_results) / n,
        "file_recall_at_10": sum(r["file_hit_at_10"] for r in eval_results) / n,
        "section_recall_at_5": sum(r["section_hit_at_5"] for r in eval_results) / n,
        "section_recall_at_10": sum(r["section_hit_at_10"] for r in eval_results) / n,
        "mean_file_mrr": sum(r["file_mrr"] for r in eval_results) / n,
        "mean_section_mrr": sum(r["section_mrr"] for r in eval_results) / n,
        "avg_top_score": sum(r["top_score"] for r in eval_results) / n,
        "failures": {
            "no_results": sum(1 for r in eval_results if r["failure"] == "NO_RESULTS"),
            "wrong_file": sum(1 for r in eval_results if r["failure"] == "WRONG_FILE"),
            "wrong_section": sum(1 for r in eval_results if r["failure"] == "WRONG_SECTION"),
            "low_confidence": sum(1 for r in eval_results if r["failure"] == "LOW_CONFIDENCE"),
            "total_failures": sum(1 for r in eval_results if r["failure"] is not None),
        },
    }


# --- Output Formatting ---


def print_results(eval_results: list[dict], metrics: dict) -> None:
    """Print evaluation results to console."""
    print("=" * 70)
    print("RETRIEVAL EVALUATION RESULTS")
    print("=" * 70)

    # Per-question results
    for i, r in enumerate(eval_results, 1):
        status = "PASS" if r["file_hit_at_5"] else "FAIL"
        marker = "[OK]" if status == "PASS" else "[!!]"

        print(f"\n{marker} Q{i}: {r['query']}")
        print(f"     Project: {r['project']} | Expected: {r['expected_file']}")
        print(f"     Top score: {r['top_score']} ({r['top_confidence']})")
        print(f"     File@5: {'yes' if r['file_hit_at_5'] else 'NO'} | "
              f"Section@5: {'yes' if r['section_hit_at_5'] else 'NO'} | "
              f"MRR: {r['file_mrr']}")
        if r["failure"]:
            print(f"     Failure: {r['failure']}")

    # Aggregate metrics
    print("\n" + "=" * 70)
    print("AGGREGATE METRICS")
    print("=" * 70)
    print(f"  Questions:          {metrics['total_questions']}")
    print(f"  File Recall@1:      {metrics['file_recall_at_1']:.1%}")
    print(f"  File Recall@5:      {metrics['file_recall_at_5']:.1%}")
    print(f"  File Recall@10:     {metrics['file_recall_at_10']:.1%}")
    print(f"  Section Recall@5:   {metrics['section_recall_at_5']:.1%}")
    print(f"  Section Recall@10:  {metrics['section_recall_at_10']:.1%}")
    print(f"  Mean File MRR:      {metrics['mean_file_mrr']:.3f}")
    print(f"  Mean Section MRR:   {metrics['mean_section_mrr']:.3f}")
    print(f"  Avg Top Score:      {metrics['avg_top_score']:.3f}")

    f = metrics["failures"]
    print(f"\n  Failures:           {f['total_failures']}/{metrics['total_questions']}")
    if f["no_results"]:
        print(f"    - No results:     {f['no_results']}")
    if f["wrong_file"]:
        print(f"    - Wrong file:     {f['wrong_file']}")
    if f["wrong_section"]:
        print(f"    - Wrong section:  {f['wrong_section']}")
    if f["low_confidence"]:
        print(f"    - Low confidence: {f['low_confidence']}")

    # Verdict
    print("\n" + "=" * 70)
    r5 = metrics["file_recall_at_5"]
    if r5 >= 0.9:
        print("VERDICT: GOOD — File recall@5 >= 90%")
    elif r5 >= 0.7:
        print("VERDICT: ACCEPTABLE — File recall@5 >= 70%, consider tuning")
    else:
        print("VERDICT: NEEDS WORK — File recall@5 < 70%, tuning required")
    print("=" * 70)


# --- Main ---


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Evaluate retrieval quality")
    parser.add_argument(
        "--questions",
        default="tests/fixtures/eval_questions.json",
        help="Path to benchmark questions JSON",
    )
    parser.add_argument(
        "--content-root",
        default=None,
        help="Override content root (default: from config)",
    )
    parser.add_argument("--top-k", type=int, default=10, help="Top-k for evaluation")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    # Load questions
    questions = load_questions(args.questions)
    print(f"Loaded {len(questions)} benchmark questions from {args.questions}\n")

    # Initialize retrieval pipeline
    settings = Settings()
    if args.content_root:
        settings = Settings(content_root=args.content_root)

    client = settings.get_qdrant_client()
    encoder = Encoder(settings.embedding_model)
    searcher = Searcher(client=client, encoder=encoder, settings=settings)

    # Run evaluation
    eval_results = []
    for q in questions:
        result = evaluate_single(searcher, q, top_k=args.top_k)
        eval_results.append(result)

    metrics = compute_aggregate_metrics(eval_results)

    # Output
    if args.json:
        output = {"results": eval_results, "metrics": metrics}
        print(json.dumps(output, indent=2))
    else:
        print_results(eval_results, metrics)

    # Exit code: 0 if recall@5 >= 70%, 1 otherwise
    return 0 if metrics.get("file_recall_at_5", 0) >= 0.7 else 1


if __name__ == "__main__":
    sys.exit(main())
