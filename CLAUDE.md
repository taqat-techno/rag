# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Identity

Local-first, Markdown-only RAG system. Claude CLI searches a local Qdrant knowledge base first, then completes answers using its own reasoning.

**Stack:** Python 3.12 / Qdrant local mode / Sentence Transformers / Claude CLI (MCP)
**No Docker. No cloud. No containers.**
**Evolving into:** long-running local service + web admin panel + Windows startup integration

## Architecture Decisions (Non-Negotiable)

### Storage
- **Qdrant local mode only** — `QdrantClient(path="./data/qdrant")`, never `host=`/`port=`
- **One collection** named `markdown_kb` for all projects
- **Project isolation via payload filter** — `project_id` field on every point, keyword index
- **SQLite for state tracking** — `data/index_state.db`, not JSON files
- **All persistent state in `data/`** — delete it to start fresh

### Embeddings
- **Model: `all-MiniLM-L6-v2`** (384 dims, cosine distance) — do not change without rebuilding the entire index
- **Always normalize embeddings** — `normalize_embeddings=True`
- **Batch encode** at 64-128 batch size

### Chunking
- **Heading-based chunking** — split at `##`, `###`, `####` boundaries
- **Fallback to paragraph splitting** if a section exceeds chunk_size, then sentence splitting as last resort
- **chunk_size=400 tokens, chunk_overlap=100 tokens**
- **Prepend heading hierarchy** to chunk text before embedding (e.g., "Architecture > Backend\n\n...")
- **Store raw text** (without headings) in payload for display
- **Deterministic chunk IDs** — `sha256(project_id::file_path::chunk_index)` formatted as UUID (Qdrant requires valid UUID strings)

### Retrieval
- **Score threshold: 0.3** — below this, results are excluded
- **Confidence labels**: HIGH (>=0.7), MODERATE (0.5-0.7), LOW (<0.5)
- **Always include confidence in formatted context** — Claude must know when retrieval is weak
- **top_k=10** default

### Integration
- **MCP server** for Claude CLI — exposes `search_knowledge_base`, `list_projects`, `index_status` tools
- **Single-process constraint** — do not run indexer/watcher and MCP server simultaneously on the same Qdrant data directory

### Ignore Rules (Phase 1+)
- **`.ragignore` files** — gitignore syntax, per-directory scope
- **Three layers:** built-in defaults > global config `[ignore].patterns` > `.ragignore` files
- **Matching:** `pathspec` library (gitignore spec, `!` negation supported)
- **Enforcement:** scanner and watcher, NOT indexer (scanner already filtered)

## Service Architecture (Phase 2+)

Full decisions in `docs/decisions.md`. Key constraints:

- **Single-process model** — the service process is the sole Qdrant owner. Watcher runs as a daemon thread. Encoder shared with `threading.RLock`.
- **CLI dual-mode** — commands probe `localhost:21420/health` (1s timeout). If service responds, forward via HTTP. If not, fall back to direct Qdrant access (current behavior).
- **MCP proxy mode** — MCP server probes service at startup. If available, becomes a thin HTTP proxy (instant startup). Otherwise falls back to direct mode (current 5-10s startup).
- **Config resolution** — `RAG_CONFIG_PATH` env > `%LOCALAPPDATA%\RAGTools\config.toml` > `./ragtools.toml`. Env vars always override config file. TOML format.
- **Data directory** — dev: `./data/` (current). Installed: `%LOCALAPPDATA%\RAGTools\`. Detected automatically.
- **Service port** — `127.0.0.1:21420`, localhost-only, no auth.
- **Logging** — service mode: `RotatingFileHandler` at `{data_dir}/logs/service.log`, 10MB, 3 backups.
- **Startup** — Task Scheduler (Phase 5), not Startup Folder or Windows Service.

## Data Pipeline

```
Scanner (scanner.py)           — discovers projects (each subdir of content_root = project_id)
  → discover_markdown_files    — rglob("*.md"), skips SKIP_DIRS (.git, node_modules, .venv, etc.)
Chunker (chunking/markdown.py) — splits by heading boundaries → paragraph → sentence fallback
Encoder (embedding/encoder.py) — SentenceTransformer, encodes chunk.text (heading-enriched)
Indexer (indexing/indexer.py)   — upserts PointStruct to Qdrant, tracks state in SQLite
Searcher (retrieval/searcher.py) — query_points with optional project_id filter
Formatter (retrieval/formatter.py) — formats results with confidence labels and source attribution
```

**Project discovery convention:** each immediate subdirectory of `content_root` becomes a `project_id`. Directories starting with `.` or `_` are skipped.

## Key Commands

```bash
pip install -e ".[dev]"           # Install with test dependencies
rag index .                       # Incremental index (skips unchanged)
rag index --full .                # Force full re-index
rag index --project my_proj .     # Index single project
rag search "query"                # Search knowledge base
rag search "query" -p my_proj -k 5  # Filter by project, limit results
rag status                        # Show collection stats
rag doctor                        # Health check
rag rebuild                       # Drop everything, re-index from scratch
rag projects                      # List indexed projects with counts
rag watch .                       # Auto-index on .md changes (Ctrl+C to stop)
rag serve                         # Start the MCP server (core + user-enabled optional tools)
rag version                       # Show version
```

## Testing

```bash
pytest                            # Run all tests
pytest tests/test_chunking.py     # Single test file
pytest -k "test_search"           # Filter by name
pytest --cov=ragtools             # With coverage
python scripts/eval_retrieval.py --questions tests/fixtures/eval_questions.json  # Eval harness
```

- **Always use `QdrantClient(":memory:")` for tests** — `Settings.get_memory_client()` helper exists
- **Fixture files in `tests/fixtures/`** — sample .md files with two projects (`project_a`, `project_b`)
- **Test modules mirror source**: `test_chunking`, `test_indexing`, `test_retrieval`, `test_integration`, `test_incremental`, `test_eval`

## What NOT to Do

- Do NOT add LangChain or LlamaIndex — we use libraries directly
- Do NOT use JSON files for state — use SQLite
- Do NOT create multiple Qdrant collections — one collection, payload filtering
- Do NOT change the embedding model without planning a full rebuild
- Do NOT suggest Docker, containers, server-mode Qdrant, cloud services, or hosted solutions
- Do NOT add cross-encoder reranking, hybrid search, or SPLADE — these are post-MVP
- Do NOT open the Qdrant data directory from multiple processes — the service is the sole owner (see `docs/decisions.md` Decision 1)
- Do NOT use React, npm, or any JS build step for the admin panel — htmx + Jinja2 only (see `docs/decisions.md` Decision 6)

## RAG Knowledge Base (MCP Tools)

One MCP server (`rag-mcp`) with per-tool access control. The agent's
visible toolset depends on which tools the user enabled in the admin
panel's "MCP Tool Access" card — disabled tools are never registered
at startup.

### Core tools — always available

- **search_knowledge_base(query, project?, top_k?)** — Search indexed Markdown content
- **list_projects()** — Discover available project IDs
- **index_status()** — Check if the knowledge base is ready

### Optional diagnostic tools — user-gated

Only registered if the user checked the box for each on the Settings page:

- **service_status()** — Live service state + watcher, scale, mode
- **recent_activity(limit?, level?)** — Structured activity-log slice
- **tail_logs(source, limit?)** — Whitelisted log tail
- **crash_history()** — Unreviewed crash markers
- **get_config()** / **get_ignore_rules()** / **get_paths()** — Config inspection
- **system_health()** — JSON form of `rag doctor`
- **list_indexed_paths(project?, limit?)** — State-DB file roster

All optional tools return the envelope `{ok, mode, as_of, data|error, hint?}`.
Their first-line docstrings include a WHEN / DO NOT USE guardrail so
the agent doesn't call them for content queries.

### Usage Rules

1. For project-specific facts, **always search first** before answering
2. Retrieved context is the **source of truth** for project-specific information
3. Use your own knowledge for explanation, reasoning, design advice, and best practices
4. If results show **LOW CONFIDENCE**, note this in your answer
5. If no results are found, say "no project-specific local content was available"
6. If the user asks about a specific project, **pass the project parameter**
7. Cite sources from retrieved chunks: `[Source: project/file | Section: heading]`
8. **Only use `ragtools-ops` tools when diagnosing the RAG system itself** — not for content queries

### Constraint

Do not run `rag index` while Claude CLI is using the MCP server — Qdrant local mode
only allows one process at a time.

## Configuration

All settings in `config.py` via Pydantic Settings. Override with env vars prefixed `RAG_` or `.env` file:

| Env Var | Default | Notes |
|---------|---------|-------|
| `RAG_QDRANT_PATH` | `data/qdrant` | Local Qdrant storage |
| `RAG_COLLECTION_NAME` | `markdown_kb` | Single collection name |
| `RAG_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Do not change without rebuild |
| `RAG_CHUNK_SIZE` | `400` | Target tokens per chunk |
| `RAG_CHUNK_OVERLAP` | `100` | Overlap tokens |
| `RAG_TOP_K` | `10` | Default search results |
| `RAG_SCORE_THRESHOLD` | `0.3` | Minimum similarity score |
| `RAG_CONTENT_ROOT` | `.` | Root for project discovery |
| `RAG_STATE_DB` | `data/index_state.db` | SQLite state path |

## Entry Points

Defined in `pyproject.toml`:
- `rag` → `ragtools.cli:app` (Typer CLI)
- `rag-mcp` → `ragtools.integration.mcp_server:main` (MCP server direct)
