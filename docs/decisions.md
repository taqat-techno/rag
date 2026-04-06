# Architecture Decision Record — Phase 0

Locked: 2026-04-06
Status: **Final** — these decisions govern Phase 1+ implementation.

RAG Tools is evolving from a CLI tool into a long-running local service. These 15 decisions
were made after a critical architecture review that identified the Qdrant single-process
constraint as the central design driver.

---

## Decision 1 — Single-Process Model

**Decision:** The service process is the sole owner of Qdrant. One process holds the
exclusive file lock for the Qdrant local-mode data directory. All other components operate
as clients of that process or as threads within it.

**Architecture:**
- FastAPI (uvicorn) runs in the main thread
- Watcher runs as a daemon thread inside the service process
- Encoder (`SentenceTransformer`) is loaded once at startup and shared
- A `QdrantOwner` singleton holds the Qdrant client + Encoder, protected by `threading.RLock`
- Indexing jobs run in a background thread, accessing Qdrant through `QdrantOwner`
- When the service is not running, CLI commands fall back to direct Qdrant access (current behavior — open, work, close)

**Default:** Service is not auto-started. Direct mode is the default until the user runs `rag service start`.

**Rationale:** Qdrant local mode (`QdrantClient(path=...)`) takes an exclusive file lock. The current codebase has 8 separate instantiation points (config.py, indexer.py x2, cli.py x2, mcp_server.py, observer.py). These work today only because users manually avoid running things concurrently. A single owner eliminates the lock contention class of bugs entirely.

**Tradeoff:** The service must be running for the watcher, MCP proxy mode, and admin panel to function. CLI commands still work in direct mode when the service is down, so the user is never fully blocked.

---

## Decision 2 — Config File Format & Location

**Decision:** TOML format. Config file is optional — the system operates on code defaults if no file exists.

**Resolution order** (first match wins):
1. `RAG_CONFIG_PATH` env var → use that path explicitly
2. `%LOCALAPPDATA%\RAGTools\config.toml` → installed/service mode (Windows)
3. `./ragtools.toml` → dev mode (relative to CWD)

**Priority chain** (highest to lowest):
1. Environment variables (`RAG_*`) — always override everything
2. Config file values — override code defaults
3. Code defaults in `Settings` class — unchanged from today

The `.env` file remains a dev convenience loaded by Pydantic Settings. It is not the production config mechanism.

**Config file schema version:** `version = 1` at the top level. Future migrations can check this.

**Writing config:** `tomli-w` library for writing changes back (e.g., from admin panel `PUT /api/config`). Reading uses `tomllib` (stdlib 3.11+) or `tomli` (fallback for 3.10).

**Default:** No config file exists initially. All defaults come from code. First write (admin panel, `rag config set`, etc.) creates the file at the detected location.

**Rationale:** TOML is the Python ecosystem standard (`pyproject.toml`). `tomllib` is stdlib in 3.11+. JSON has no comments. YAML requires PyYAML (C extension). The resolution chain ensures dev mode works unchanged while installed mode uses the standard Windows per-user app data location.

---

## Decision 3 — Ignore Rules Design

### 3a — File Name

**Decision:** `.ragignore`

**Rationale:** Familiar `.gitignore` convention. Specific enough to avoid conflicts with other tools. `.indexignore` is too generic. `.ragtools-ignore` is too long.

### 3b — Matching Library

**Decision:** `pathspec` library (MIT, ~30KB, zero dependencies).

**Rationale:** Implements the full gitignore spec including `!` negation patterns. Battle-tested — used by `black`, `isort`, and many other tools. No reason to write a custom matcher when this exists.

### 3c — Rule Layers

Three layers, all additive (if ANY layer matches, the file is ignored):

| Priority | Layer | Source | Mutable by user |
|----------|-------|--------|-----------------|
| 1 (most specific) | Per-directory `.ragignore` | `.ragignore` files on disk | Yes — edit the file |
| 2 | Global config | `config.toml` `[ignore].patterns` | Yes — config file or admin UI |
| 3 (least specific) | Built-in defaults | Hardcoded in `ignore.py` | No |

A `.ragignore` `!` negation pattern can un-ignore something matched by a lower-priority layer. This matches how `.gitignore` layering works.

### 3d — Built-in Defaults

Exact superset of current `SKIP_DIRS` (from `scanner.py:22-26`), plus additional common patterns:

```
# Directories (current SKIP_DIRS — preserved exactly)
.git/
.hg/
.svn/
.venv/
venv/
__pycache__/
node_modules/
site-packages/
.tox/
.mypy_cache/
.pytest_cache/
.hypothesis/
dist/
build/
*.egg-info/
.stversions/

# Additional directories
.cache/

# File patterns (new)
*.pyc
*.pyo
```

### 3e — New Module

`src/ragtools/ignore.py` — `IgnoreRules` class:
- Loads built-in defaults + global config patterns + `.ragignore` files
- `is_ignored(file_path: Path, content_root: Path) -> bool`
- `get_reason(file_path: Path, content_root: Path) -> str | None` (for diagnostics)
- Caches compiled `pathspec.PathSpec` per directory

### 3f — Integration Points

- `scanner.py` — `discover_markdown_files()` receives an `IgnoreRules` instance. Checks each file before adding to results. Replaces `SKIP_DIRS` constant entirely.
- `observer.py` — `_md_filter()` uses the same `IgnoreRules` instance (constructed once in `run_watch()`). Also watches for `.ragignore` file changes to trigger reload.
- `indexer.py` — does NOT check ignore rules. Scanner already filtered. Single responsibility principle.
- Rebuild — uses the same scanner, so ignore rules apply automatically.
- Manual single-file index — should respect ignore rules (warn if the file is ignored).

### 3g — Config Representation

```toml
[ignore]
patterns = [
    "drafts/",
    "*.tmp",
    "ARCHIVE/",
]
use_ragignore_files = true
```

Defaults: `patterns = []`, `use_ragignore_files = true`.

---

## Decision 4 — Service Port & Binding

**Decision:** Default port `21420`. Bind to `127.0.0.1` only, never `0.0.0.0`.

**Configurable:** Port is configurable via `[service].port` in config or `RAG_SERVICE_PORT` env var. Bind address is NOT configurable — exposing to the network is an explicit non-goal.

**Default:** `127.0.0.1:21420`

**Rationale:** Port 21420 is above 1024 (no elevation needed) and unlikely to conflict with common dev tools. Localhost-only binding means no Windows Firewall prompts, no accidental network exposure.

---

## Decision 5 — Localhost Auth

**Decision:** No authentication. No tokens, no keys, no headers.

**Default:** Unauthenticated.

**Rationale:** The service binds to `127.0.0.1` only (Decision 4). Any process that can reach it is already running as the local user. Adding auth creates setup friction (how does MCP get the token? how does CLI get it?) with zero security benefit. This matches Docker Desktop, VS Code language servers, Jupyter on localhost, and Syncthing.

**Flexibility:** If remote access is ever added (binding to `0.0.0.0`), auth becomes mandatory. But that is an explicit non-goal for this product.

---

## Decision 6 — Frontend Framework

**Decision:** htmx + Jinja2 templates, served directly by FastAPI. No JavaScript build step.

**CSS:** Classless CSS framework — Pico CSS (~10KB) or Simple.css (~5KB). No Tailwind, no custom CSS build.

**Template location:** `src/ragtools/service/templates/`
**Static assets:** `src/ragtools/service/static/`

**Default:** Server-rendered HTML with htmx for dynamic updates.

**Rationale:** The admin panel is a status dashboard and config editor. htmx gives smooth interactivity (partial page updates, polling, toggle switches) without a JS toolchain. Jinja2 is already a transitive FastAPI dependency. The target user installs via `pip` or `winget` — requiring Node.js is unacceptable. Zero build step means templates work in dev mode and packaged mode identically.

**Tradeoff:** Complex future UI features (drag-and-drop, real-time graphs) would be harder. Acceptable for a config-and-status dashboard.

---

## Decision 7 — MCP Proxy Strategy

**Decision:** MCP server probes `http://127.0.0.1:{PORT}/health` once at startup (2-second timeout). Result determines the mode for the entire session:

- **Probe succeeds (200):** Proxy mode. All tool calls forward to the service via `httpx.Client` (synchronous). MCP does NOT load the encoder or open Qdrant. Startup is near-instant (~100ms).
- **Probe fails:** Direct mode. Current behavior — load encoder (5-10s), open Qdrant, hold both for the session.

**Transport:** stdio (unchanged). Claude CLI launches `rag-mcp` as a subprocess. The proxy/direct decision is internal.

**Mid-session behavior:** Mode does not change. If the service goes down during a proxy-mode session, tool calls return clear error messages (`"Service unavailable. Restart with rag service start."`) — they do NOT attempt to switch to direct mode, because acquiring Qdrant's exclusive lock while the service might still hold it is unsafe.

**Timeouts:** 5 seconds for search/status/projects. 120 seconds for indexing operations.

**Rationale:** Near-instant MCP startup dramatically improves Claude CLI experience. The fallback preserves current functionality when the user hasn't adopted the service yet.

---

## Decision 8 — Service Lifecycle on Windows

**Start:** `rag service start` launches a detached background process:
- Uses `subprocess.Popen` with `CREATE_NO_WINDOW` (0x08000000) and `DETACHED_PROCESS` (0x00000008) creation flags
- Runs the internal command `rag service run` (foreground, starts uvicorn)
- stdout/stderr redirected to log file

**Internal command:** `rag service run` is not user-facing. It starts uvicorn in the foreground. Used by: `rag service start` (via subprocess), Task Scheduler (Phase 5), and debugging.

**PID file:** `{data_dir}/service.pid`, written after successful startup (encoder loaded, Qdrant open, uvicorn listening). Deleted on clean exit. Stale PID files detected by checking if the process is alive.

**Stop:** `rag service stop` attempts shutdown in order:
1. `POST http://127.0.0.1:{PORT}/api/shutdown` — graceful shutdown. Service sets a shutdown event, waits up to 30 seconds for in-flight indexing to finish, then exits.
2. If API unreachable: read PID file, terminate process via `ctypes.windll.kernel32.TerminateProcess` (Windows) or `os.kill` (Unix).
3. Delete PID file.

**Status:** `rag service status` probes `/health` first. Falls back to PID file check if unreachable.

**Rationale:** Windows has no `fork()` and no reliable POSIX signals. `CREATE_NO_WINDOW` + `DETACHED_PROCESS` is the standard Python-on-Windows background process pattern. The HTTP shutdown + PID kill fallback handles all failure modes.

---

## Decision 9 — Logging Strategy

**Service mode:**
- Python `logging` with `RotatingFileHandler`
- Path: `{data_dir}/logs/service.log`
- Rotation: 10 MB per file, keep 3 backups
- Format: `%(asctime)s %(levelname)-8s %(name)s %(message)s` (human-readable)
- Named loggers: `ragtools.service`, `ragtools.indexing`, `ragtools.watcher`, `ragtools.mcp`

**CLI mode (no service):**
- Log to stderr via `rich.console.Console` (unchanged from current behavior)
- No file logging

**Default level:** `INFO`. Configurable via `[logging].level` in config or `RAG_LOG_LEVEL` env var.

**Rationale:** Rotating file handler prevents unbounded growth (critical for a background service). Human-readable format because the primary consumer is a developer reading logs. Separate logger names allow per-component filtering without architecture changes.

---

## Decision 10 — Data Directory Layout

**Dev mode** (current behavior, backwards-compatible):
```
./data/
  qdrant/              — Qdrant local storage
  index_state.db       — SQLite state
  logs/                — (only if service started from dev)
    service.log
  service.pid
```

**Installed/service mode:**
```
%LOCALAPPDATA%\RAGTools\
  config.toml          — persistent config
  data/
    qdrant/
    index_state.db
  logs/
    service.log
  service.pid
```

**Detection logic** (evaluated in order):
1. If `RAG_DATA_DIR` env var is set → use that directory (explicit override)
2. If `%LOCALAPPDATA%\RAGTools\config.toml` exists → use `%LOCALAPPDATA%\RAGTools\` (installed mode)
3. Otherwise → use `./data/` relative to CWD (dev mode)

The `Settings` class gains a `data_dir` property that encapsulates this logic. All path fields (`qdrant_path`, `state_db`) become relative to `data_dir` by default.

**Default:** Dev mode (`./data/`), matching current behavior exactly.

**Rationale:** Dev mode preserves full backwards compatibility. Installed mode uses the standard Windows per-user data location. `rag doctor` reports which mode is active and why.

---

## Decision 11 — Startup Strategy Direction

**Phase 0-4:** No auto-start. User runs `rag service start` manually.

**Phase 5:** Task Scheduler via `schtasks.exe`.
- Task name: `RAGTools Service`
- Trigger: at user logon
- Delay: configurable (default 30 seconds)
- CLI commands: `rag service install` / `rag service uninstall`

**Not using:**
- **Startup Folder** — shows console flash, no retry, no delay, feels unprofessional
- **Windows Service** — requires `pywin32` or `nssm`, admin elevation, service account decisions. Overkill for a single-user local tool. May revisit post-Phase 7 if enterprise demand exists.

**Rationale:** Task Scheduler gives invisible startup, configurable delay, retry on failure, and scriptability via `schtasks.exe`. No admin privileges needed for user-logon tasks.

---

## Decision 12 — Encoder Thread Safety

**Decision:** `threading.RLock` in the `QdrantOwner` singleton serializes all encode + search/index operations.

Single encoder instance, single lock. The lock protects both the encoder and the Qdrant client — search (encode query → search Qdrant) and indexing (encode batch → upsert Qdrant) both acquire the lock for their full operation.

**CLI mode (no service):** No threading, no lock needed. Unchanged from current behavior.

**Rationale:** SentenceTransformer is not thread-safe. PyTorch tensor operations can corrupt under concurrent access. A per-thread encoder wastes ~100MB RAM per thread. A lock is the simplest correct solution. Throughput loss from serialization is negligible for a single-user tool where search is ~50ms.

---

## Decision 13 — Dependencies to Add

### Phase 1 (ignore rules + config)
| Package | Version | Purpose |
|---------|---------|---------|
| `pathspec` | `>=0.12.0` | gitignore-style pattern matching |
| `tomli` | `>=2.0.0; python_version < '3.11'` | TOML reading (3.10 fallback) |
| `tomli-w` | `>=1.0.0` | TOML writing |

### Phase 2 (service layer)
| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | `>=0.111.0` | HTTP service framework |
| `uvicorn[standard]` | `>=0.30.0` | ASGI server |
| `httpx` | `>=0.27.0` | HTTP client (CLI dual-mode, MCP proxy) |

### Phase 4 (admin panel)
| Package | Version | Purpose |
|---------|---------|---------|
| `jinja2` | `>=3.1.0` | Templates (transitive from FastAPI, pin explicitly) |

**Not adding:** `pywin32` (unnecessary), `aiofiles` (unnecessary), `python-multipart` (defer until needed).

---

## Decision 14 — CLI Dual-Mode Design

**Detection:** Each service-aware CLI command calls `_probe_service()` which attempts `httpx.get("http://127.0.0.1:{port}/health", timeout=1.0)`. Port read from config with default 21420.

- Response 200 → route command via HTTP to service
- Connection error → fall back to direct Qdrant access (current behavior)

**Transparency:** No mode flags needed. `rag search "query"` works whether the service is running or not. The user does not need to know or care which mode is active.

**Command routing:**

| Command | Service mode | Direct mode (fallback) |
|---------|-------------|----------------------|
| `rag search` | `GET /api/search` | Direct Qdrant |
| `rag index` | `POST /api/index` | Direct Qdrant |
| `rag status` | `GET /api/status` | Direct SQLite + Qdrant |
| `rag projects` | `GET /api/projects` | Direct SQLite |
| `rag rebuild` | `POST /api/rebuild` | Direct Qdrant |
| `rag doctor` | `GET /health` + local checks | Current behavior |
| `rag watch` | `POST /api/watcher/start` | Current standalone watcher |
| `rag serve` | Error: "use `rag service start`" | Current MCP stdio launch |
| `rag service *` | Always direct (manage the service itself) | N/A |

**Rationale:** Transparent dual-mode means the user's workflow doesn't change. The 1-second health probe adds negligible latency to commands that already take 5-10 seconds (encoder loading) in direct mode.

---

## Decision 15 — Watcher Unavailable Paths

**Decision:**
- Missing path at watcher start → log warning, skip that path, continue watching others
- Path goes offline during watching → catch error, log warning, keep watching remaining paths, retry the failed path every 60 seconds
- Path comes back → resume watching, log info

**Service mode:** Watcher thread reports unavailable paths via `/api/watcher/status` so the admin panel can display them.

**Default:** Skip with warning. Retry every 60 seconds. Do not crash.

**Rationale:** Content roots may be on network shares or removable drives. Crashing the entire service because one path is temporarily unavailable is unacceptable.

**Tradeoff:** A permanently unavailable path generates a warning every 60 seconds in the log. The user can remove it via config or admin panel.

---

## Summary Table

| # | Decision | Default | Locked |
|---|----------|---------|--------|
| 1 | Single-process model | Service owns Qdrant exclusively | Yes |
| 2 | Config format & location | TOML, `%LOCALAPPDATA%\RAGTools\config.toml` or `./ragtools.toml` | Yes |
| 3 | Ignore rules | `.ragignore`, `pathspec`, 3-layer precedence | Yes |
| 4 | Service port | `127.0.0.1:21420` | Yes |
| 5 | Localhost auth | None | Yes |
| 6 | Frontend | htmx + Jinja2, no JS build | Yes |
| 7 | MCP proxy | Probe at startup, proxy or fallback | Yes |
| 8 | Service lifecycle | `CREATE_NO_WINDOW`, PID file, HTTP shutdown | Yes |
| 9 | Logging | `RotatingFileHandler`, 10MB, 3 backups | Yes |
| 10 | Data directory | Dev: `./data/`, Installed: `%LOCALAPPDATA%\RAGTools\` | Yes |
| 11 | Startup strategy | Task Scheduler (Phase 5) | Yes |
| 12 | Encoder thread safety | `threading.RLock` in `QdrantOwner` | Yes |
| 13 | Dependencies | `pathspec`, `tomli`, `tomli-w`, later `fastapi`, `uvicorn`, `httpx` | Yes |
| 14 | CLI dual-mode | Transparent HTTP/direct based on health probe | Yes |
| 15 | Watcher unavailable paths | Skip, warn, retry 60s | Yes |
