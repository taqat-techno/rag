"""Embedding model wrapper using Sentence Transformers."""

import threading
from collections import OrderedDict

import numpy as np
from sentence_transformers import SentenceTransformer

_QUERY_CACHE_SIZE = 128


class Encoder:
    """Thin wrapper around SentenceTransformer for consistent encoding."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        self.model = SentenceTransformer(model_name, device="cpu")
        self.dimension = self.model.get_sentence_embedding_dimension()
        self._query_cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._cache_lock = threading.Lock()

    def metadata(self):
        """Declare this backend's model identity for collection enforcement (A5).

        Satisfies :class:`ragtools.embedding.backend.EmbeddingBackend`; the
        returned record is what :func:`~ragtools.embedding.backend.assert_model_compatible`
        checks on every collection open. ``normalize`` is always True here —
        :meth:`encode_batch` / :meth:`encode_query` pass ``normalize_embeddings=True``.
        """
        from ragtools.embedding.backend import ModelMetadata

        return ModelMetadata(
            model_name=self.model_name, dimension=self.dimension, normalize=True
        )

    def encode_batch(self, texts: list[str], batch_size: int = 64) -> np.ndarray:
        """Encode a list of texts into normalized embeddings.

        Args:
            texts: List of strings to encode.
            batch_size: Batch size for encoding.

        Returns:
            numpy array of shape (len(texts), dimension)
        """
        return self.model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=len(texts) > 100,
            convert_to_numpy=True,
        )

    def encode_query(self, query: str) -> np.ndarray:
        """Encode a single query string with LRU caching. Thread-safe.

        Returns:
            numpy array of shape (dimension,)
        """
        with self._cache_lock:
            if query in self._query_cache:
                self._query_cache.move_to_end(query)
                return self._query_cache[query]

        embedding = self.model.encode(
            query,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        with self._cache_lock:
            self._query_cache[query] = embedding
            if len(self._query_cache) > _QUERY_CACHE_SIZE:
                self._query_cache.popitem(last=False)
        return embedding
