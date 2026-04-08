"""Tests for MCP server tool functions.

Tests the tool functions directly by patching module globals,
bypassing MCP transport (which is the SDK's responsibility).
"""

from pathlib import Path

import pytest

from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.indexing.indexer import ensure_collection, index_file
from ragtools.indexing.scanner import scan_project, get_relative_path
from ragtools.retrieval.searcher import Searcher
from ragtools.integration import mcp_server

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def mcp_env():
    """Set up MCP server globals with in-memory Qdrant and test fixtures."""
    settings = Settings(content_root=str(FIXTURES))
    client = Settings.get_memory_client()
    encoder = Encoder(settings.embedding_model)

    ensure_collection(client, settings.collection_name, encoder.dimension)

    for pid, file_path in scan_project(str(FIXTURES)):
        rel = get_relative_path(file_path, str(FIXTURES))
        index_file(
            client=client,
            encoder=encoder,
            collection_name=settings.collection_name,
            project_id=pid,
            file_path=file_path,
            relative_path=rel,
        )

    # Patch module globals for direct mode with per-request client
    mcp_server._mode = "direct"
    mcp_server._settings = settings
    mcp_server._encoder = encoder
    mcp_server._init_error = None

    # Monkey-patch _get_direct_client to return the test in-memory client
    original_get_client = mcp_server._get_direct_client
    mcp_server._get_direct_client = lambda: client

    yield settings, client, encoder

    # Reset globals after tests
    mcp_server._mode = "uninitialized"
    mcp_server._settings = None
    mcp_server._encoder = None
    mcp_server._init_error = None
    mcp_server._get_direct_client = original_get_client


# --- search_knowledge_base ---


class TestSearchKnowledgeBase:
    def test_basic_search(self, mcp_env):
        result = mcp_server.search_knowledge_base("backend architecture")
        assert "[RAG CONTEXT" in result
        assert "Source:" in result

    def test_project_filter(self, mcp_env):
        result = mcp_server.search_knowledge_base("documentation", project="project_a")
        assert "project_a" in result

    def test_top_k(self, mcp_env):
        result = mcp_server.search_knowledge_base("architecture", top_k=2)
        assert "[RAG CONTEXT" in result
        source_lines = [l for l in result.split("\n") if "Source:" in l]
        assert len(source_lines) <= 2

    def test_empty_query(self, mcp_env):
        result = mcp_server.search_knowledge_base("")
        assert "[RAG ERROR]" in result
        assert "empty" in result.lower()

    def test_whitespace_query(self, mcp_env):
        result = mcp_server.search_knowledge_base("   ")
        assert "[RAG ERROR]" in result

    def test_no_relevant_results(self, mcp_env):
        result = mcp_server.search_knowledge_base(
            "xyzzy flurbo garbanzoid completely irrelevant nonsense"
        )
        # Should get either RAG NOTICE (no results) or RAG CONTEXT with low confidence
        assert "[RAG" in result

    def test_not_initialized(self):
        """When server hasn't been initialized, return error."""
        old_mode = mcp_server._mode
        old_error = mcp_server._init_error
        mcp_server._mode = "direct"
        mcp_server._init_error = "Not initialized"
        try:
            result = mcp_server.search_knowledge_base("test")
            assert "[RAG ERROR]" in result
            assert "Not initialized" in result
        finally:
            mcp_server._mode = old_mode
            mcp_server._init_error = old_error


# --- list_projects ---


class TestListProjects:
    def test_lists_projects(self, mcp_env):
        result = mcp_server.list_projects()
        assert "project_a" in result
        assert "project_b" in result
        assert "Indexed projects" in result

    def test_not_initialized(self):
        old_mode = mcp_server._mode
        old_error = mcp_server._init_error
        mcp_server._mode = "direct"
        mcp_server._init_error = "Not ready"
        try:
            result = mcp_server.list_projects()
            assert "[RAG ERROR]" in result
        finally:
            mcp_server._mode = old_mode
            mcp_server._init_error = old_error


# --- index_status ---


class TestIndexStatus:
    def test_reports_ready(self, mcp_env):
        result = mcp_server.index_status()
        assert "[RAG STATUS]" in result
        assert "ready" in result.lower()
        assert "chunks" in result.lower()

    def test_shows_model(self, mcp_env):
        result = mcp_server.index_status()
        assert "all-MiniLM-L6-v2" in result

    def test_not_initialized(self):
        old_mode = mcp_server._mode
        old_error = mcp_server._init_error
        mcp_server._mode = "direct"
        mcp_server._init_error = "Knowledge base not initialized"
        try:
            result = mcp_server.index_status()
            assert "[RAG STATUS]" in result
            assert "not initialized" in result.lower()
        finally:
            mcp_server._mode = old_mode
            mcp_server._init_error = old_error
