"""Tests for MCP server proxy and direct modes."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ragtools.config import Settings
from ragtools.integration import mcp_server


FIXTURES = Path(__file__).parent / "fixtures"


# --- Proxy mode tests ---


class TestProxyMode:
    """Test MCP behavior when service is available (proxy mode)."""

    def setup_method(self):
        """Set up proxy mode with a mock HTTP client."""
        mcp_server._mode = "proxy"
        mcp_server._settings = Settings()
        self.mock_client = MagicMock()
        mcp_server._http_client = self.mock_client

    def teardown_method(self):
        mcp_server._mode = "uninitialized"
        mcp_server._http_client = None
        mcp_server._settings = None

    def test_search_forwards_to_service(self):
        self.mock_client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "formatted": "[RAG CONTEXT] Test results",
                "count": 1,
                "results": [],
            },
        )
        result = mcp_server.search_knowledge_base("test query")
        assert "[RAG CONTEXT]" in result
        self.mock_client.get.assert_called_once()
        call_args = self.mock_client.get.call_args
        assert call_args[0][0] == "/api/search"
        assert call_args[1]["params"]["query"] == "test query"

    def test_search_with_project_filter(self):
        self.mock_client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"formatted": "results", "count": 1, "results": []},
        )
        mcp_server.search_knowledge_base("query", project="project_a")
        params = self.mock_client.get.call_args[1]["params"]
        assert params["project"] == "project_a"

    def test_search_empty_query(self):
        result = mcp_server.search_knowledge_base("")
        assert "ERROR" in result

    def test_list_projects_forwards(self):
        self.mock_client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "projects": [
                    {"project_id": "proj_a", "files": 5, "chunks": 20},
                    {"project_id": "proj_b", "files": 3, "chunks": 10},
                ]
            },
        )
        result = mcp_server.list_projects()
        assert "proj_a" in result
        assert "proj_b" in result
        assert "2" in result  # count

    def test_list_projects_empty(self):
        self.mock_client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"projects": []},
        )
        result = mcp_server.list_projects()
        assert "No projects" in result

    def test_index_status_forwards(self):
        self.mock_client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "collection_name": "markdown_kb",
                "total_files": 10,
                "total_chunks": 50,
                "points_count": 50,
                "projects": ["proj_a"],
            },
        )
        result = mcp_server.index_status()
        assert "proxy mode" in result
        assert "50" in result

    def test_service_error_returns_clear_message(self):
        import httpx
        self.mock_client.get.side_effect = httpx.ConnectError("Connection refused")
        result = mcp_server.search_knowledge_base("test")
        assert "Service unavailable" in result
        assert "rag service start" in result

    def test_service_500_returns_error(self):
        self.mock_client.get.return_value = MagicMock(
            status_code=500,
            text="Internal Server Error",
        )
        result = mcp_server.search_knowledge_base("test")
        assert "ERROR" in result
        assert "500" in result


# --- Direct mode tests ---


class TestDirectMode:
    """Test MCP behavior when service is unavailable (direct/fallback mode)."""

    def setup_method(self):
        mcp_server._mode = "direct"
        mcp_server._settings = Settings(content_root=str(FIXTURES))
        mcp_server._init_error = None

    def teardown_method(self):
        mcp_server._mode = "uninitialized"
        mcp_server._settings = None
        mcp_server._encoder = None
        mcp_server._searcher = None
        mcp_server._init_error = None

    def test_direct_mode_with_init_error(self):
        mcp_server._init_error = "Knowledge base not initialized."
        result = mcp_server.search_knowledge_base("test")
        assert "ERROR" in result
        assert "not initialized" in result

    def test_direct_index_status_with_init_error(self):
        mcp_server._init_error = "Collection not found."
        result = mcp_server.index_status()
        assert "STATUS" in result
        assert "not found" in result

    def test_direct_list_projects_with_init_error(self):
        mcp_server._init_error = "Not ready."
        result = mcp_server.list_projects()
        assert "ERROR" in result


# --- Mode detection tests ---


class TestModeDetection:
    """Test the initialization/mode selection logic."""

    def teardown_method(self):
        mcp_server._mode = "uninitialized"
        mcp_server._http_client = None
        mcp_server._settings = None
        mcp_server._encoder = None
        mcp_server._searcher = None
        mcp_server._init_error = None

    @patch("httpx.get")
    def test_proxy_mode_when_service_available(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        mcp_server._initialize()
        assert mcp_server._mode == "proxy"
        assert mcp_server._http_client is not None
        assert mcp_server._encoder is None  # Not loaded in proxy mode

    @patch("httpx.get", side_effect=Exception("Connection refused"))
    def test_direct_mode_when_service_unavailable(self, mock_get):
        # This will try to load encoder + Qdrant — may fail in test env
        # but should at least set mode to "direct"
        mcp_server._initialize()
        assert mcp_server._mode == "direct"
        assert mcp_server._http_client is None

    def test_index_status_reports_mode(self):
        # Proxy mode
        mcp_server._mode = "proxy"
        mcp_server._settings = Settings()
        mock_client = MagicMock()
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "collection_name": "test",
                "total_files": 0,
                "total_chunks": 0,
                "points_count": 0,
                "projects": [],
            },
        )
        mcp_server._http_client = mock_client
        result = mcp_server.index_status()
        assert "proxy" in result
