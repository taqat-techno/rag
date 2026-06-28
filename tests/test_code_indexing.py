"""End-to-end validation: code files are indexed, retrievable, and surfaced.

Proves the Success Criteria:
  - RAG indexes both documentation AND source code.
  - Retrieval works over code.
  - Feature requests trigger a codebase-first search.
  - Retrieved files appear in the generated (formatted) response.
"""

from pathlib import Path

import pytest

from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.indexing.indexer import ensure_collection, index_file
from ragtools.indexing.scanner import scan_project, get_relative_path
from ragtools.integration import mcp_server
from ragtools.retrieval.dev_pipeline import dev_search
from ragtools.retrieval.feature_intent import detect_dev_intent
from ragtools.retrieval.formatter import format_dev_context
from ragtools.retrieval.searcher import Searcher

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def indexed():
    """Index the code_project fixture into an in-memory Qdrant collection."""
    settings = Settings(content_root=str(FIXTURES))
    client = Settings.get_memory_client()
    encoder = Encoder(settings.embedding_model)
    ensure_collection(client, settings.collection_name, encoder.dimension)

    files = scan_project(str(FIXTURES), project_id="code_project")
    indexed_paths = []
    for pid, fp in files:
        rel = get_relative_path(fp, str(FIXTURES))
        index_file(
            client=client,
            encoder=encoder,
            collection_name=settings.collection_name,
            project_id=pid,
            file_path=fp,
            relative_path=rel,
        )
        indexed_paths.append(rel)

    searcher = Searcher(client=client, encoder=encoder, settings=settings)
    return settings, client, encoder, searcher, indexed_paths


class TestCodeFilesIndexed:
    def test_scanner_discovers_code_and_docs(self, indexed):
        _, _, _, _, paths = indexed
        joined = " ".join(paths)
        assert "auth_service.py" in joined          # python
        assert "routes.ts" in joined                # typescript (nested)
        assert "settings.yaml" in joined            # config (nested)
        assert "README.md" in joined                # documentation

    def test_code_chunks_have_metadata(self, indexed):
        _, client, _, _, _ = indexed
        from qdrant_client import models
        points, _ = client.scroll(
            collection_name="markdown_kb",
            limit=256,
            with_payload=True,
            with_vectors=False,
        )
        py_points = [p for p in points if p.payload.get("file_path", "").endswith("auth_service.py")]
        assert py_points
        assert all(p.payload["language"] == "python" for p in py_points)
        assert all(p.payload["extension"] == ".py" for p in py_points)
        chunk_types = {p.payload["chunk_type"] for p in py_points}
        assert "code" in chunk_types          # at least one code chunk
        assert "comment" in chunk_types        # module docstring extracted as comment


class TestRetrievalWorks:
    def test_search_finds_code(self, indexed):
        _, _, _, searcher, _ = indexed
        results = searcher.search("authenticate a user and issue a token", project_id="code_project")
        assert results
        assert any(r.file_path.endswith("auth_service.py") for r in results)

    def test_chunk_type_filter(self, indexed):
        _, _, _, searcher, _ = indexed
        code_only = searcher.search("router endpoint", project_id="code_project", chunk_types=["code"])
        assert code_only
        assert all(r.chunk_type == "code" for r in code_only)

    def test_metadata_round_trips(self, indexed):
        _, _, _, searcher, _ = indexed
        results = searcher.search("issue session token", project_id="code_project", chunk_types=["code"])
        assert results
        assert any(r.language == "python" for r in results)


class TestFeatureRequestTriggersSearch:
    def test_dev_intent_detected(self):
        assert detect_dev_intent("implement a new API endpoint for sessions")

    def test_dev_search_runs_layers(self, indexed):
        _, _, _, searcher, _ = indexed
        outcome = dev_search(searcher, "add an API endpoint that validates a token",
                             project_id="code_project")
        assert outcome.is_dev_request
        assert outcome.triggers
        assert outcome.results
        assert outcome.layers["code"] >= 1


class TestRetrievedFilesAppearInResponse:
    def test_dev_context_lists_files(self, indexed):
        _, _, _, searcher, _ = indexed
        outcome = dev_search(searcher, "implement token validation in the auth service",
                             project_id="code_project")
        formatted = format_dev_context(outcome.results, "implement token validation", outcome.triggers)
        assert "Relevant Files:" in formatted
        assert "Existing Implementation:" in formatted
        assert "Recommended Changes:" in formatted
        assert "Sample Code:" in formatted
        assert "auth_service.py" in formatted

    def test_mcp_search_project_context_direct(self, indexed):
        settings, client, encoder, _, _ = indexed
        # Wire MCP server direct-mode globals to the in-memory client.
        old = (mcp_server._mode, mcp_server._settings, mcp_server._encoder,
               mcp_server._init_error, mcp_server._get_direct_client)
        mcp_server._mode = "direct"
        mcp_server._settings = settings
        mcp_server._encoder = encoder
        mcp_server._init_error = None
        mcp_server._get_direct_client = lambda: client
        try:
            out = mcp_server.search_project_context(
                "implement a new endpoint to validate auth tokens",
                project="code_project",
            )
            assert "Relevant Files:" in out
            assert "auth_service.py" in out or "routes.ts" in out
        finally:
            (mcp_server._mode, mcp_server._settings, mcp_server._encoder,
             mcp_server._init_error, mcp_server._get_direct_client) = old
