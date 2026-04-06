"""MCP server exposing RAG tools to Claude CLI.

Two modes determined at startup:
  - Proxy mode: service is running → forward all calls via HTTP (instant startup)
  - Direct mode: service unavailable → load encoder + Qdrant locally (5-10s startup)

Mode is locked for the session. No mid-session switching.

Run directly: python -m ragtools.integration.mcp_server
Or via entry point: rag-mcp
Or via CLI: rag serve
"""

import logging
import sys

from mcp.server.fastmcp import FastMCP

from ragtools.config import Settings

logger = logging.getLogger(__name__)

# --- Global state ---

_settings: Settings | None = None
_mode: str = "uninitialized"  # "proxy" | "direct" | "uninitialized"

# Proxy mode state
_http_client = None  # httpx.Client when in proxy mode

# Direct mode state (legacy)
_encoder = None
_searcher = None
_init_error: str | None = None

mcp_app = FastMCP("ragtools")


def _initialize() -> None:
    """Determine mode and initialize accordingly."""
    global _mode, _http_client, _settings, _encoder, _searcher, _init_error

    _settings = Settings()

    # Try proxy mode first — probe the service
    try:
        import httpx

        url = f"http://{_settings.service_host}:{_settings.service_port}/health"
        r = httpx.get(url, timeout=2.0)
        if r.status_code == 200:
            _mode = "proxy"
            _http_client = httpx.Client(
                base_url=f"http://{_settings.service_host}:{_settings.service_port}",
                timeout=httpx.Timeout(5.0, read=120.0),
            )
            _init_error = None
            logger.info("MCP initialized in PROXY mode (service at %s:%d)",
                        _settings.service_host, _settings.service_port)
            return
    except Exception as e:
        logger.debug("Service probe failed: %s", e)

    # Fallback: direct mode (load encoder + open Qdrant)
    _mode = "direct"
    logger.info("MCP initializing in DIRECT mode (service unavailable)")

    try:
        from pathlib import Path

        from ragtools.embedding.encoder import Encoder
        from ragtools.retrieval.searcher import Searcher

        _encoder = Encoder(_settings.embedding_model)

        qdrant_path = Path(_settings.qdrant_path)
        if not qdrant_path.exists():
            _init_error = (
                "Knowledge base not initialized. "
                "Run `rag index <path>` to index your Markdown files first."
            )
            return

        client = _settings.get_qdrant_client()

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
        logger.exception("Direct mode initialization failed")


def _check_ready() -> str | None:
    """Return error message if not ready, or None if OK."""
    if _mode == "proxy":
        return None  # Proxy is always "ready" — errors are per-request
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
    if not query or not query.strip():
        return "[RAG ERROR] Query cannot be empty."

    if _mode == "proxy":
        return _proxy_search(query.strip(), project, top_k)
    else:
        return _direct_search(query.strip(), project, top_k)


@mcp_app.tool()
def list_projects() -> str:
    """List all indexed projects in the knowledge base.

    Returns project IDs that can be used with the `project` parameter
    in search_knowledge_base.
    """
    if _mode == "proxy":
        return _proxy_list_projects()
    else:
        return _direct_list_projects()


@mcp_app.tool()
def index_status() -> str:
    """Check the status of the local knowledge base index.

    Returns collection statistics including total chunks and configuration.
    Useful to verify the index exists before searching.
    """
    if _mode == "proxy":
        return _proxy_index_status()
    else:
        return _direct_index_status()


# --- Proxy mode implementations ---


def _proxy_search(query: str, project: str | None, top_k: int) -> str:
    try:
        params = {"query": query, "top_k": top_k}
        if project:
            params["project"] = project
        r = _http_client.get("/api/search", params=params)
        if r.status_code == 200:
            return r.json()["formatted"]
        return f"[RAG ERROR] Service returned {r.status_code}: {r.text}"
    except Exception as e:
        return _proxy_error(e)


def _proxy_list_projects() -> str:
    try:
        r = _http_client.get("/api/projects")
        if r.status_code == 200:
            data = r.json()["projects"]
            if not data:
                return "No projects found in the knowledge base."
            lines = [f"Indexed projects ({len(data)}):"]
            for p in data:
                lines.append(f"  - {p['project_id']} ({p['files']} files, {p['chunks']} chunks)")
            return "\n".join(lines)
        return f"[RAG ERROR] Service returned {r.status_code}: {r.text}"
    except Exception as e:
        return _proxy_error(e)


def _proxy_index_status() -> str:
    try:
        r = _http_client.get("/api/status")
        if r.status_code == 200:
            data = r.json()
            return (
                f"[RAG STATUS] Knowledge base is ready (proxy mode).\n"
                f"  Collection: {data.get('collection_name', 'unknown')}\n"
                f"  Total files: {data.get('total_files', 0)}\n"
                f"  Total chunks: {data.get('total_chunks', 0)}\n"
                f"  Points: {data.get('points_count', 0)}\n"
                f"  Projects: {', '.join(data.get('projects', []))}\n"
                f"  Mode: proxy (forwarding to service)"
            )
        return f"[RAG ERROR] Service returned {r.status_code}: {r.text}"
    except Exception as e:
        return _proxy_error(e)


def _proxy_error(e: Exception) -> str:
    """Format a proxy-mode error. Does NOT attempt to switch to direct mode."""
    import httpx
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
        return (
            "[RAG ERROR] Service unavailable. "
            "The RAG service may have stopped. "
            "Restart with: rag service start"
        )
    return f"[RAG ERROR] Proxy request failed: {e}"


# --- Direct mode implementations (preserved from original) ---


def _direct_search(query: str, project: str | None, top_k: int) -> str:
    error = _check_ready()
    if error:
        return f"[RAG ERROR] {error}"

    try:
        from ragtools.retrieval.formatter import format_context

        results = _searcher.search(
            query=query,
            project_id=project,
            top_k=top_k,
            score_threshold=_settings.score_threshold,
        )
        return format_context(results, query)
    except Exception as e:
        logger.exception("Search failed")
        return f"[RAG ERROR] Search failed: {e}"


def _direct_list_projects() -> str:
    error = _check_ready()
    if error:
        return f"[RAG ERROR] {error}"

    try:
        client = _searcher.client
        collection_name = _settings.collection_name

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


def _direct_index_status() -> str:
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
            f"[RAG STATUS] Knowledge base is ready (direct mode).\n"
            f"  Collection: {collection_name}\n"
            f"  Total chunks: {count}\n"
            f"  Embedding model: {_settings.embedding_model}\n"
            f"  Score threshold: {_settings.score_threshold}\n"
            f"  Mode: direct (local encoder + Qdrant)"
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
