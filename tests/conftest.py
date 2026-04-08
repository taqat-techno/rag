"""Shared test fixtures for RAG Tools."""

import os
import pytest
import tempfile

from ragtools.config import Settings


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Prevent tests from reading the CWD ragtools.toml.

    Points RAG_CONFIG_PATH to a non-existent file in a temp directory
    so Settings() uses only built-in defaults, not whatever ragtools.toml
    happens to be in the working directory.
    """
    fake_config = str(tmp_path / "ragtools.toml")
    monkeypatch.setenv("RAG_CONFIG_PATH", fake_config)


@pytest.fixture
def settings():
    """Create a Settings instance with defaults."""
    return Settings()


@pytest.fixture
def memory_client():
    """Create an in-memory Qdrant client for testing."""
    return Settings.get_memory_client()
