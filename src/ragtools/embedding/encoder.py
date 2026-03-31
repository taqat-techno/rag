"""Embedding model wrapper using Sentence Transformers."""

import numpy as np
from sentence_transformers import SentenceTransformer


class Encoder:
    """Thin wrapper around SentenceTransformer for consistent encoding."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model = SentenceTransformer(model_name)
        self.dimension = self.model.get_sentence_embedding_dimension()

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
        """Encode a single query string.

        Returns:
            numpy array of shape (dimension,)
        """
        return self.model.encode(
            query,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
