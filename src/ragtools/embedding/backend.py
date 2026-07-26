"""Embedding backend abstraction + model-metadata enforcement (RAG v3, S9 / A5).

An embedded vector is only interpretable under the exact model that made it. So
a collection records the model that produced it, and every open re-checks that
the *current* backend matches — a mismatch is REFUSED, not silently searched
(§11.4, A5). This module holds the pure pieces:

* :class:`ModelMetadata` — the record stored on a collection and compared on open.
* :func:`assert_model_compatible` — the refusal gate (model name, dimension, and
  normalization must all agree; those three are what make vectors comparable).
* :class:`EmbeddingBackend` — the structural contract the concrete encoder
  (:class:`ragtools.embedding.encoder.Encoder`) already satisfies, so alternate
  or sparse backends can be swapped without touching the pipeline.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S9 -> G9; A5)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

_DEFAULT_BACKEND = "sentence-transformers"

#: Inference runtimes. Vectors are NOT interchangeable across these — see
#: ModelMetadata.runtime.
RUNTIME_TORCH = "torch"
RUNTIME_ONNX = "onnx"


class ModelMismatchError(RuntimeError):
    """A collection's stored model is incompatible with the current backend."""


@dataclass(frozen=True)
class ModelMetadata:
    """Identity of the model that produced a collection's vectors.

    Stored on the collection (payload / collection metadata) and enforced on
    every open. ``model_name``, ``dimension``, ``normalize`` **and ``runtime``**
    together decide whether two sets of vectors are comparable.

    ``runtime`` is load-bearing, not informational. Measured on this project
    (W5-A spike): the same ``all-MiniLM-L6-v2`` under torch vs ONNX produced
    mean cosine 0.9939 and **min 0.9087** — close, but not the same vectors. A
    collection built under one runtime must therefore refuse to be searched
    under the other until it is re-indexed. ``backend`` remains informational
    (the same runtime via a different wrapper library is still compatible).
    """

    model_name: str
    dimension: int
    normalize: bool = True
    backend: str = _DEFAULT_BACKEND
    runtime: str = RUNTIME_TORCH

    def to_dict(self) -> dict:
        return {
            "model_name": self.model_name,
            "dimension": self.dimension,
            "normalize": self.normalize,
            "backend": self.backend,
            "runtime": self.runtime,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ModelMetadata":
        return cls(
            model_name=data["model_name"],
            dimension=int(data["dimension"]),
            normalize=bool(data.get("normalize", True)),
            backend=data.get("backend", _DEFAULT_BACKEND),
            # Collections written before runtimes were tracked were torch.
            runtime=data.get("runtime", RUNTIME_TORCH),
        )


def _coerce(m) -> ModelMetadata:
    return m if isinstance(m, ModelMetadata) else ModelMetadata.from_dict(m)


def assert_model_compatible(stored, current) -> None:
    """Refuse (raise :class:`ModelMismatchError`) if the two models differ.

    Accepts :class:`ModelMetadata` or a plain dict (as it comes off a Qdrant
    payload) on either side. Compares the three identity fields that make
    vectors comparable — model name, dimension, and normalization.
    """
    s = _coerce(stored)
    c = _coerce(current)
    for field in ("model_name", "dimension", "normalize", "runtime"):
        if getattr(s, field) != getattr(c, field):
            raise ModelMismatchError(
                f"model mismatch on {field}: collection was built with "
                f"{s.model_name!r} (dim={s.dimension}, normalize={s.normalize}) "
                f"but the current backend is {c.model_name!r} "
                f"(dim={c.dimension}, normalize={c.normalize}). Refusing to "
                "search — re-index under the current model or reopen the correct one."
            )


@runtime_checkable
class EmbeddingBackend(Protocol):
    """Structural contract for an embedding backend.

    The concrete :class:`ragtools.embedding.encoder.Encoder` already provides
    ``encode_batch`` / ``encode_query`` / ``dimension``; adding ``metadata()``
    lets any backend declare the identity that :func:`assert_model_compatible`
    enforces. ``runtime_checkable`` so tests and wiring can assert conformance.
    """

    dimension: int

    def encode_batch(self, texts: list[str], batch_size: int = 64) -> np.ndarray: ...

    def encode_query(self, query: str) -> np.ndarray: ...

    def metadata(self) -> ModelMetadata: ...
