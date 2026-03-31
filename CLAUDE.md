# CLAUDE.md — Local Markdown RAG System

## Project Identity

Local-first, Markdown-only RAG system. Claude CLI searches a local Qdrant knowledge base first, then completes answers using its own reasoning.

**Stack:** Python 3.12 / Qdrant local mode / Sentence Transformers / Claude CLI (MCP)
**No Docker. No cloud. No server processes. No containers.**

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
- **Fallback to paragraph splitting** if a section exceeds chunk_size
- **chunk_size=400 tokens, chunk_overlap=100 tokens**
- **Prepend heading hierarchy** to chunk text before embedding (e.g., "Architecture > Backend\n\n...")
- **Store raw text** (without headings) in payload for display
- **Deterministic chunk IDs** — `sha256(project_id + file_path + chunk_index)[:16]`

### Retrieval
- **Score threshold: 0.3** — below this, results are excluded
- **Confidence labels**: HIGH (>=0.7), MODERATE (0.5-0.7), LOW (<0.5)
- **Always include confidence in formatted context** — Claude must know when retrieval is weak
- **top_k=10** default

### Integration
- **MCP server** for Claude CLI — exposes `search_knowledge_base` tool
- **Single-process constraint** — do not run indexer and MCP server simultaneously on the same data directory

## Package Structure

```
src/ragtools/
  cli.py              — Typer CLI
  config.py           — Pydantic Settings from .env
  models.py           — Chunk, FileRecord, SearchResult
  chunking/           — Markdown parsing and splitting
  embedding/          — SentenceTransformer wrapper
  indexing/            — Scanner, indexer, state tracking
  retrieval/           — Searcher, formatter
  integration/         — MCP server
```

## Key Commands

```bash
pip install -e .          # Install in dev mode
pip install -e ".[dev]"   # With test dependencies
rag index .               # Index all Markdown files
rag index --full .        # Force full re-index
rag search "query"        # Search the knowledge base
rag status                # Show collection stats
rag doctor                # Health check
rag rebuild               # Drop everything, re-index from scratch
rag projects              # List indexed projects
pytest                    # Run tests
```

## Testing

- **Always use `QdrantClient(":memory:")` for tests** — never touch real data
- **Fixture files in `tests/fixtures/`** — sample .md files for each test scenario
- **Test chunking independently** from indexing
- **Test retrieval independently** from formatting

## What NOT to Do

- Do NOT suggest Docker, containers, or server-mode Qdrant for MVP
- Do NOT add LangChain or LlamaIndex as dependencies — we use libraries directly
- Do NOT use JSON files for state — use SQLite
- Do NOT create multiple Qdrant collections — one collection, payload filtering
- Do NOT change the embedding model without planning a full rebuild
- Do NOT suggest cloud services, APIs, or hosted solutions
- Do NOT add cross-encoder reranking, hybrid search, or SPLADE for MVP — these are post-MVP
- Do NOT add watchfiles/auto-indexing for MVP
- Do NOT add a web UI
- Do NOT open the Qdrant data directory from multiple processes simultaneously

## Build Order

Stage 0 → 1 → 2 → 3 → 4 (MVP) → 5 → 6 → 7

Each stage must be tested before moving to the next. See `implementation_plan_local.md` for full details.

## Dependencies

```
qdrant-client>=1.12.0
sentence-transformers>=5.0.0
typer[all]>=0.12.0
pydantic>=2.0.0
pydantic-settings>=2.0.0
python-frontmatter>=1.0.0
markdown-it-py>=3.0.0
rich>=13.0.0
```

Dev: `pytest>=8.0.0`, `pytest-cov>=5.0.0`

## File Conventions

- Source in `src/ragtools/`
- Tests in `tests/`
- Scripts in `scripts/`
- All local data in `data/` (gitignored)
- Config in `.env` with `RAG_` prefix
