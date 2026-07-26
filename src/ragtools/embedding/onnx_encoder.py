"""ONNX embedding backend (Phase 5 / W5-A).

Drop-in replacement for :class:`ragtools.embedding.encoder.Encoder` that runs the
same model under **ONNX Runtime instead of PyTorch**, satisfying the existing
:class:`~ragtools.embedding.backend.EmbeddingBackend` protocol.

Measured on this project (W5-A spike, warm, `all-MiniLM-L6-v2`):

===================  =============  =============  ==========
metric               torch + ST     ONNX           delta
===================  =============  =============  ==========
import               3.99 s         0.50 s         8x faster
model load           0.26 s         0.15 s         --
cold-start floor     4.26 s         0.65 s         6.5x faster
batch (15 texts)     175.6 ms       75.4 ms        2.3x faster
ML stack on disk     508 MB         54 MB          -455 MB
===================  =============  =============  ==========

**Vectors are NOT interchangeable with the torch runtime.** The same model under
the two runtimes measured mean cosine 0.9939 with a **minimum of 0.9087**. That
is why :class:`~ragtools.embedding.backend.ModelMetadata` carries ``runtime`` and
why the compatibility gate refuses a cross-runtime open: adopting this backend
requires a re-index, which is why it is sequenced onto the one planned migration.

``fastembed`` is an optional dependency; importing this module without it raises
a clear error rather than failing deep inside a search.

Plan: docs/planning/RAG_MODERNIZATION_MASTER_PLAN.md  (Phase 5 -> G5, D6/D7)
"""

from __future__ import annotations

import threading
from collections import OrderedDict

import numpy as np

from ragtools.embedding.backend import RUNTIME_ONNX, ModelMetadata

_QUERY_CACHE_SIZE = 128

#: fastembed publishes the model under its own namespace; this is the same
#: `all-MiniLM-L6-v2` weights re-exported to ONNX.
DEFAULT_ONNX_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class OnnxEncoder:
    """Encoder backed by ONNX Runtime via ``fastembed``."""

    def __init__(self, model_name: str = DEFAULT_ONNX_MODEL, *, cache_dir: str | None = None):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise ImportError(
                "The ONNX embedding backend requires 'fastembed'. Install it with "
                "`pip install \"qdrant-client[fastembed]\"` (it brings ONNX Runtime "
                "and does NOT install PyTorch)."
            ) from exc

        self.model_name = model_name
        kwargs = {"model_name": model_name}
        if cache_dir:
            kwargs["cache_dir"] = cache_dir
        self._model = TextEmbedding(**kwargs)
        # fastembed L2-normalises its output; verified in the W5-A spike
        # (candidate L2 norm min=max=1.0000).
        self._normalize = True
        self.dimension = self._probe_dimension()
        self._query_cache: OrderedDict = OrderedDict()
        self._cache_lock = threading.Lock()

    def _probe_dimension(self) -> int:
        vec = next(iter(self._model.embed(["dimension probe"])))
        return int(len(vec))

    # -- EmbeddingBackend protocol ---------------------------------------

    def encode_batch(self, texts: list, batch_size: int = 64) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)
        vecs = list(self._model.embed(texts, batch_size=batch_size))
        return np.asarray(vecs, dtype=np.float32)

    def encode_query(self, query: str) -> np.ndarray:
        with self._cache_lock:
            if query in self._query_cache:
                self._query_cache.move_to_end(query)
                return self._query_cache[query]

        vec = np.asarray(next(iter(self._model.embed([query]))), dtype=np.float32)

        with self._cache_lock:
            self._query_cache[query] = vec
            if len(self._query_cache) > _QUERY_CACHE_SIZE:
                self._query_cache.popitem(last=False)
        return vec

    def metadata(self) -> ModelMetadata:
        """Declare identity INCLUDING the runtime, so a collection built under
        torch cannot be silently searched with ONNX vectors."""
        return ModelMetadata(
            model_name=self.model_name,
            dimension=self.dimension,
            normalize=self._normalize,
            backend="fastembed",
            runtime=RUNTIME_ONNX,
        )
