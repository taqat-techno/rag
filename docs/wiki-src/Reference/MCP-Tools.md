# Reference: MCP Tools

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Source of truth** | `src/ragtools/integration/mcp_server.py` |

Reg exposes three tools to Claude CLI via the Model Context Protocol. The server runs as `rag-mcp` (entry point), `rag serve` (CLI subcommand), or `python -m ragtools.integration.mcp_server`. Startup picks **proxy mode** (service is up) or **direct mode** (service unavailable) — see [MCP Proxy Decision](Architecture-MCP-Proxy-Decision).

## Tool summary

| Tool | Arguments | Returns |
|---|---|---|
| `search_knowledge_base` | `query: str`, `project: str \| None = None`, `top_k: int = 10` | Formatted context string with source attribution + confidence bands |
| `list_projects` | none | Formatted project list (ids + counts in proxy mode; ids only in direct mode) |
| `index_status` | none | Formatted status (mode, collection, chunks, embedding model) |

All tools return **plain-text strings** — not JSON — per the MCP convention. Errors are returned as strings prefixed `[RAG ERROR]` or `[RAG STATUS]`.

## `search_knowledge_base(query, project=None, top_k=10)`

Search the local Markdown knowledge base.

### Arguments
- `query: str` — natural language query. Empty or whitespace-only returns `[RAG ERROR] Query cannot be empty.`
- `project: str | None` — optional project ID to filter results. Use `list_projects` first to discover IDs.
- `top_k: int` — maximum number of results (default 10).

### Behavior
- **Proxy mode:** forwards to `GET /api/search?query=...&top_k=...&compact=true[&project=...]`. Returns the service's `formatted` field.
- **Direct mode:** loads the encoder once at MCP startup, opens Qdrant per request, releases the lock before returning.

### Confidence labeling
Results are surfaced with their confidence band (HIGH / MODERATE / LOW) per [Confidence Model](Core-Concepts-Confidence-Model). Callers are expected to hedge their own responses accordingly.

### Example

```
search_knowledge_base(query="how does the watcher debounce work", top_k=5)
```
Returns a multi-line string with numbered results, each showing `(score) project/file | heading` plus a snippet.

## `list_projects()`

List all indexed projects.

### Returns
- **Proxy mode:** `f"Indexed projects (N):\n  - {project_id} ({files} files, {chunks} chunks)\n..."`. Includes counts.
- **Direct mode:** same header line, then ids only (no counts). The list is derived by scrolling unique `project_id` payload values from the collection.
- Empty collection: `"No projects found in the knowledge base."`

### Use before `search_knowledge_base`
Claude typically calls `list_projects` first to learn valid `project` values before filtering a search.

## `index_status()`

Report whether the knowledge base is ready and, if so, what's in it.

### Returns (proxy mode)
```
[RAG STATUS] Knowledge base is ready (proxy mode).
  Collection: markdown_kb
  Total files: 47
  Total chunks: 612
  Points: 612
  Projects: docs-internal, notes, ...
  Mode: proxy (forwarding to service)
```

### Returns (direct mode)
```
[RAG STATUS] Knowledge base is ready (direct mode).
  Collection: markdown_kb
  Total chunks: 612
  Embedding model: all-MiniLM-L6-v2
  Score threshold: 0.3
  Mode: direct (per-request Qdrant access — lock released between queries)
```

### Returns (empty collection)
```
[RAG STATUS] Collection exists but is empty. Run `rag index <path>` to populate it.
```

### Returns (not initialized)
```
[RAG STATUS] Knowledge base not initialized. Run `rag index <path>` ...
```

## Startup behavior

On launch the MCP server probes `/health` twice with 2 s retry. On success → proxy mode (~100 ms startup). On failure → direct mode (~5-10 s for encoder load). **Mode is locked for the session** — it does not switch if the service becomes available mid-session. See [MCP Proxy Decision](Architecture-MCP-Proxy-Decision).

## Logging

The MCP server sends all logs to **stderr** so stdout stays clean for the MCP stdio transport. `httpx` and `httpcore` INFO-level logs are suppressed for the same reason.

## MCP config JSON

The service exposes `GET /api/mcp-config` which returns the correct `mcpServers` entry for the running install (frozen exe → `sys.executable serve`; dev → `rag-mcp`; fallback → `python -m ragtools.integration.mcp_server`). This is the canonical way to connect Claude CLI — paste the output into `.mcp.json`. See [Reference: HTTP API](Reference-HTTP-API#mcp-connection).

## Code paths

- `src/ragtools/integration/mcp_server.py:_initialize` (line 38) — mode selection.
- `src/ragtools/integration/mcp_server.py:search_knowledge_base` (line 121) — tool definition.
- `src/ragtools/integration/mcp_server.py:list_projects` (line 147).
- `src/ragtools/integration/mcp_server.py:index_status` (line 160).
- `src/ragtools/integration/mcp_server.py:main` (line 349) — entry point.

## Deprecations

None as of v2.4.2.
