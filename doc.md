# RAG Tools — Operational Knowledge Base

> **Purpose:** This file is the canonical support knowledge base for the RAG Tools local system. It is written as an operational reference for installing, configuring, running, validating, troubleshooting, and repairing the tool. It is grounded in the current state of the repository at `C:/MY-WorkSpace/rag`.
>
> This is NOT a plugin-development guide. It documents the RAG Tools product itself so that a future Claude plugin can give accurate, operational answers about installation, setup, configuration, runtime, failures, and recovery.

---

## Table of Contents

1. [RAG Tools Overview in This Project](#1-rag-tools-overview-in-this-project)
2. [Current RAG-Related Components in the Repository](#2-current-rag-related-components-in-the-repository)
3. [Installation Sources and Distribution](#3-installation-sources-and-distribution)
4. [Supported Platforms and OS Notes](#4-supported-platforms-and-os-notes)
5. [Prerequisites and Dependencies](#5-prerequisites-and-dependencies)
6. [Installation Instructions](#6-installation-instructions)
7. [Post-Install Verification](#7-post-install-verification)
8. [Directory Structure and Important File Locations](#8-directory-structure-and-important-file-locations)
9. [Configuration Files and Their Purpose](#9-configuration-files-and-their-purpose)
10. [MCP Configuration for Claude Code](#10-mcp-configuration-for-claude-code)
11. [Runtime, Launch, and Execution Flows](#11-runtime-launch-and-execution-flows)
12. [Supporting Scripts, Hooks, Skills, and Config Helpers](#12-supporting-scripts-hooks-skills-and-config-helpers)
13. [Logs, Diagnostics, and Health Checks](#13-logs-diagnostics-and-health-checks)
14. [Common Errors and Known Failure Modes](#14-common-errors-and-known-failure-modes)
15. [Troubleshooting and Repair Playbooks](#15-troubleshooting-and-repair-playbooks)
16. [Safe Reset, Reinstall, and Recovery](#16-safe-reset-reinstall-and-recovery)
17. [Versioning and Compatibility Notes](#17-versioning-and-compatibility-notes)
18. [Operational Risks, Constraints, and Assumptions](#18-operational-risks-constraints-and-assumptions)
19. [Gaps, Unknowns, and Items Needing Confirmation](#19-gaps-unknowns-and-items-needing-confirmation)
20. [Source Files to Recheck When Updating This Doc](#20-source-files-to-recheck-when-updating-this-doc)
21. [Quick Support Checklist](#21-quick-support-checklist)

---

## 1. RAG Tools Overview in This Project

RAG Tools (package name `ragtools`, current version **2.4.2**) is a local-first Retrieval-Augmented Generation system that indexes local Markdown files into an embedded Qdrant vector database and exposes them for:

- **CLI search** via the `rag` command
- **Claude Code** integration via a Model Context Protocol (MCP) server
- **Web admin panel** hosted locally on `http://127.0.0.1:21420`
- **File watcher** that auto-indexes on change

**Architectural principles (from `docs/decisions.md`):**

- **Single-process model** — one service process is the sole owner of the Qdrant data directory. Qdrant local mode takes an exclusive file lock, so concurrent writers are not possible.
- **No cloud, no Docker, no API keys** — everything runs on the user's machine.
- **No external vector server** — Qdrant runs in embedded mode: `QdrantClient(path="...")`.
- **Single collection** called `markdown_kb` with per-project payload filtering (project isolation via `project_id` field).
- **Embedding model** is `all-MiniLM-L6-v2` (384 dimensions, SentenceTransformers), forced to CPU device for cross-platform consistency (`src/ragtools/embedding/encoder.py`).

**Vendor:** TaqaTechno. Repository: `https://github.com/taqat-techno/rag`.

---

## 2. Current RAG-Related Components in the Repository

The repository at `C:/MY-WorkSpace/rag` is the product itself, not a plugin for it. Everything is RAG-related. Key subsystems:

| Area | Location | Purpose |
|------|----------|---------|
| CLI entry point | `src/ragtools/cli.py` | Typer-based `rag` command with subcommands |
| Config | `src/ragtools/config.py` | Pydantic Settings, path resolution, platform detection |
| Data models | `src/ragtools/models.py` | Chunk, SearchResult, etc. |
| Ignore rules | `src/ragtools/ignore.py` | Three-layer ignore (built-in + global + `.ragignore`) |
| Chunking | `src/ragtools/chunking/markdown.py` | Heading-based Markdown chunking with paragraph/sentence fallback |
| Embedding | `src/ragtools/embedding/encoder.py` | SentenceTransformer wrapper with LRU query cache |
| Indexing | `src/ragtools/indexing/{indexer,scanner,state}.py` | File scanning, SQLite state tracking, Qdrant upsert |
| Retrieval | `src/ragtools/retrieval/{searcher,formatter}.py` | Vector search + result formatting (full / compact / brief) |
| Service (FastAPI) | `src/ragtools/service/` | Admin panel, HTTP API, watcher host, owner pattern |
| MCP integration | `src/ragtools/integration/mcp_server.py` | Claude Code MCP server, proxy + direct modes |
| Watcher | `src/ragtools/watcher/observer.py` + `service/watcher_thread.py` | `watchfiles`-based auto-indexing |
| Templates | `src/ragtools/service/templates/` | Jinja2 admin panel (base, dashboard, projects, search, map, config) |
| Static assets | `src/ragtools/service/static/` | htmx, ECharts, CSS, logo, favicon |
| Tests | `tests/` | 253 pytest tests across all subsystems |
| Installer | `installer.iss`, `rag.spec`, `scripts/build.py`, `scripts/launch.vbs` | PyInstaller + Inno Setup Windows installer flow |
| CI/CD | `.github/workflows/release.yml`, `.github/workflows/test.yml` | Parallel Windows + macOS build pipeline |
| Docs | `README.md`, `docs/`, `RELEASING.md`, `CLAUDE.md` | User docs, decisions, workflows, release checklist |

---

## 3. Installation Sources and Distribution

RAG Tools ships from a single source: the **GitHub Releases page** at `https://github.com/taqat-techno/rag/releases`.

The CI/CD pipeline (`.github/workflows/release.yml`) builds three artifacts in parallel on tag push:

| Artifact | Platform | Type | Approximate Size |
|----------|----------|------|------------------|
| `RAGTools-Setup-{version}.exe` | Windows | Inno Setup installer (recommended) | ~488 MB |
| `RAGTools-{version}-portable.zip` | Windows | Portable bundle (no install) | ~564 MB |
| `RAGTools-{version}-macos-arm64.tar.gz` | macOS Apple Silicon | PyInstaller tarball | ~423 MB |

The large size is dominated by the bundled SentenceTransformer model (`all-MiniLM-L6-v2`, ~175 MB) and the full PyTorch runtime (~600 MB).

**Development install from source** (clone the repo) is also supported — see section 6.

There is no other distribution channel. WinGet manifests exist under `winget/` but are not yet published to the official WinGet repository (see `RELEASING.md` for the intended submission flow).

---

## 4. Supported Platforms and OS Notes

| OS | Support | Install artifact | Notes |
|----|---------|------------------|-------|
| **Windows 10/11 (x64)** | ✅ Full | `RAGTools-Setup-{version}.exe` or portable `.zip` | Primary development platform. Includes auto-start on login via VBScript in Windows Startup folder. |
| **macOS 14+ (Apple Silicon / arm64)** | ✅ Phase 1 | `RAGTools-{version}-macos-arm64.tar.gz` | Built on GitHub `macos-14` runners. No `.app` bundle or `.dmg` yet (Phase 1 is a raw PyInstaller tarball). No login auto-start yet. |
| **macOS Intel (x86_64)** | ❌ Not built | — | Not produced by CI. Could be added later by extending the matrix. |
| **Linux** | ❌ No release artifact | — | The codebase works in dev mode via `pip install -e ".[dev]"` but CI does not produce a Linux binary. |

**Key platform differences:**

- **Windows** uses `%LOCALAPPDATA%\RAGTools\` for data/config/logs; macOS uses `~/Library/Application Support/RAGTools/`. The resolver is in `src/ragtools/config.py` via `_get_app_dir()`.
- **Windows** supports login auto-start via a VBScript dropped into `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`. **macOS** currently logs "skipped (not Windows)" during auto-start registration — no LaunchAgent support is shipped yet.
- **PyTorch device selection** is forced to `device="cpu"` in `src/ragtools/embedding/encoder.py` to avoid MPS (Apple Metal) out-of-memory crashes on macOS runners and user machines. Do not remove this.

---

## 5. Prerequisites and Dependencies

### End-user prerequisites (installer path)

The `.exe` installer is self-contained. It bundles Python, PyTorch, SentenceTransformers, Qdrant client, FastAPI, and the embedding model. **No prerequisites** beyond Windows 10/11.

For the macOS `.tar.gz`: the user extracts the archive and runs `./rag` from the terminal. No extra prerequisites, but first launch may require bypassing Gatekeeper (`xattr -cr rag/`).

### Developer prerequisites (source install)

- **Python ≥ 3.10** (Python 3.12 is the tested CI version)
- **Git**
- **pip** (bundled with Python)

### Runtime Python dependencies (from `pyproject.toml`)

| Package | Role |
|---------|------|
| `qdrant-client>=1.12.0` | Embedded vector database |
| `sentence-transformers>=3.0.0` | Embedding model wrapper |
| `typer[all]>=0.12.0` | CLI framework |
| `pydantic>=2.0.0`, `pydantic-settings>=2.0.0` | Config and data models |
| `python-frontmatter>=1.0.0`, `markdown-it-py>=3.0.0` | Markdown parsing |
| `rich>=13.0.0` | CLI output |
| `mcp>=1.26.0` | Claude Code MCP protocol |
| `watchfiles>=1.0.0` | Rust-based file watcher |
| `pathspec>=0.12.0` | gitignore-style pattern matching |
| `tomli-w>=1.0.0` | TOML writing (reading uses stdlib `tomllib` on ≥3.11) |
| `fastapi>=0.111.0`, `uvicorn[standard]>=0.30.0` | Admin panel service |
| `httpx>=0.27.0` | Service proxy client |
| `jinja2>=3.1.0` | Template rendering |

### Build dependencies

- `pyinstaller>=6.0.0` (via `pip install -e ".[build]"`)
- **Inno Setup 6+** (Windows installer compilation — installs user-level via `winget install JRSoftware.InnoSetup` to `%LOCALAPPDATA%\Programs\Inno Setup 6\`)
- On macOS: `tar` (for creating the release tarball). No `create-dmg` yet.

### Test dependencies

`pip install -e ".[dev]"` installs `pytest`, `pytest-cov`, `pytest-asyncio`.

---

## 6. Installation Instructions

### 6.1 Windows — installer (recommended for end users)

1. Download `RAGTools-Setup-{version}.exe` from [the releases page](https://github.com/taqat-techno/rag/releases).
2. Double-click. The installer is user-level (no admin required).
3. The installer:
   - Copies files to `%LOCALAPPDATA%\Programs\RAGTools\` (or a user-selected location — default is per-user install).
   - Adds the install directory to `HKCU\Environment\Path` if the "Add to PATH" checkbox is selected.
   - Creates the data directory structure under `%LOCALAPPDATA%\RAGTools\` (`data/`, `logs/`).
   - Installs a smart launcher VBScript (`scripts/launch.vbs`).
   - Registers the Windows Startup folder auto-start task if selected.
   - Optionally starts the service and opens the admin panel immediately (tasks `startnow` and `startup` default to checked).
4. On first run, the service takes ~5–10 seconds to load the embedding model, then the admin panel opens at `http://127.0.0.1:21420`.

### 6.2 Windows — portable zip

1. Download `RAGTools-{version}-portable.zip`.
2. Extract anywhere (e.g., `C:\Tools\RAGTools\`).
3. Open a terminal in the extracted directory and run `rag.exe version` to verify.
4. Start manually: `rag.exe service start` (or run `rag.exe service run` for foreground mode).

**Note:** The portable zip does not install the startup auto-registration or PATH entry. The user must add these manually if desired.

### 6.3 macOS — tarball

1. Download `RAGTools-{version}-macos-arm64.tar.gz` (requires Apple Silicon — M1 or later).
2. Extract: `tar -xzf RAGTools-{version}-macos-arm64.tar.gz`
3. Remove Gatekeeper quarantine: `xattr -cr rag/`
4. `cd rag && ./rag version` to verify.
5. Start: `./rag service start` or run in foreground with `./rag service run`.
6. Open browser to `http://127.0.0.1:21420`.

**macOS login auto-start is not currently implemented.** See section 18 (Risks) and `src/ragtools/service/startup.py` — the function returns `False` on non-Windows rather than registering a LaunchAgent.

### 6.4 Development install from source (all platforms)

```bash
git clone https://github.com/taqat-techno/rag.git
cd rag

# Create virtual environment
python -m venv .venv

# Activate:
#   Windows (Git Bash):   source .venv/Scripts/activate
#   Windows (CMD):        .venv\Scripts\activate
#   macOS / Linux:        source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Verify
rag version
rag doctor

# Run tests
pytest
```

For dev mode the config file is `./ragtools.toml` (CWD-relative) and data is stored in `./data/`.

### 6.5 Linux

No pre-built artifact. Use the developer install from source. The core code is cross-platform, but startup auto-registration is only implemented for Windows.

---

## 7. Post-Install Verification

Run this sequence after any install or upgrade:

```bash
# 1. Version check
rag version
# Expected: "ragtools v<X.Y.Z>"

# 2. Health check
rag doctor
# Expected: all components OK except possibly "Collection NOT FOUND"
# if the service is running and holds the Qdrant lock — this is expected.

# 3. Service should be running (installer starts it automatically)
curl http://127.0.0.1:21420/health
# Expected: {"status":"ready","collection":"markdown_kb"}

# 4. Admin panel smoke test
# Open http://127.0.0.1:21420 in a browser.
# Expected: Dashboard with "Add Your First Project" if no projects configured.
```

Validation checklist:

- [ ] `rag version` returns the expected version
- [ ] `rag doctor` reports all dependencies present
- [ ] `curl http://127.0.0.1:21420/health` returns status `ready`
- [ ] Admin panel opens in browser
- [ ] Service process is visible in Task Manager (Windows) or Activity Monitor (macOS)
- [ ] Data directory exists at the correct platform-specific path (see section 8)
- [ ] Service log shows no ERROR lines since startup
- [ ] After adding a project, indexing starts automatically

---

## 8. Directory Structure and Important File Locations

### 8.1 Installed layout

| Path (Windows) | Path (macOS) | Purpose |
|----------------|--------------|---------|
| `%LOCALAPPDATA%\Programs\RAGTools\rag.exe` | `{extract_dir}/rag` | Main executable (PyInstaller one-dir output) |
| `%LOCALAPPDATA%\Programs\RAGTools\_internal\` | `{extract_dir}/_internal/` | Bundled Python runtime + dependencies |
| `%LOCALAPPDATA%\Programs\RAGTools\model_cache\` | `{extract_dir}/model_cache/` | Pre-bundled embedding model (`all-MiniLM-L6-v2`) |
| `%LOCALAPPDATA%\Programs\RAGTools\launch.vbs` | — | Smart launcher (Windows only) |
| `%LOCALAPPDATA%\RAGTools\config.toml` | `~/Library/Application Support/RAGTools/config.toml` | User config file |
| `%LOCALAPPDATA%\RAGTools\data\qdrant\` | `~/Library/Application Support/RAGTools/data/qdrant/` | Qdrant local storage (vector DB) |
| `%LOCALAPPDATA%\RAGTools\data\index_state.db` | `~/Library/Application Support/RAGTools/data/index_state.db` | SQLite file-hash tracking |
| `%LOCALAPPDATA%\RAGTools\logs\service.log` | `~/Library/Application Support/RAGTools/logs/service.log` | Rotating service log (10 MB × 3 backups) |
| `%LOCALAPPDATA%\RAGTools\service.pid` | `~/Library/Application Support/RAGTools/service.pid` | Running service PID file |

**Data dir resolution** (source: `src/ragtools/config.py` → `get_data_dir()`):

1. `RAG_DATA_DIR` env var (if set)
2. Platform-specific default via `_get_app_dir()`
3. Fallback: `./data/` resolved from CWD (dev mode)

**Config resolution** (source: `src/ragtools/config.py` → `_find_config_path()`):

1. `RAG_CONFIG_PATH` env var (if set and file exists)
2. `<platform app dir>/config.toml`
3. `./ragtools.toml` (dev mode, CWD-relative)
4. `None` → falls back to code defaults

**Config WRITE path** (source: `get_config_write_path()`) — critical for avoiding the v2.4.1 restart bug:

- Always uses the platform app dir in packaged mode. Never CWD-relative.

### 8.2 Source/dev layout

```
src/ragtools/
  cli.py                 # CLI commands
  config.py              # Settings, path resolution
  models.py              # Data models
  ignore.py              # Three-layer ignore rules
  chunking/markdown.py   # Heading-based Markdown chunking
  embedding/encoder.py   # SentenceTransformer + LRU query cache
  indexing/
    indexer.py           # Full + incremental indexing
    scanner.py           # File discovery, nested-path scoping
    state.py             # SQLite hash tracking
  retrieval/
    searcher.py          # Semantic search + score thresholding
    formatter.py         # Full / compact / brief output
  integration/mcp_server.py   # Claude Code MCP server
  service/
    app.py               # FastAPI app setup, singleton accessors
    owner.py             # QdrantOwner (RLock) — sole Qdrant holder
    routes.py            # HTTP API routes
    pages.py             # Admin panel rendering + fragments
    run.py               # Service startup, lifespan, post-startup tasks
    startup.py           # Windows Startup folder VBScript registration
    process.py           # PID file, start/stop/kill cross-platform
    watcher_thread.py    # Watcher thread with auto-restart
    map_data.py          # Semantic map PCA projection
    activity.py          # In-memory activity log ring buffer
    templates/*.html     # Jinja2 templates
    static/              # CSS, JS, images
  watcher/observer.py    # Low-level watchfiles loop
tests/                   # 253 pytest tests
scripts/
  build.py               # Build orchestration (PyInstaller + model + verify)
  launch.vbs             # Windows smart launcher (copied into installer)
  verify_setup.py        # Environment/dependency verification
  eval_retrieval.py      # Retrieval quality evaluation harness
docs/                    # Decision records, workflows, bug notes
```

---

## 9. Configuration Files and Their Purpose

### 9.1 `config.toml` / `ragtools.toml`

TOML file with optional sections. Example structure:

```toml
version = 2

[[projects]]
id = "my_project"
name = "My Project"
path = "C:\\path\\to\\project\\folder"
enabled = true
ignore_patterns = []

[[projects]]
id = "another_project"
name = "Another Project"
path = "C:\\other\\path"
enabled = true
ignore_patterns = ["*.tmp", "scratch/"]

[startup]
open_browser = true
delay = 30

[ignore]
patterns = ["*.bak", "_private/"]
```

**Schema version:** `version = 2` is current. `version = 1` was the legacy single-`content_root` format and is auto-migrated on load (see `src/ragtools/config.py` → `migrate_v1_to_v2()`).

### 9.2 Environment variables (`RAG_*` prefix)

All settings can be overridden with environment variables. Full list from `src/ragtools/config.py`:

| Variable | Default | Purpose |
|----------|---------|---------|
| `RAG_CONFIG_PATH` | auto | Override config file location |
| `RAG_DATA_DIR` | auto | Override data directory |
| `RAG_QDRANT_PATH` | `data/qdrant` | Qdrant local storage path |
| `RAG_COLLECTION_NAME` | `markdown_kb` | Collection name (single, don't change) |
| `RAG_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Model name (don't change without rebuild) |
| `RAG_CHUNK_SIZE` | `400` | Target tokens per chunk |
| `RAG_CHUNK_OVERLAP` | `100` | Overlap tokens |
| `RAG_TOP_K` | `10` | Default search result count |
| `RAG_SCORE_THRESHOLD` | `0.3` | Minimum similarity score |
| `RAG_STATE_DB` | `data/index_state.db` | SQLite state path |
| `RAG_CONTENT_ROOT` | `.` | Legacy v1 content root |
| `RAG_SERVICE_HOST` | `127.0.0.1` | Bind address |
| `RAG_SERVICE_PORT` | `21420` | Bind port |
| `RAG_LOG_LEVEL` | `INFO` | Logger level |
| `RAG_STARTUP_DELAY` | `30` | Seconds before auto-start fires |
| `RAG_STARTUP_OPEN_BROWSER` | `true` | Open browser after startup |

### 9.3 `.ragignore` files

Per-directory files with gitignore syntax. Supports `!` negation. Parsed by `src/ragtools/ignore.py` via `pathspec`.

### 9.4 Built-in ignore rules (always active)

From `src/ragtools/ignore.py`:

```
.git/, .hg/, .svn/
__pycache__/, .venv/, venv/, site-packages/, .mypy_cache/, .pytest_cache/
*.pyc, *.pyo
dist/, build/, *.egg-info/
.cache/, .claude/, CLAUDE.md
node_modules/
```

---

## 10. MCP Configuration for Claude Code

RAG Tools exposes three MCP tools to Claude Code:

| Tool | Purpose |
|------|---------|
| `search_knowledge_base(query, project?, top_k?)` | Semantic search with optional project filter |
| `list_projects()` | Return configured project IDs |
| `index_status()` | Check if the knowledge base is ready |

### 10.1 Registration in `.mcp.json`

For dev mode (source install with `pip install -e .`):

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

For packaged install, the admin panel at **Settings → Connect to Claude** displays the exact config pointing to the installed `rag.exe`. This is computed dynamically in `routes.py` → `mcp_config()` using `sys.frozen` detection.

Valid locations for `.mcp.json`:

- **Project-level:** `<repo>/.mcp.json` (recommended for team-shared configs)
- **User-level:** `~/.claude/.mcp.json` (global)

### 10.2 Dual-mode MCP server

The MCP server (`src/ragtools/integration/mcp_server.py`) probes `http://127.0.0.1:{service_port}/health` at startup:

- **Proxy mode** — Service is running → MCP forwards all calls over HTTP. Instant startup, no model load.
- **Direct mode** — Service is not running → MCP loads the encoder itself and opens Qdrant directly. Takes 5–10 seconds to load the model. Qdrant client is opened/closed per request to release the file lock.

**Retry:** v2.4.2 added a 2-second retry before falling back to direct mode, in case the service is still starting up when Claude launches the MCP server.

### 10.3 Important MCP constraints

- **Never run `rag index` or `rag watch` (direct mode) while Claude Code is using the MCP server in direct mode.** Both take the Qdrant file lock — only one process can hold it at a time.
- **Preferred setup:** start the service (`rag service start`), then MCP runs in proxy mode and there is no lock contention.

---

## 11. Runtime, Launch, and Execution Flows

### 11.1 Service startup sequence

Sources: `src/ragtools/service/run.py`, `app.py`, `startup.py`

1. `main()` sets up rotating file logging (`%LOCALAPPDATA%\RAGTools\logs\service.log`, 10 MB × 3 backups).
2. PID file is written to `{data_dir}/service.pid`.
3. uvicorn starts FastAPI on `127.0.0.1:21420`.
4. FastAPI `lifespan` callback:
   - Creates `Settings()` (loads config file + env vars).
   - Validates configured project paths; logs warnings for missing paths.
   - Creates `QdrantOwner` — loads encoder (`SentenceTransformer("all-MiniLM-L6-v2", device="cpu")`) and opens Qdrant client.
   - Ensures the `markdown_kb` collection exists.
5. Post-startup thread (5-second `threading.Timer`):
   - Polls `/health` until ready (up to 30 seconds).
   - Starts the file watcher (auto-starts unconditionally if there are enabled projects).
   - Registers the Windows Startup folder task if not already installed (Windows only).
   - Runs startup sync: incremental index — **guarded**: skips if `settings.projects` is empty (prevents data-loss incident from v2.4.1).
   - Opens browser if launched from scheduler and `startup_open_browser=true`.
6. Service is ready. All HTTP traffic is handled by `routes.py`; all UI fragments by `pages.py`.

### 11.2 Search flow

```
User/Claude → rag search / MCP search_knowledge_base / Admin panel search
  → Searcher.search(query)
  → encoder.encode_query(query) [LRU cache]
  → client.query_points(collection="markdown_kb", query_vector, filter={project_id}, top_k=10, score_threshold=0.3)
  → formatter.format_context(results)  # full / compact / brief
  → Return with HIGH/MODERATE/LOW confidence labels
```

### 11.3 Incremental indexing flow (split-lock design from v2.4.0)

```
Phase 1 (OUTSIDE the RLock — pure file I/O):
  scanner.scan_configured_projects() → list of (project_id, file_path)
  For each file: hash_file(), check state.file_changed()
  If changed: chunk_markdown_file() → list of Chunk
  Accumulate pending: [(pid, rel_path, hash, chunks), ...]

Phase 2 (INSIDE the RLock, windowed batches of 30 files):
  For each batch:
    Acquire owner._lock
    Flatten all batch chunks → encoder.encode_batch(texts)
    For each file in batch:
      delete_file_points(old)
      chunks_to_points() + upsert_points()
      state.update()
    state.commit()  # one commit per batch
    Release lock
```

The lock release between batches lets queued search requests run, so search latency stays in the milliseconds even during a multi-minute full index.

### 11.4 CLI dual-mode

Every CLI command first probes `http://127.0.0.1:21420/health` with a 1-second timeout. If the service is up, the command forwards to it via HTTP. If not, the command opens Qdrant directly (fallback). This means most commands work even when the service is stopped, at the cost of a 5–10 second encoder load for commands that need embeddings.

---

## 12. Supporting Scripts, Hooks, Skills, and Config Helpers

### 12.1 Scripts (`scripts/`)

| Script | Purpose |
|--------|---------|
| `build.py` | Orchestrates PyInstaller build + model cache + verification. Flags: `--no-model`, `--installer` |
| `launch.vbs` | Smart Windows launcher: checks health → starts service if needed → opens admin panel. Bundled into the installer. |
| `verify_setup.py` | Dev-mode environment check (Python version, imports, etc.) |
| `eval_retrieval.py` | Retrieval quality benchmark (MRR, Recall@K) against a question set |

### 12.2 `.claude/skills/` (development-time skills)

| Skill | Purpose |
|-------|---------|
| `windows_app_actions/` | Windows app integration patterns (startup folder, VBScript, process management) |
| `macos_app_actions/` | macOS equivalent patterns (LaunchAgents, .app bundles) |
| `cross_platform_app_launch/` | Shared launch patterns across Windows/macOS |
| `macos_release/` | macOS release playbook (Phase 2 / Phase 3 signing) |

These are development aids, not runtime components.

### 12.3 `.claude/agents/`

| Agent | Purpose |
|-------|---------|
| `rag-builder.md` | Mechanical implementation tasks for the RAG project |
| `rag-investigator.md` | Research and technical verification for RAG decisions |
| `rag-log-monitor.md` | Automated log inspection during testing |

### 12.4 Key configuration files at repo root

| File | Purpose |
|------|---------|
| `pyproject.toml` | Python package metadata + dependencies |
| `ragtools.toml` | Dev-mode user config (projects, startup) |
| `installer.iss` | Inno Setup Windows installer script |
| `rag.spec` | PyInstaller spec file |
| `CLAUDE.md` | Development-time instructions for Claude Code when working on this repo |
| `.mcp.json` | (If present) MCP server config for this project |

---

## 13. Logs, Diagnostics, and Health Checks

### 13.1 Log locations

| Environment | Location |
|-------------|----------|
| Windows installed | `%LOCALAPPDATA%\RAGTools\logs\service.log` |
| macOS installed | `~/Library/Application Support/RAGTools/logs/service.log` |
| Dev mode | `./data/logs/service.log` |

**Rotation:** `logging.handlers.RotatingFileHandler`, 10 MB per file, 3 backups retained. Max ~40 MB total.

**Format:** `%(asctime)s %(levelname)-8s %(name)s %(message)s`

Log level is configurable via `RAG_LOG_LEVEL` or the `log_level` field in `config.toml`. Defaults to `INFO`.

### 13.2 In-memory activity log

Additionally, a ring buffer of 500 recent events is held in memory (`src/ragtools/service/activity.py`). Exposed via:

- `GET /api/activity` — JSON response
- Admin panel bottom drawer — polls every 15 seconds
- Dashboard "Recent Activity" card

**Not persistent** — lost on service restart.

### 13.3 Health endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Lightweight ready check — returns `{"status":"ready","collection":"markdown_kb"}` |
| `GET /api/status` | Full index stats (files, chunks, projects, last_indexed) |
| `GET /api/projects` | Configured projects with file/chunk counts |
| `GET /api/watcher/status` | Watcher running state and watched paths |
| `GET /api/config` | Current settings |
| `GET /api/mcp-config` | Dynamically-generated MCP config snippet |

### 13.4 Health check CLI

```bash
rag doctor
```

Returns a table with status of Python, all dependencies (qdrant-client, sentence-transformers, mcp, fastapi, etc.), service status, data directory, state DB, collection, and ignore rules.

---

## 14. Common Errors and Known Failure Modes

### 14.1 Confirmed known failure modes

| Symptom | Root cause | Fix / status |
|---------|-----------|--------------|
| **Projects disappear after PC restart** + `[Errno 13] Permission denied: 'ragtools.toml'` on add project | **v2.4.1 fixed**. VBScript launcher inherited CWD=`C:\Windows\System32`. Config written with a relative path landed in an unwritable directory, and startup sync deleted "orphaned" data. | Upgrade to ≥ v2.4.1. VBScript now sets `shell.CurrentDirectory`, `get_config_write_path()` always uses `%LOCALAPPDATA%`, and startup sync skips if projects list is empty. |
| **MPS backend out of memory** on macOS | **v2.4.2 fixed**. PyTorch auto-detected Apple Metal GPU; MPS pool exhausted on small test fixtures. | Encoder now forces `device="cpu"` in `SentenceTransformer(...)`. Do not revert. |
| **`rag doctor` → Collection NOT FOUND** | Expected when the service is running and holds the Qdrant lock. The CLI opens a separate Qdrant client which cannot access the locked directory. | Not a bug. Ignore when service is up. |
| **HuggingFace unauthenticated warning** on startup | Cosmetic. Model is bundled locally. | Ignore. Can set `HF_TOKEN` or `TRANSFORMERS_OFFLINE=1` to suppress. |
| **Watcher silent death** | Pre-v2.4 the watcher thread could throw and die without recovery. | **v2.4 fix:** auto-restart with exponential backoff (5 retries, 5s–80s). |
| **Search blocked during full index** | Pre-v2.4 a single RLock was held across the entire indexing loop. | **v2.4 fix:** split-lock indexing. Search remains responsive during indexing. |
| **Add project fails silently** | Duplicate `path` values were previously accepted. | **v2.4 fix:** duplicate path validation in `project_create()`. |
| **`rag doctor` / `rag rebuild` crash with `NameError: _get_ignore_rules`** | `_get_ignore_rules` helper was missing from `cli.py`. | **v2.4 fix:** helper re-added. If user sees this, they are on a broken dev branch. |

### 14.2 Startup / runtime exceptions to watch in logs

- `RuntimeError: Storage folder data/qdrant is already accessed by another instance of Qdrant client` — another process already holds the Qdrant lock. Check for zombie Python processes or a second `rag service` instance. See 15.2.
- `ERROR: Application startup failed. Exiting.` (uvicorn) — usually paired with the Qdrant lock error above, or a missing model cache in the bundle.
- `Failed to auto-register startup task (non-fatal)` — OK if the user chose to skip startup on install, or on macOS where it is not yet supported.
- `Startup sync skipped: no projects configured (check config path)` — **diagnostic hint.** Means config didn't load. Verify the config file path per section 8 and 9.

### 14.3 UI-visible errors

- **"Failed to add project: [Errno 13] Permission denied: 'ragtools.toml'"** — the v2.4.1 bug. Upgrade.
- **"Project not found"** flash when removing a project — project was deleted by another window/tab or the config was reloaded externally.
- **"Rebuilding knowledge base... this may take several minutes"** — not an error; full-page banner for Rebuild action.

---

## 15. Troubleshooting and Repair Playbooks

### 15.1 Service will not start

1. Check the log: `%LOCALAPPDATA%\RAGTools\logs\service.log` (Windows) or `~/Library/Application Support/RAGTools/logs/service.log` (macOS).
2. Grep for `ERROR` or `Traceback`.
3. If Qdrant lock error, see 15.2.
4. If model load error (missing `model_cache/`), reinstall — the bundle is corrupt.
5. Try running in the foreground: `rag service run`. This shows startup output directly in the terminal.
6. Try `rag doctor` — if Python or dependencies are broken, the issue is below the service layer.

### 15.2 Qdrant "already accessed by another instance"

1. Stop any running RAG service: `rag service stop`.
2. Check for zombie processes:
   - Windows: `tasklist | findstr rag.exe` then `taskkill /F /PID <pid>`
   - macOS/Linux: `ps aux | grep rag`, then `kill -9 <pid>`
3. Remove stale PID file if present: `%LOCALAPPDATA%\RAGTools\service.pid`
4. Remove the Qdrant lock file: `%LOCALAPPDATA%\RAGTools\data\qdrant\.lock`
5. Start the service again: `rag service start`

Note: Never delete the Qdrant data directory while the service is running — it holds the lock.

### 15.3 Add-project fails with permission denied

**Upgrade to ≥ v2.4.1 immediately.** This is the post-restart config path bug. Workaround on older versions: manually create `%LOCALAPPDATA%\RAGTools\config.toml` and add the project there while the service is stopped, then restart the service.

### 15.4 Service runs but projects are empty after restart

1. `curl http://127.0.0.1:21420/api/projects` — confirm the API also reports zero.
2. Check the log for `Startup sync skipped: no projects configured` — confirms the config didn't load.
3. Verify the config file exists at the platform-specific path (section 8).
4. Compare contents: does it have `[[projects]]` sections?
5. Check for stale CWD-relative files: look for a `ragtools.toml` in `C:\Windows\System32\`, the user home, or the install directory. Move it to `%LOCALAPPDATA%\RAGTools\config.toml`.
6. Restart the service: `rag service stop && rag service start`.
7. Verify: `curl http://127.0.0.1:21420/api/projects`.

### 15.5 Indexing seems slow or stuck

1. Check the activity log in the admin panel (bottom drawer). Watch for "Incremental: X indexed, Y skipped, Z deleted" or "Full index started/completed" messages.
2. Check CPU usage. The encoder is CPU-bound (by design — see section 4).
3. For very large projects, incremental index is fast (SHA256 hash check skips unchanged files). Full index is proportional to total chunks.
4. If the watcher is firing too often during edits, increase debounce: `rag watch . --debounce 5000`.

### 15.6 Admin panel won't load / port already in use

1. Is another service bound to `21420`? `netstat -ano | findstr 21420` (Windows) or `lsof -i :21420` (macOS).
2. Change the port: set `RAG_SERVICE_PORT=21421` or edit `config.toml` → `service_port`.
3. Note: "Restart required" badge in Settings — changes to `service_port` or `log_level` require a service restart to take effect.

### 15.7 Watcher is not running

1. `curl http://127.0.0.1:21420/api/watcher/status` — check `running` field.
2. If `false`, start it: `curl -X POST http://127.0.0.1:21420/api/watcher/start`.
3. Check log for watcher errors. If the watcher repeatedly crashes, the auto-restart kicks in with exponential backoff.
4. Verify project paths exist (the watcher skips nonexistent paths and logs a warning).

### 15.8 MCP server not connecting from Claude Code

1. Is the service running? `rag service status`.
2. Check `.mcp.json` — is the `command` correct? For installed mode it should be the full path to `rag.exe` with `serve` argument. The Settings page in the admin panel shows the exact config.
3. Claude Code logs: see `~/.claude/logs/` for MCP server startup failures.
4. Try direct launch: run `rag-mcp` or `rag serve` in a terminal. It should block waiting for stdio input — that confirms it launched correctly.
5. Verify stdio is clean (no print statements to stdout). The MCP protocol uses stdio and any stray output breaks it.

---

## 16. Safe Reset, Reinstall, and Recovery

### 16.1 Reset indexed data but keep config

```bash
rag rebuild
```

This drops all Qdrant data and the state DB, then re-indexes from configured projects.

### 16.2 Nuclear reset — delete everything

**⚠️ Destructive. Stop the service first.**

```bash
rag service stop
```

Then delete:

- Windows: `rmdir /S /Q %LOCALAPPDATA%\RAGTools\data` (keeps config.toml) or `rmdir /S /Q %LOCALAPPDATA%\RAGTools` (everything)
- macOS: `rm -rf ~/Library/Application\ Support/RAGTools/data` or `rm -rf ~/Library/Application\ Support/RAGTools`

Then restart: `rag service start`. A fresh data directory and empty config will be created.

### 16.3 Reinstall from scratch

1. Uninstall via Windows "Add or Remove Programs" (the installer has an uninstaller). During uninstall, it prompts whether to keep user data.
2. Download the latest `RAGTools-Setup-{version}.exe` from GitHub releases.
3. Run the installer. User data is preserved if you said "Yes" to keeping it.
4. Verify with the checklist in section 7.

### 16.4 Upgrade without losing data

The installer is designed for in-place upgrade. It:

1. Stops the running service.
2. Removes the startup task.
3. Overwrites files in the install directory.
4. Reinstalls the startup task.
5. Starts the service.

The data directory (`%LOCALAPPDATA%\RAGTools\data\`) and config are untouched.

### 16.5 Recovery from corrupted Qdrant storage

If the `data/qdrant/` directory is corrupt (rare — usually from an ungraceful kill during upsert):

1. Stop the service.
2. Backup: `move %LOCALAPPDATA%\RAGTools\data\qdrant %LOCALAPPDATA%\RAGTools\data\qdrant.bak`
3. Start the service. A fresh empty collection will be created.
4. `rag rebuild` to re-index everything.

State DB corruption is possible but uncommon — same recovery applies to `index_state.db`.

---

## 17. Versioning and Compatibility Notes

- **Current version:** 2.4.2 (from `src/ragtools/__init__.py`, `pyproject.toml`, `installer.iss` — these must stay in sync per `RELEASING.md`).
- **Scheme:** Semantic versioning. X.Y.Z where X=major (breaking), Y=minor (features), Z=patch (fixes).
- **Python:** ≥ 3.10, tested on 3.12.
- **Embedding model:** `all-MiniLM-L6-v2` (384 dims). Changing this requires a full rebuild — the collection dimensions are baked in.
- **Qdrant collection name:** `markdown_kb`. One collection, project isolation via payload.
- **Config schema version:** Currently 2. Version 1 is auto-migrated on load.
- **State DB schema:** Single table `file_state (file_path PK, project_id, file_hash, chunk_count, last_indexed)`. Index on `project_id` added in v2.4.0.
- **Breaking change history:**
  - v2.0.0 introduced the multi-project model (replaced `content_root`).
  - v2.4.1 changed the config write path resolution — critical fix, not a breaking change.
  - v2.4.2 added macOS platform support — no Windows breaking changes.

---

## 18. Operational Risks, Constraints, and Assumptions

### 18.1 Single-process constraint (hard)

Qdrant local mode takes an exclusive file lock. Running two processes against the same data directory is not possible. This is the central design constraint (`docs/decisions.md` → Decision 1).

**Implication:** Users must understand that `rag index` / `rag watch` / `rag service` / Claude Code MCP cannot all independently hold Qdrant. The service is designed to be the one owner.

### 18.2 macOS limitations (v2.4.2)

- No `.app` bundle — tarball only (Phase 1 of the macOS rollout).
- No `.dmg` — user must `tar -xzf` manually.
- No Gatekeeper signing — user must `xattr -cr rag/` on first use.
- No LaunchAgent auto-start — user must start the service manually each time.
- Intel Macs are not built.

These are documented roadmap items (see `.claude/skills/macos_release/SKILL.md`).

### 18.3 macOS MPS — must stay disabled

The encoder is explicitly forced to `device="cpu"` to prevent MPS (Apple Metal) memory exhaustion. Do not "optimize" this by letting PyTorch auto-select the device. It will break CI and real user installations on Apple Silicon.

### 18.4 Bundle size

~1 GB after extraction. ~488 MB Windows installer / ~423 MB macOS tarball compressed. This is dominated by PyTorch + the bundled model. Not reducible without removing the model (which would require a first-run download).

### 18.5 Windows Startup folder auto-start

Uses a VBScript in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\RAGTools.vbs`. This works without elevation but has limitations:

- If the user moves or deletes the install directory, the VBScript breaks until the service runs and re-registers itself.
- The VBScript CWD was a bug source — see v2.4.1 fix.
- `schtasks /sc onlogon` was abandoned because it requires elevation even with `/it`.

### 18.6 Syncthing / cloud-synced config directory

If the config file is in a Syncthing-managed or similar cloud-synced directory, another device's state can overwrite local changes. Symptom: projects reappear after removal. Mitigation: store `config.toml` outside the synced directory, or add it to `.stignore`.

### 18.7 MCP token consumption

The MCP output format is compact by default (sentence-boundary truncation, version-suffix deduplication) to reduce Claude's context consumption by ~60–70%. The compact mode is the default; full mode is only used for the admin panel search.

### 18.8 Re-ranking is not implemented

Retrieval is pure bi-encoder (no cross-encoder re-ranking). Score threshold is 0.3. Confidence labels: HIGH (≥0.7), MODERATE (0.5–0.7), LOW (<0.5). Re-ranking is listed as a post-MVP enhancement in the decision record.

---

## 19. Gaps, Unknowns, and Items Needing Confirmation

Marked clearly because the user asked for honesty about what is not verified:

1. **Linux packaged artifact** — The code is cross-platform but no CI build or installer exists. Dev-mode install works; packaged install is unverified.
2. **macOS login auto-start** — Not yet implemented. Confirmed unimplemented (see `src/ragtools/service/startup.py`: `_check_windows()` returns False on other platforms).
3. **WinGet submission** — `winget/` manifests exist but have not been submitted to the official WinGet repository. `RELEASING.md` describes the intended flow but it is not automated.
4. **Intel macOS support** — The `macos-14` runner only builds arm64. An x86_64 build would require adding `macos-13` or similar to the matrix. Not implemented.
5. **Signing / notarization** — Not implemented on either platform. Windows users may see SmartScreen warnings; macOS users must bypass Gatekeeper manually.
6. **Persistent activity log** — Currently in-memory only (500-entry ring buffer). Listed as a deferred improvement.
7. **Structured request logging in FastAPI** — No middleware yet. uvicorn logs are at WARNING level, suppressing HTTP access traces.
8. **`scripts/verify_setup.py` behavior** — Exists but I have not re-read it in detail; treat it as a general dev-mode sanity script. Recheck when updating this document.
9. **Exact behavior of `rag ignore check`** — Documented in CLI help but full edge-case semantics should be re-verified against `src/ragtools/cli.py` for the support doc.
10. **Behavior when a project's disk is unmounted** — The service validates paths at startup and logs a warning, but runtime behavior when a drive disappears during watcher operation is not explicitly documented.
11. **macOS data directory migration if user changes `~/Library/Application Support/RAGTools/` location** — Not implemented.
12. **Interaction with antivirus on Windows** — PyInstaller bundles sometimes trigger false positives. UPX is disabled in `rag.spec` specifically to reduce this. Real-world false-positive rate is not tracked.

---

## 20. Source Files to Recheck When Updating This Doc

When this doc is updated, re-verify these files first — they are the authoritative sources for the matching sections:

| Section | Source |
|---------|--------|
| Architecture and single-process model | `docs/decisions.md` (Phase 0 ADR) |
| Workflows and user flows | `docs/workflows.md` |
| CLI commands | `src/ragtools/cli.py` |
| Config paths and platform detection | `src/ragtools/config.py` — `_get_app_dir()`, `_find_config_path()`, `get_config_write_path()`, `get_data_dir()` |
| Service startup sequence | `src/ragtools/service/run.py`, `app.py`, `startup.py` |
| Indexing pipeline | `src/ragtools/service/owner.py` → `run_incremental_index()`, `run_full_index()` |
| MCP server | `src/ragtools/integration/mcp_server.py` |
| Watcher | `src/ragtools/service/watcher_thread.py`, `src/ragtools/watcher/observer.py` |
| Installer | `installer.iss`, `rag.spec`, `scripts/build.py`, `scripts/launch.vbs` |
| CI/CD | `.github/workflows/release.yml`, `.github/workflows/test.yml` |
| Release process | `RELEASING.md` |
| User-facing docs | `README.md` |
| Version strings | `src/ragtools/__init__.py`, `pyproject.toml`, `installer.iss` |
| Known bugs | `docs/bugs/bugs.md` |
| Backlog / roadmap | `docs/backlog-future-releases.md`, `tasks/roadmap_v2.md` |

---

## 21. Quick Support Checklist

Use this as the first-pass triage flow when a user reports a problem:

- [ ] **Version** — `rag version` — is the user on the latest?
- [ ] **Health** — `rag doctor` — which component is red?
- [ ] **Service status** — `rag service status` — is it running?
- [ ] **Port** — `curl http://127.0.0.1:21420/health` — does it respond?
- [ ] **Log** — `%LOCALAPPDATA%\RAGTools\logs\service.log` (Windows) or `~/Library/Application Support/RAGTools/logs/service.log` (macOS) — any `ERROR` or `Traceback`?
- [ ] **Config** — does `%LOCALAPPDATA%\RAGTools\config.toml` exist? Does it have `[[projects]]`?
- [ ] **Projects API** — `curl http://127.0.0.1:21420/api/projects` — does it show the configured projects?
- [ ] **Watcher** — `curl http://127.0.0.1:21420/api/watcher/status` — running?
- [ ] **Process** — `tasklist | findstr rag.exe` (Windows) or `ps aux | grep rag` (macOS) — is the process alive?
- [ ] **Qdrant lock** — if the error mentions "already accessed", see playbook 15.2.
- [ ] **Admin panel** — does `http://127.0.0.1:21420` load in a browser?
- [ ] **Disk space** — enough free space for Qdrant data and logs?
- [ ] **Antivirus** — is anti-virus quarantining `rag.exe`?

If the above does not isolate the issue, collect:

- Full log file (compressed)
- Version string
- OS name and version
- Output of `rag doctor`
- Contents of `config.toml` (redact sensitive project paths if needed)
- A description of the last successful state and the action that triggered the problem

---

**Document version:** 1.0 — initial version written against RAG Tools v2.4.2. Keep this document synchronized with the files listed in section 20.
