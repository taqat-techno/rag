"""A5/S6 — enforce model compatibility when opening an existing collection.

§11.4: "model metadata enforced on every open" with a model-mismatch REFUSAL.
Qdrant records a collection's vector dimension, so a collection built under a
different model (different dimension) is caught at open time rather than
returning silently-meaningless cosine scores. This wires the A5 core
(:class:`~ragtools.embedding.backend.ModelMismatchError`) into `ensure_collection`
against a REAL (in-memory) Qdrant collection.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (A5; S6 §11.4)
"""

import pytest
from qdrant_client.models import Distance, VectorParams

from ragtools.config import Settings
from ragtools.embedding.backend import ModelMismatchError
from ragtools.indexing.indexer import (
    assert_collection_model_compatible,
    ensure_collection,
)


def _client():
    return Settings.get_memory_client()


def test_ensure_is_a_noop_when_dimension_matches():
    c = _client()
    ensure_collection(c, "kb", 384)
    ensure_collection(c, "kb", 384)  # exists, same dim -> fine
    assert "kb" in [col.name for col in c.get_collections().collections]


def test_ensure_refuses_a_dimension_mismatch():
    c = _client()
    c.create_collection("kb", vectors_config=VectorParams(size=384, distance=Distance.COSINE))
    with pytest.raises(ModelMismatchError):
        ensure_collection(c, "kb", 768)  # wrong-model dimension


def test_guard_names_both_dimensions():
    c = _client()
    c.create_collection("kb", vectors_config=VectorParams(size=384, distance=Distance.COSINE))
    with pytest.raises(ModelMismatchError) as ei:
        assert_collection_model_compatible(c, "kb", 768)
    msg = str(ei.value)
    assert "384" in msg and "768" in msg


def test_guard_is_best_effort_on_missing_collection():
    # A collection that doesn't exist can't be a mismatch — never a false refusal.
    assert_collection_model_compatible(_client(), "nope", 384)  # no raise
