"""S10 — rank-based fusion (RRF) for cross-collection retrieval.

Scores from different collections are not comparable (different corpora/models),
so cross-collection results are merged by RECIPROCAL RANK, which is invariant to
score scale. This pins the pure fusion core the retrieval router uses.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S10 -> G10)
"""

from ragtools.retrieval.fusion import reciprocal_rank_fusion, rrf_scores


def _id(x):
    return x


def test_item_ranked_high_in_both_lists_wins():
    a = ["x", "y", "z"]
    b = ["x", "w", "y"]
    fused = reciprocal_rank_fusion([a, b], key=_id)
    assert fused[0] == "x"


def test_fusion_includes_every_unique_item():
    fused = reciprocal_rank_fusion([["a", "b"], ["b", "c"]], key=_id)
    assert set(fused) == {"a", "b", "c"}


def test_fusion_is_scale_invariant():
    # RRF uses ranks, not raw scores. Represent two collections purely by order;
    # an item consistently ranked first must win regardless of any score scale.
    dense = ["hit", "n1", "n2", "n3"]
    sparse = ["hit", "m1", "m2"]
    fused = reciprocal_rank_fusion([dense, sparse], key=_id)
    assert fused[0] == "hit"


def test_fusion_is_deterministic():
    f1 = reciprocal_rank_fusion([["a", "b"], ["b", "a"]], key=_id)
    f2 = reciprocal_rank_fusion([["a", "b"], ["b", "a"]], key=_id)
    assert f1 == f2


def test_ties_break_by_first_appearance():
    # a and b get identical RRF scores (each rank-0 in one list, absent in the
    # other); the earlier-seen item stays first (stable, deterministic).
    fused = reciprocal_rank_fusion([["a"], ["b"]], key=_id)
    assert fused == ["a", "b"]


def test_empty_inputs():
    assert reciprocal_rank_fusion([], key=_id) == []
    assert reciprocal_rank_fusion([[], []], key=_id) == []


def test_scores_are_exposed_and_additive():
    scores = rrf_scores([["x", "y"], ["x"]], key=_id, k=60)
    # x appears rank0 in both lists -> 1/61 + 1/61; y rank1 in one -> 1/62.
    assert scores["x"] > scores["y"]
    assert abs(scores["x"] - (2 / 61)) < 1e-9


def test_dedup_across_collections_by_key():
    # The same logical item from two collections fuses into one entry.
    fused = reciprocal_rank_fusion(
        [[{"id": "f", "col": "a"}], [{"id": "f", "col": "b"}]],
        key=lambda d: d["id"],
    )
    assert len(fused) == 1
    assert fused[0]["id"] == "f"
