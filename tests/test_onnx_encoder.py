"""Phase 5 (W5-A) — the ONNX embedding backend.

Structure is asserted unconditionally; the live-model tests are gated on
`fastembed` being installed, the same way the managed-Qdrant e2e is gated.

Plan: docs/planning/RAG_MODERNIZATION_MASTER_PLAN.md  (Phase 5 -> G5, D6/D7)
"""

import importlib.util

import pytest

from ragtools.embedding.backend import RUNTIME_ONNX, RUNTIME_TORCH, ModelMetadata

_HAS_FASTEMBED = importlib.util.find_spec("fastembed") is not None


def test_module_imports_without_fastembed_installed():
    """Importing the module must not require the optional dependency — only
    CONSTRUCTING the encoder does."""
    from ragtools.embedding import onnx_encoder
    assert onnx_encoder.DEFAULT_ONNX_MODEL.endswith("all-MiniLM-L6-v2")


@pytest.mark.skipif(_HAS_FASTEMBED, reason="fastembed IS installed here")
def test_missing_dependency_gives_an_actionable_error():
    from ragtools.embedding.onnx_encoder import OnnxEncoder
    with pytest.raises(ImportError) as ei:
        OnnxEncoder()
    msg = str(ei.value)
    assert "fastembed" in msg
    assert "PyTorch" in msg          # tell the user what it avoids


def test_onnx_metadata_declares_its_runtime():
    """The guard that prevents a torch-built collection being searched with ONNX
    vectors (measured min cosine between runtimes: 0.9087)."""
    meta = ModelMetadata(model_name="all-MiniLM-L6-v2", dimension=384,
                         normalize=True, backend="fastembed", runtime=RUNTIME_ONNX)
    assert meta.runtime == RUNTIME_ONNX
    assert meta.runtime != RUNTIME_TORCH


@pytest.mark.skipif(not _HAS_FASTEMBED, reason="fastembed not installed")
def test_live_onnx_encoder_satisfies_the_protocol():
    from ragtools.embedding.backend import EmbeddingBackend
    from ragtools.embedding.onnx_encoder import OnnxEncoder

    enc = OnnxEncoder()
    assert isinstance(enc, EmbeddingBackend)
    assert enc.dimension == 384
    assert enc.metadata().runtime == RUNTIME_ONNX

    batch = enc.encode_batch(["alpha", "beta"])
    assert batch.shape == (2, 384)
    q = enc.encode_query("alpha")
    assert q.shape == (384,)
    # cached second call returns the same object contents
    assert (enc.encode_query("alpha") == q).all()


@pytest.mark.skipif(not _HAS_FASTEMBED, reason="fastembed not installed")
def test_live_onnx_output_is_normalized():
    import numpy as np
    from ragtools.embedding.onnx_encoder import OnnxEncoder

    v = OnnxEncoder().encode_batch(["normalization check"])
    assert abs(float(np.linalg.norm(v[0])) - 1.0) < 1e-3


@pytest.mark.skipif(not _HAS_FASTEMBED, reason="fastembed not installed")
def test_empty_batch_is_safe():
    from ragtools.embedding.onnx_encoder import OnnxEncoder
    assert OnnxEncoder().encode_batch([]).shape == (0, 384)
