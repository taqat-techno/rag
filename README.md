# RAG Tools

Local Markdown RAG system for [Claude Code](https://claude.ai/code). Index your local Markdown files into a vector database and search them from Claude or the built-in admin panel.

**No cloud. No Docker. No API keys.** Everything runs on your machine.

## What It Does

- Indexes `.md` files from local folders into an embedded Qdrant vector database
- Provides semantic search via CLI, admin panel, or Claude Code (MCP integration)
- Watches for file changes and auto-indexes in the background
- Runs as a local service with a web-based admin panel at `http://localhost:21420`

## Install from Source (Development)

### Prerequisites

- Python 3.12+
- Git

### Setup

```bash
git clone https://github.com/taqat-techno/rag.git
cd rag

# Create virtual environment
python -m venv .venv

# Activate it
# Windows (Git Bash):
source .venv/Scripts/activate
# Windows (CMD):
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Verify
rag version
rag doctor
```

### Run

```bash
# Start the service (background)
rag service start

# Open the admin panel
# http://localhost:21420

# Or run in foreground
rag service run
```

The admin panel opens automatically at [http://localhost:21420](http://localhost:21420). From there you can add projects, search, view the semantic map, and manage settings.

## Install from Release (Windows)

Download `RAGTools-Setup-{version}.exe` from [Releases](https://github.com/taqat-techno/rag/releases). Run the installer. RAG Tools starts automatically on login and opens the admin panel.

A portable `.zip` and macOS `tar.gz` are also available on the releases page.

## Add Your First Project

### From the Admin Panel

1. Open [http://localhost:21420/projects](http://localhost:21420/projects)
2. Click **+ New Project**
3. Enter a project ID, name, and folder path
4. Click **Add Project** — indexing starts automatically

### From the CLI

```bash
rag project add --name "My Docs" --path /path/to/markdown/folder
```

## CLI Reference

### Core Commands

```bash
rag search "how does auth work?"          # Search the knowledge base
rag search "schema" -p my_project -k 5    # Filter by project, limit results
rag status                                # Show index stats
rag projects                              # List projects with file/chunk counts
rag doctor                                # Health check
rag version                               # Show version
```

### Indexing

```bash
rag index .                               # Incremental index (skips unchanged)
rag index --full .                        # Force full re-index
rag index --project my_proj .             # Index one project only
rag rebuild                               # Drop everything, rebuild from scratch
```

### Service Management

```bash
rag service start                         # Start background service
rag service stop                          # Stop the service
rag service status                        # Check if running
rag service install                       # Register for auto-start on login
rag service uninstall                     # Remove auto-start
```

### Project Management

```bash
rag project list                          # List configured projects
rag project add --name "Docs" --path /p   # Add a project
rag project remove my_project             # Remove a project
rag project enable my_project             # Enable a disabled project
rag project disable my_project            # Disable (keeps data)
```

### File Watching

```bash
rag watch .                               # Watch and auto-index on changes
rag watch . --debounce 5000               # Custom debounce (ms)
```

### Ignore Rules

```bash
rag ignore list .                         # Show active ignore patterns
rag ignore check . path/to/file.md        # Check if a file is ignored
```

## Connect to Claude Code (MCP)

RAG Tools integrates with Claude Code via the [Model Context Protocol](https://modelcontextprotocol.io). Claude can search your indexed knowledge base automatically.

### Setup

Add this to your `.mcp.json` (project-level or `~/.claude/.mcp.json` for global):

```json
{
  "mcpServers": {
    "ragtools": {
      "command": "rag-mcp",
      "args": []
    }
  }
}
```

If using the installed `.exe` version, the admin panel shows the exact config at **Settings > Connect to Claude**.

### How It Works

- When the service is running, the MCP server connects via HTTP proxy (instant startup)
- When the service is not running, it falls back to direct mode (loads the model, ~5-10s startup)
- Claude automatically uses `search_knowledge_base` when you ask project-specific questions

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `search_knowledge_base` | Search indexed content with optional project filter |
| `list_projects` | List available project IDs |
| `index_status` | Check if the knowledge base is ready |

## Admin Panel

The web admin panel at `http://localhost:21420` provides:

| Page | Purpose |
|------|---------|
| **Dashboard** | Overview with stats, project table, activity log, quick search |
| **Map** | 2D/3D semantic visualization of your indexed content |
| **Search** | Full search with project filtering and confidence scores |
| **Projects** | Add, edit, toggle, remove projects |
| **Settings** | Indexing params, retrieval params, service config, MCP config |

## Configuration

Settings can be configured via the admin panel, environment variables, or `ragtools.toml`.

### Config File

The config file location depends on the mode:

| Mode | Location |
|------|----------|
| Dev (source) | `./ragtools.toml` (project root) |
| Installed (Windows) | `%LOCALAPPDATA%\RAGTools\config.toml` |
| Installed (macOS) | `~/Library/Application Support/RAGTools/config.toml` |

### Environment Variables

All settings can be overridden with `RAG_` prefixed environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `RAG_CHUNK_SIZE` | `400` | Target tokens per chunk |
| `RAG_CHUNK_OVERLAP` | `100` | Overlap tokens between chunks |
| `RAG_TOP_K` | `10` | Default search results |
| `RAG_SCORE_THRESHOLD` | `0.3` | Minimum similarity score |
| `RAG_QDRANT_PATH` | `data/qdrant` | Qdrant storage path |
| `RAG_STATE_DB` | `data/index_state.db` | SQLite state path |
| `RAG_CONFIG_PATH` | (auto) | Override config file path |
| `RAG_DATA_DIR` | (auto) | Override data directory |

### Ignore Rules

RAG Tools uses three layers of ignore rules (gitignore syntax):

1. **Built-in** — `.git/`, `.venv/`, `node_modules/`, `__pycache__/`, `dist/`, `build/`, `.cache/`, `.claude/`, `CLAUDE.md`, `*.pyc`
2. **Global config** — patterns in `ragtools.toml` under `[ignore]`
3. **Per-directory** — `.ragignore` files (gitignore syntax, supports `!` negation)

## Project Structure

```
src/ragtools/
  cli.py              # CLI commands (Typer)
  config.py           # Settings and config resolution
  models.py           # Data models (Chunk, SearchResult)
  ignore.py           # Three-layer ignore rules
  chunking/           # Heading-based Markdown chunking
  embedding/          # SentenceTransformer encoder with query cache
  indexing/            # File scanning, state tracking, batch indexing
  retrieval/           # Semantic search and result formatting
  integration/         # MCP server (proxy + direct mode)
  service/             # FastAPI service, admin panel, file watcher
    templates/         # Jinja2 HTML templates
    static/            # CSS, JS, images
  watcher/             # File change observer (watchfiles)
tests/                 # 253 tests
scripts/               # Build and evaluation scripts
data/                  # Local state (gitignored)
```

## Architecture

- **Qdrant** local mode — embedded vector database, no server process needed
- **all-MiniLM-L6-v2** — 384-dim sentence embeddings, runs on CPU
- **Heading-based chunking** — splits at `##`/`###`/`####` boundaries with paragraph/sentence fallback
- **Single collection** with project isolation via payload filtering
- **Incremental indexing** — SHA256 file hashing, only re-indexes changed files
- **Split-lock indexing** — search remains available during indexing (scan/chunk outside lock, encode/upsert in windowed batches)
- **FastAPI + htmx** — admin panel with server-rendered templates, no JS framework

## Running Tests

```bash
pip install -e ".[dev]"
pytest                            # Run all tests
pytest tests/test_chunking.py     # Single file
pytest -k "test_search"           # Filter by name
pytest --cov=ragtools             # With coverage
```

## Building from Source

```bash
pip install -e ".[build]"

# Build PyInstaller bundle
python scripts/build.py

# Build with Inno Setup installer (Windows, requires Inno Setup 6)
python scripts/build.py --installer

# Skip model download (faster, uses cached model)
python scripts/build.py --no-model
```

Output: `dist/rag/` (bundle) and `dist/RAGTools-Setup-{version}.exe` (installer).

## License

By [TaqaTechno](https://github.com/taqat-techno).
