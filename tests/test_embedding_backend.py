"""S9 / A5 — embedding backend abstraction + model-metadata enforcement.

A collection's vectors are only meaningful under the exact model that produced
them: change the model name, the dimension, or whether outputs were normalized,
and cosine scores become nonsense. §11.4 / A5 require model metadata to be
"enforced on every open" with a model-mismatch REFUSAL — this pins that pure
core (the metadata record, its round-trip for payload storage, and the
compatibility gate) plus the structural backend contract the encoder satisfies.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S9 -> G9; A5)
"""

import numpy as np
import pytest

from ragtools.embedding.backend import (
    EmbeddingBackend,
    ModelMetadata,
    ModelMismatchError,
    assert_model_compatible,
)


def _meta(**over) -> ModelMetadata:
    base = dict(model_name="all-MiniLM-L6-v2", dimension=384, normalize=True,
                backend="sentence-transformers")
    base.update(over)
    return ModelMetadata(**base)


# --- metadata record + round-trip ---------------------------------------


def test_metadata_roundtrips_through_dict():
    m = _meta()
    assert ModelMetadata.from_dict(m.to_dict()) == m


def test_from_dict_ignores_unknown_keys():
    d = _meta().to_dict()
    d["extra"] = "ignored"
    assert ModelMetadata.from_dict(d) == _meta()


# --- the compatibility gate (A5 refusal) --------------------------------


def test_identical_metadata_is_compatible():
    assert_model_compatible(_meta(), _meta())  # no raise


@pytest.mark.parametrize("field,bad", [
    ("model_name", "bge-small-en"),
    ("dimension", 768),
    ("normalize", False),
])
def test_incompatible_metadata_is_refused(field, bad):
    with pytest.raises(ModelMismatchError):
        assert_model_compatible(_meta(), _meta(**{field: bad}))


def test_refusal_names_both_models():
    with pytest.raises(ModelMismatchError) as ei:
        assert_model_compatible(_meta(model_name="stored-model"),
                                _meta(model_name="current-model"))
    msg = str(ei.value)
    assert "stored-model" in msg and "current-model" in msg


def test_compat_accepts_dict_on_either_side():
    # Metadata comes off a Qdrant payload as a dict; the gate coerces it.
    assert_model_compatible(_meta().to_dict(), _meta())
    with pytest.raises(ModelMismatchError):
        assert_model_compatible(_meta().to_dict(), _meta(dimension=1024))


# --- structural backend contract ----------------------------------------


def test_duck_typed_backend_satisfies_protocol():
    class FakeBackend:
        dimension = 3

        def encode_batch(self, texts, batch_size=64):
            return np.zeros((len(texts), 3), dtype=np.float32)

        def encode_query(self, query):
            return np.zeros(3, dtype=np.float32)

        def metadata(self):
            return _meta(dimension=3)

    fb = FakeBackend()
    assert isinstance(fb, EmbeddingBackend)  # runtime_checkable protocol
    assert fb.metadata().dimension == 3
    assert fb.encode_batch(["a", "b"]).shape == (2, 3)


# --- W5-A: the runtime is part of vector identity -----------------------


def test_runtime_defaults_to_torch_and_roundtrips():
    from ragtools.embedding.backend import RUNTIME_ONNX, RUNTIME_TORCH
    assert _meta().runtime == RUNTIME_TORCH
    m = _meta(runtime=RUNTIME_ONNX)
    assert ModelMetadata.from_dict(m.to_dict()).runtime == RUNTIME_ONNX


def test_a_different_runtime_is_refused():
    """Measured (W5-A): torch vs ONNX on the SAME model gave mean cosine 0.9939
    and min 0.9087 — close, but not the same vectors. A collection built under
    one runtime must not be searched under the other."""
    from ragtools.embedding.backend import RUNTIME_ONNX
    with pytest.raises(ModelMismatchError):
        assert_model_compatible(_meta(), _meta(runtime=RUNTIME_ONNX))


def test_legacy_metadata_without_runtime_is_treated_as_torch():
    """Collections written before runtimes were tracked must keep working."""
    from ragtools.embedding.backend import RUNTIME_TORCH
    legacy = {"model_name": "all-MiniLM-L6-v2", "dimension": 384, "normalize": True}
    assert ModelMetadata.from_dict(legacy).runtime == RUNTIME_TORCH
    assert_model_compatible(legacy, _meta())      # no raise
