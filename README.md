# RAG Tools

Local Markdown RAG system for [Claude Code](https://claude.ai/code). Index your local Markdown files into a vector database and search them from Claude or the built-in admin panel.

**No cloud. No Docker. No API keys.** Everything runs on your machine.

## What It Does

- Indexes `.md` files from local folders into an embedded Qdrant vector database
- Provides semantic search via CLI, admin panel, or Claude Code (MCP integration)
- Watches for file changes and auto-indexes in the background
- Runs as a supervised local service with a web-based admin panel at `http://localhost:21420`
- Shows a **system-tray icon** (green/amber/red) so you can see service health at a glance
- Auto-restarts on crash via a built-in supervisor + a Windows Task Scheduler watchdog
- Surfaces crashes via **desktop toast notifications** so you find out immediately, not hours later
- **Auto-backs-up** the state DB before destructive operations (rebuild, project removal)
- **Per-tool MCP access control** — the agent only sees the tools you've granted it

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

# Optional extras (all safe to install, not auto-bundled)
pip install -e ".[notifications]"   # Windows desktop toasts (winotify)
pip install -e ".[tray]"            # System-tray icon (pystray + Pillow)
pip install -e ".[build]"           # PyInstaller, for producing release artifacts

# Verify
rag version
rag doctor
```

### Run

```bash
# Start the supervised service (auto-restarts on crash)
rag service start

# Open the admin panel
# http://localhost:21420

# Or run in foreground
rag service run
```

The admin panel opens automatically at [http://localhost:21420](http://localhost:21420). From there you can add projects, search, view the semantic map, manage settings, and grant MCP tools to the agent.

## Install from Release (Windows)

Download `RAGTools-Setup-{version}.exe` from [Releases](https://github.com/taqat-techno/rag/releases). Run the installer. RAG Tools starts automatically on login, opens the admin panel, and registers an auto-recovery watchdog task.

A portable `.zip` and macOS `tar.gz` are also available on the releases page.

## Add Your First Project

### From the Admin Panel

1. Open [http://localhost:21420/projects](http://localhost:21420/projects)
2. Click **+ New Project**
3. Enter a project ID, name, and folder path
4. Click **Add Project** — indexing starts automatically; you'll get a desktop toast when it finishes

### From the CLI

```bash
# Single project
rag project add --name "My Docs" --path /path/to/markdown/folder

# Many projects at once with a glob pattern
rag project add-from-glob "D:/Work/*/docs"            # preview + confirm
rag project add-from-glob "D:/Work/*/docs" --yes      # skip confirmation
rag project add-from-glob "D:/**" --exclude "*/archive/*" --dry-run   # scoping + preview
```

## CLI Reference

### Core Commands

```bash
rag search "how does auth work?"          # Search the knowledge base
rag search "schema" -p my_project -k 5    # Filter by project, limit results
rag status                                # Show index stats
rag projects                              # List projects with file/chunk counts
rag doctor                                # Health check (incl. login-startup + watchdog status)
rag version                               # Show version
```

### Indexing

```bash
rag index .                               # Incremental index (skips unchanged)
rag index --full .                        # Force full re-index
rag index --project my_proj .             # Index one project only
rag rebuild                               # Drop everything, rebuild from scratch (auto-backed-up)
```

### Service Management

```bash
rag service start                         # Start supervised service
rag service stop                          # Stop the service (fast escalation to force-kill)
rag service status                        # Check if running
rag service install                       # Register for auto-start on login
rag service uninstall                     # Remove auto-start

# Watchdog (Task Scheduler — restarts the service if the supervisor dies)
rag service watchdog install              # Register the 15-min watchdog task
rag service watchdog uninstall
rag service watchdog status
rag service watchdog check                # What the task invokes; exits 0 always
```

### Project Management

```bash
rag project list                          # List configured projects
rag project add --name "Docs" --path /p   # Add a project
rag project add-from-glob "D:/Work/*"     # Bulk-add from a glob pattern
rag project remove my_project             # Remove a project
rag project enable my_project             # Enable a disabled project
rag project disable my_project            # Disable (keeps data)
```

### Backups

```bash
rag backup list                           # Show snapshots
rag backup create --note "before refactor"  # Manual snapshot
rag backup restore <backup-id>            # Restore (takes pre-restore safety snapshot)
rag backup prune --keep 5                 # Trim old snapshots
```

Snapshots are taken automatically before `rag rebuild` and project removal.

### System Tray (optional extra)

```bash
pip install "ragtools[tray]"              # One-time extras install

rag tray                                  # Start the tray icon (blocks)
rag tray install                          # Register tray to auto-start on login (Windows only)
rag tray uninstall
rag tray status                           # Is it installed / currently running?
```

The tray icon shows the TaqaTechno logo with a small colored status badge: 🟢 ready, 🟡 starting, 🔴 down. Right-click for Open admin panel / Restart / Stop / View logs / Quit.

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

RAG Tools integrates with Claude Code via the [Model Context Protocol](https://modelcontextprotocol.io). Claude can search your indexed knowledge base and, if you grant it, diagnose the RAG service itself.

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

If using the installed `.exe` version, the admin panel shows the exact config at **Settings → Connect to Claude**.

### How It Works

- When the service is running, the MCP server connects via HTTP proxy (instant startup)
- When the service is not running, it falls back to direct mode (loads the model, ~5–10 s startup)
- Claude automatically uses `search_knowledge_base` when you ask project-specific questions

### MCP Tools

**Core tools — always available:**

| Tool | Description |
|------|-------------|
| `search_knowledge_base(query, project?, projects?, top_k?)` | Search indexed content. Optional `project` (single) or `projects` (list) filter. |
| `list_projects()` | Discover available project IDs |
| `index_status()` | Check if the knowledge base is ready |

**Optional tools — user-granted from the admin panel's "MCP Tool Access" card**, default OFF:

| Category | Tools | What they enable |
|---|---|---|
| **Diagnostics** | `service_status`, `recent_activity`, `tail_logs`, `crash_history`, `get_config`, `get_ignore_rules`, `get_paths`, `system_health`, `list_indexed_paths` | Agent can self-debug without human handholding |
| **Project reads** | `project_status`, `project_summary`, `list_project_files`, `get_project_ignore_rules`, `preview_ignore_effect` | Agent orients on a project before acting |
| **Project writes** (guarded) | `run_index`, `reindex_project`, `add_project_ignore_rule`, `remove_project_ignore_rule` | Agent maintains the index for a project it's working on |

Each optional tool has its own on/off checkbox. Disabled tools are not registered at all — invisible to the agent, zero token cost.

**Safety model:**
- All writes require proxy mode (service running)
- `reindex_project` requires `confirm_token == project_id` (defeats blind prompt-injected invocations)
- All agent writes are logged to the activity feed with `source="mcp"`
- Destructive ops (`add_project`, `remove_project`, `shutdown`, `backup restore`) are CLI-only — **never reachable from the agent**

## Admin Panel

The web admin panel at `http://localhost:21420` provides:

| Page | Purpose |
|------|---------|
| **Dashboard** | Overview with stats, project table, activity log, quick search |
| **Map** | 2D/3D semantic visualization of your indexed content |
| **Search** | Full search with project filtering and confidence scores |
| **Projects** | Add, edit, toggle, remove projects |
| **Settings** | Indexing params, retrieval params, notifications toggle + test button, MCP Tool Access checkboxes, service config, MCP config |

Crash markers appear as a dismissable red banner at the top of every page until reviewed. Service, supervisor, and watcher failures each get their own banner type.

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
| `RAG_DESKTOP_NOTIFICATIONS` | `true` | Desktop toasts on crash / completion |
| `RAG_BACKUP_KEEP` | `10` | Number of state-DB snapshots to retain |

### Ignore Rules

RAG Tools uses three layers of ignore rules (gitignore syntax):

1. **Built-in** — `.git/`, `.venv/`, `node_modules/`, `__pycache__/`, `dist/`, `build/`, `.cache/`, `.claude/`, `CLAUDE.md`, `*.pyc`
2. **Global config** — patterns in `ragtools.toml` under `[ignore]`
3. **Per-project / per-directory** — the project's `ignore_patterns` list plus `.ragignore` files (gitignore syntax, supports `!` negation)

## Project Structure

```
src/ragtools/
  cli.py              # CLI commands (Typer)
  config.py           # Settings and config resolution
  backup.py           # State-DB backup/restore
  tray.py             # System-tray icon
  tray_icons.py       # PIL icon generation
  project_glob.py     # Bulk project add-from-glob
  models.py           # Data models (Chunk, SearchResult)
  ignore.py           # Three-layer ignore rules
  chunking/           # Heading-based Markdown chunking
  embedding/          # SentenceTransformer encoder with query cache
  indexing/           # File scanning, state tracking, batch indexing
  retrieval/          # Semantic search and result formatting
  integration/        # MCP server with per-tool access control
  service/
    app.py            # FastAPI app + lifespan
    routes.py         # HTTP endpoints (search, index, config, crash, backup, etc.)
    pages.py          # Admin-panel page renderers
    owner.py          # QdrantOwner — single-threaded Qdrant gateway
    process.py        # service start/stop + PID lifecycle
    supervisor.py     # Respawn-on-crash parent
    watchdog.py       # Task Scheduler watchdog
    startup.py        # Windows login auto-start
    tray_startup.py   # Tray auto-start
    watcher_thread.py # File watcher (daemon thread)
    notify.py         # Desktop toast notifier
    crash_history.py  # Crash-marker banner
    logs.py           # Whitelisted log tailing
    templates/        # Jinja2 HTML templates
    static/           # CSS, JS, images
tests/                # 498 tests
scripts/              # Build, launch, eval
data/                 # Local state (gitignored)
```

## Architecture Highlights

- **Qdrant** local mode — embedded vector database, no server process needed
- **all-MiniLM-L6-v2** — 384-dim sentence embeddings, runs on CPU (`device="cpu"` forced to avoid MPS memory issues on Apple Silicon)
- **Heading-based chunking** — splits at `##`/`###`/`####` boundaries with paragraph/sentence fallback
- **Single collection** with project isolation via payload filtering
- **Incremental indexing** — SHA256 file hashing, only re-indexes changed files
- **Split-lock indexing** — search remains available during indexing (scan/chunk outside lock, encode/upsert in windowed batches of 30 files)
- **FastAPI + htmx** — admin panel with server-rendered templates, no JS framework
- **Supervisor + watchdog** — two-tier auto-recovery. Supervisor respawns the service on crash (exponential backoff, 5-crash budget). Task Scheduler watchdog runs every 15 min and starts the service if both the supervisor and the service are down.
- **Auto-backup** — snapshots the SQLite state DB (via SQLite's online-backup API, WAL-safe) before `rag rebuild` or project-remove. `rag backup restore` takes a pre-restore safety snapshot so the restore itself is reversible.

## Running Tests

```bash
pip install -e ".[dev]"
pytest                            # Run all tests (~60s)
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
