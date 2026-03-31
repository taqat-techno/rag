"""Configuration for RAG Tools."""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables with RAG_ prefix."""

    # Qdrant local mode
    qdrant_path: str = "data/qdrant"
    collection_name: str = "markdown_kb"

    # Embedding
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384

    # Chunking
    chunk_size: int = 400
    chunk_overlap: int = 100

    # Content
    content_root: str = "."

    # Retrieval
    top_k: int = 10
    score_threshold: float = 0.3

    # State
    state_db: str = "data/index_state.db"

    model_config = {"env_prefix": "RAG_", "env_file": ".env", "env_file_encoding": "utf-8"}

    def get_qdrant_client(self):
        """Create a Qdrant client in local persistent mode."""
        from qdrant_client import QdrantClient

        Path(self.qdrant_path).mkdir(parents=True, exist_ok=True)
        return QdrantClient(path=self.qdrant_path)

    @staticmethod
    def get_memory_client():
        """Create an in-memory Qdrant client for testing."""
        from qdrant_client import QdrantClient

        return QdrantClient(":memory:")
