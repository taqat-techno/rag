"""MCP server exposing RAG tools to Claude CLI.

Run directly: python -m ragtools.integration.mcp_server
Or via entry point: rag-mcp
Or via CLI: rag serve
"""

import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.retrieval.formatter import format_context
from ragtools.retrieval.searcher import Searcher

logger = logging.getLogger(__name__)

# --- Global state (initialized once at startup) ---

_settings: Settings | None = None
_encoder: Encoder | None = None
_searcher: Searcher | None = None
_init_error: str | None = None

mcp_app = FastMCP("ragtools")


def _initialize() -> None:
    """Load encoder and connect to Qdrant. Called once at startup."""
    global _settings, _encoder, _searcher, _init_error
    try:
        _settings = Settings()

        # Load embedding model (takes 5-10 seconds)
        _encoder = Encoder(_settings.embedding_model)

        # Check if Qdrant data directory exists
        qdrant_path = Path(_settings.qdrant_path)
        if not qdrant_path.exists():
            _init_error = (
                "Knowledge base not initialized. "
                "Run `rag index <path>` to index your Markdown files first."
            )
            return

        client = _settings.get_qdrant_client()

        # Check collection exists
        collections = [c.name for c in client.get_collections().collections]
        if _settings.collection_name not in collections:
            _init_error = (
                f"Collection '{_settings.collection_name}' not found. "
                "Run `rag index <path>` to create the index."
            )
            return

        _searcher = Searcher(client=client, encoder=_encoder, settings=_settings)
        _init_error = None

    except Exception as e:
        _init_error = f"Failed to initialize RAG server: {e}"
        logger.exception("Initialization failed")


def _check_ready() -> str | None:
    """Return an error message if the server is not ready, or None if OK."""
    return _init_error


# --- MCP Tools ---


@mcp_app.tool()
def search_knowledge_base(
    query: str,
    project: str | None = None,
    top_k: int = 10,
) -> str:
    """Search the local Markdown knowledge base for information relevant to a query.

    Returns formatted context with source attribution and confidence scores.
    Use the `project` parameter to restrict search to a specific project.
    Use `list_projects` first to discover available project IDs.

    Args:
        query: Natural language search query describing what you need.
        project: Optional project ID to filter results (use list_projects to see IDs).
        top_k: Maximum number of results to return (default 10).
    """
    error = _check_ready()
    if error:
        return f"[RAG ERROR] {error}"

    if not query or not query.strip():
        return "[RAG ERROR] Query cannot be empty."

    try:
        results = _searcher.search(
            query=query.strip(),
            project_id=project,
            top_k=top_k,
            score_threshold=_settings.score_threshold,
        )
        return format_context(results, query.strip())
    except Exception as e:
        logger.exception("Search failed")
        return f"[RAG ERROR] Search failed: {e}"


@mcp_app.tool()
def list_projects() -> str:
    """List all indexed projects in the knowledge base.

    Returns project IDs that can be used with the `project` parameter
    in search_knowledge_base.
    """
    error = _check_ready()
    if error:
        return f"[RAG ERROR] {error}"

    try:
        client = _searcher.client
        collection_name = _settings.collection_name

        # Scroll to extract unique project_ids
        all_project_ids: set[str] = set()
        offset = None
        while True:
            results, offset = client.scroll(
                collection_name=collection_name,
                limit=100,
                offset=offset,
                with_payload=["project_id"],
                with_vectors=False,
            )
            for point in results:
                pid = point.payload.get("project_id")
                if pid:
                    all_project_ids.add(pid)
            if offset is None:
                break

        if not all_project_ids:
            return "No projects found in the knowledge base."

        projects = sorted(all_project_ids)
        lines = [f"Indexed projects ({len(projects)}):"]
        for p in projects:
            lines.append(f"  - {p}")
        return "\n".join(lines)

    except Exception as e:
        logger.exception("list_projects failed")
        return f"[RAG ERROR] Failed to list projects: {e}"


@mcp_app.tool()
def index_status() -> str:
    """Check the status of the local knowledge base index.

    Returns collection statistics including total chunks and configuration.
    Useful to verify the index exists before searching.
    """
    error = _check_ready()
    if error:
        return f"[RAG STATUS] {error}"

    try:
        client = _searcher.client
        collection_name = _settings.collection_name
        info = client.get_collection(collection_name)
        count = info.points_count

        if count == 0:
            return (
                "[RAG STATUS] Collection exists but is empty. "
                "Run `rag index <path>` to populate it."
            )

        return (
            f"[RAG STATUS] Knowledge base is ready.\n"
            f"  Collection: {collection_name}\n"
            f"  Total chunks: {count}\n"
            f"  Embedding model: {_settings.embedding_model}\n"
            f"  Score threshold: {_settings.score_threshold}"
        )

    except Exception as e:
        logger.exception("index_status failed")
        return f"[RAG ERROR] Failed to get index status: {e}"


# --- Entry point ---


def main():
    """Entry point for the MCP server."""
    _initialize()
    mcp_app.run(transport="stdio")


if __name__ == "__main__":
    main()
