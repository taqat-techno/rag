"""Shared test fixtures for RAG Tools."""

import pytest

from ragtools.config import Settings


@pytest.fixture
def settings():
    """Create a Settings instance with defaults."""
    return Settings()


@pytest.fixture
def memory_client():
    """Create an in-memory Qdrant client for testing."""
    return Settings.get_memory_client()
