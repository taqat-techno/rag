# RAG Tools — Revised Product & Engineering Roadmap v2

Written after critical architecture review. Supersedes all prior phase plans.

---

<executive_summary>

RAG Tools is a local-first, Windows-first Markdown RAG system evolving from a CLI tool
into a long-running local service with a web admin panel, Windows startup integration,
and packaged distribution.

The core architectural correction from the review: **the service process must become the
single owner of Qdrant**. Every other component — CLI, MCP server, watcher, admin panel —
must operate as a client of that service or as a thread within it. The previous design
assumed multiple processes could safely share Qdrant local mode. They cannot.

The corrected build order:
1. Lock architecture decisions and build the ignore-rules foundation (Phase 0-1)
2. Build the service layer as the Qdrant owner with HTTP API (Phase 2)
3. Convert MCP to a thin proxy against the service (Phase 3)
4. Add the web admin panel served by the service (Phase 4)
5. Add Windows startup via Task Scheduler (Phase 5)
6. Package into an installer (Phase 6)
7. Distribute via winget and plan future maturity (Phase 7)

New first-class feature: **ignore rules** — configurable file/folder exclusion for
indexing and watching, with `.ragignore` file support, global config, and admin UI exposure.
This is integrated across Phases 0, 1, 2, and 4 as a real product feature.

</executive_summary>

---

<phase_0>

## Phase 0 — Architecture Correction & Foundation Decisions

### Goal
Lock every major technical decision before writing service code. Define the single-process
model, config strategy, ignore rules architecture, and directory layout. No code ships in
this phase — only design documents and decision records.

### Why This Phase Exists
The prior plan's biggest weakness was treating "add FastAPI" as a bolt-on. In reality, the
service layer requires rethinking process ownership, config persistence, CLI behavior, and
MCP integration. Making these decisions mid-implementation leads to rewrites. Phase 0 pays
the design debt upfront.

### Features Included
None — this is architecture and decision-making only.

### Technical Work

**1. Single-process model definition**
- Document: the service process is the sole Qdrant owner
- Watcher runs as a daemon thread inside the service, not a separate process
- Encoder (SentenceTransformer) is loaded once at service startup, protected by a threading.Lock
- FastAPI runs in the main thread via uvicorn
- Indexing jobs run in a background thread, coordinated via a simple job queue

**2. CLI dual-mode strategy**
- CLI commands probe `localhost:{PORT}/health` first
- If service responds: forward command via HTTP (e.g., `rag search` → `GET /api/search?q=...`)
- If service is down: fall back to direct Qdrant access (current behavior)
- Fallback mode acquires Qdrant, does the work, releases it
- New CLI subcommand group: `rag service start|stop|status`

**3. Config persistence strategy**
- Primary config file: `%LOCALAPPDATA%\RAGTools\config.toml`
- On Linux/macOS (if ever): `~/.config/ragtools/config.toml`
- Env vars (`RAG_*`) still override config file values (Pydantic Settings priority)
- `.env` file is dev-only convenience, not the production config mechanism
- Config file is TOML — Python 3.11+ has `tomllib` in stdlib, use `tomli-w` for writing
- First run creates config with defaults if file doesn't exist
- Config schema versioned with `version = 1` field for future migrations

**4. Data and log directory layout**

```
%LOCALAPPDATA%\RAGTools\
  config.toml           — persistent config (service port, ignore rules, startup prefs, paths)
  data\
    qdrant\             — Qdrant local storage
    index_state.db      — SQLite state tracker
  logs\
    service.log         — main service log (rotate at 10MB, keep 3)
    index.log           — indexing operations log
  service.pid           — PID file when service is running
```

Dev mode (no installer): data stays in `./data/` relative to project root, as today.
Service mode: uses `%LOCALAPPDATA%\RAGTools\` paths.
Detection: if `RAG_DATA_DIR` env var is set, use it. Otherwise check if running as installed
service (PID file location, or `--installed` flag). Default to `./data/` for backwards compat.

**5. Ignore rules architecture**

Layered ignore system with three levels:

| Level | Source | Scope | Mutable by user |
|-------|--------|-------|-----------------|
| Built-in defaults | Hardcoded in code | Global | No |
| Global config | `config.toml` `[ignore]` section | All projects | Yes (config file or admin UI) |
| Per-directory ignore file | `.ragignore` file in any directory | That directory and descendants | Yes (edit file directly) |

**Precedence (most specific wins):**
1. `.ragignore` in or above the file's directory → if matched, file is ignored
2. Global config ignore patterns → if matched, file is ignored
3. Built-in defaults → if matched, file is ignored
4. If nothing matches → file is included

**Built-in defaults** (replaces current hardcoded `SKIP_DIRS`):
```
# Directories always skipped
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
.cache/

# Files always skipped
*.pyc
*.pyo
```

**Matching strategy:** gitignore-style glob patterns using `pathspec` library (MIT, lightweight,
already implements gitignore semantics including `!` negation patterns). Not regex — users
already know gitignore syntax.

**Config file representation:**
```toml
[ignore]
# Additional patterns beyond built-in defaults
patterns = [
    "drafts/",
    "*.tmp",
    "ARCHIVE/",
    "*.bak",
]
use_ragignore_files = true    # Whether to parse .ragignore files on disk
```

**`.ragignore` file format:**
Same as `.gitignore` — one pattern per line, `#` for comments, `!` for negation.
Scoped to the directory it lives in and all subdirectories.

**Where ignore logic runs:**
- `scanner.py` — `discover_markdown_files()` checks ignore rules before yielding files
- `observer.py` — `_md_filter()` checks ignore rules before accepting a change event
- `indexer.py` — does NOT re-check (scanner already filtered). Single responsibility.
- Rebuild command — uses the same scanner, so ignore rules apply automatically
- Manual single-file index — should still respect ignore rules (warn if file is ignored)

**6. Service port and binding**
- Default port: 21420 (arbitrary high port, unlikely to conflict)
- Bind: `127.0.0.1` only — never `0.0.0.0`
- No auth on localhost — trusted local user assumption
- Port configurable in `config.toml` under `[service]`

**7. Logging strategy**
- Use Python `logging` module with `RotatingFileHandler`
- Service mode: log to `%LOCALAPPDATA%\RAGTools\logs\service.log`
- CLI mode: log to stderr (current behavior)
- Log levels: configurable in config, default INFO
- Structured enough to parse, but human-readable (not JSON)

### Dependencies
None — this is decision-making work.

### Risks / Hidden Complexity
- Over-designing Phase 0 and delaying implementation. Mitigation: timebox to 2-3 days max.
  Write decisions, not essays.
- Config migration strategy may need revision later. Mitigation: version the config schema
  from day one (`version = 1`).

### Deliverables
- `docs/architecture.md` — single-process model, component diagram, data flow
- `docs/decisions/` — ADR (Architecture Decision Records) for each numbered decision above
- Updated `CLAUDE.md` reflecting new architecture constraints
- Updated `config.py` design (on paper, not code yet)

### Exit Criteria
- [ ] Every decision in this phase has a written record with rationale
- [ ] Team can answer: "where does config live?", "who owns Qdrant?", "how does CLI talk to
      service?", "how do ignore rules layer?" without ambiguity
- [ ] No code written — this phase is complete when decisions are locked

</phase_0>

---

<phase_1>

## Phase 1 — Core Engine Stabilization & Ignore Rules Foundation

### Goal
Refactor the existing CLI/indexing/search/watcher code to prepare for the service layer.
Implement ignore rules in the file discovery and watcher pipelines. After this phase,
the core engine is clean enough that Phase 2 (service) is an additive layer, not a rewrite.

### Why This Phase Exists
The current code works but has assumptions baked in that will fight the service architecture:
- `SKIP_DIRS` is hardcoded with no user configurability
- Config is env-var-only with no persistent file
- No concept of "check if service is running" in CLI
- Watcher filter (`_md_filter`) duplicates skip logic from scanner without sharing it
- Encoder and Qdrant client are created per-command — fine for CLI, but service needs
  shared instances

This phase fixes those foundations WITHOUT building the service yet.

### Features Included

**1. Ignore rules engine**
- New module: `src/ragtools/ignore.py`
- Loads built-in defaults + global config patterns + `.ragignore` files
- Exposes a single function: `is_ignored(file_path, content_root) -> bool`
- Also exposes: `get_ignore_reason(file_path, content_root) -> str | None` for diagnostics
- Uses `pathspec` library for gitignore-style matching

**2. Scanner integration**
- `discover_markdown_files()` uses ignore engine instead of hardcoded `SKIP_DIRS`
- `SKIP_DIRS` constant removed (replaced by built-in defaults in ignore engine)
- Scanner reports ignored file count in its return value

**3. Watcher integration**
- `_md_filter()` uses the same ignore engine instance
- Ignored file changes don't trigger indexing
- Watcher logs ignored events at DEBUG level

**4. Config file support**
- `config.py` refactored to support TOML config file as a source
- Priority: env vars > config file > defaults
- `Settings` class gains `ignore_patterns: list[str]` and `use_ragignore_files: bool` fields
- New helper: `Settings.load(config_path=None)` — finds and loads config file
- Config file created with defaults on first access if missing

**5. CLI ignore commands**
- `rag ignore list` — show all active ignore patterns (built-in + config + ragignore files found)
- `rag ignore test <path>` — check if a specific file would be ignored and why
- `rag index` gains `--show-ignored` flag to print ignored files during indexing

**6. Shared component preparation**
- Encoder class gets a `threading.Lock` around encode calls (prep for service threading)
- Extract Qdrant client creation into a `QdrantManager` class that supports both
  persistent (service) and transient (CLI) modes — but service mode is NOT implemented yet
- Clean up imports so that `indexer.py` doesn't directly construct clients

### Technical Work

1. Add `pathspec` to dependencies in `pyproject.toml`
2. Create `src/ragtools/ignore.py`:
   - `IgnoreRules` class: loads built-in + config + `.ragignore` files
   - Caches compiled pathspec per directory (`.ragignore` files)
   - Method: `is_ignored(path: Path, root: Path) -> bool`
   - Method: `get_reason(path: Path, root: Path) -> str | None` (returns "built-in: .git/",
     "config: drafts/", "ragignore: /path/.ragignore: *.tmp", or None)
3. Refactor `scanner.py`:
   - `discover_markdown_files(directory, ignore_rules) -> list[Path]`
   - Remove `SKIP_DIRS` constant
   - Accept `IgnoreRules` instance as parameter
4. Refactor `observer.py`:
   - `_md_filter` uses shared `IgnoreRules` instance
   - `run_watch()` constructs `IgnoreRules` once and passes it
5. Add `tomli-w` to dependencies for config writing
6. Refactor `config.py`:
   - Add `config_path` resolution logic (check `RAG_CONFIG_PATH` env var, then
     `%LOCALAPPDATA%\RAGTools\config.toml`, then `./ragtools.toml`)
   - Add ignore-related fields to `Settings`
   - Add `save()` method for writing back config changes (needed later for admin panel)
7. Add `rag ignore` CLI subcommand group
8. Add `--show-ignored` flag to `rag index`
9. Write tests:
   - `tests/test_ignore.py` — pattern matching, layered precedence, ragignore parsing
   - Update `test_scanner.py` — scanner uses ignore rules
   - Update `test_integration.py` — watcher respects ignore rules

### Dependencies
- Phase 0 decisions must be locked (especially ignore rules design and config file strategy)
- `pathspec` library must be evaluated and confirmed (MIT license, no heavy deps)

### Risks / Hidden Complexity
- **`.ragignore` caching:** if a `.ragignore` file changes while the watcher is running,
  the ignore rules are stale. Mitigation: the watcher should also watch for `.ragignore`
  file changes and reload the ignore engine when they change.
- **Backwards compatibility:** removing `SKIP_DIRS` could change indexing behavior for
  existing users if built-in defaults don't exactly match. Mitigation: built-in defaults
  should be a superset of current `SKIP_DIRS`.
- **Config file location on dev vs installed:** need the resolution chain to work cleanly
  in both modes without surprising behavior.

### Deliverables
- `src/ragtools/ignore.py` — ignore rules engine
- Refactored `scanner.py`, `observer.py`, `config.py`
- `rag ignore list` and `rag ignore test` CLI commands
- `--show-ignored` flag on `rag index`
- Tests for ignore rules
- Updated `.env.example` with new fields
- Default `ragtools.toml` / `config.toml` template

### Exit Criteria
- [ ] `rag index . --show-ignored` prints ignored files with reasons
- [ ] `rag ignore test some/path/file.md` correctly reports whether it's ignored and why
- [ ] Creating a `.ragignore` file with `*.tmp` in a project dir causes `.tmp` files to be
      skipped during indexing
- [ ] Adding `patterns = ["drafts/"]` to config causes `drafts/` dirs to be skipped
- [ ] Watcher does NOT trigger indexing for changes to ignored files
- [ ] All existing tests still pass
- [ ] No service code written yet — this phase is purely engine-level

</phase_1>

---

<phase_2>

## Phase 2 — Service Layer (The Hard Phase)

### Goal
Build the long-running localhost service that owns Qdrant, runs the watcher as a thread,
exposes an HTTP API, and makes CLI commands work through it. After this phase, the system
has a stable background process that other components can safely delegate to.

### Why This Phase Exists
This is the architectural pivot. Today, each CLI command opens Qdrant, does work, closes it.
The MCP server opens Qdrant at startup and holds it until killed. The watcher opens/closes
per indexing run. None of these can coexist safely.

The service becomes the single owner: it opens Qdrant once, keeps it open, and serves all
requests through its HTTP API. Everything else — CLI, MCP, admin panel — becomes a client.

### Features Included

**1. FastAPI service (`src/ragtools/service/`)**
- `app.py` — FastAPI application with all API endpoints
- `lifecycle.py` — start, stop, status logic with PID file management
- `watcher_thread.py` — watcher adapted to run as a daemon thread
- `qdrant_owner.py` — shared Qdrant client + Encoder, protected by locks

**2. HTTP API endpoints**
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Readiness probe (returns 200 when encoder loaded + Qdrant open) |
| GET | `/api/search` | Search knowledge base (query, project, top_k params) |
| POST | `/api/index` | Trigger indexing (body: path, full, project) |
| POST | `/api/rebuild` | Drop and rebuild index |
| GET | `/api/status` | Collection stats (same as `rag status`) |
| GET | `/api/projects` | List indexed projects |
| GET | `/api/config` | Read current config |
| PUT | `/api/config` | Update config (writes to config.toml) |
| POST | `/api/watcher/start` | Start watcher thread |
| POST | `/api/watcher/stop` | Stop watcher thread |
| GET | `/api/watcher/status` | Watcher state (running, paths, last event) |
| GET | `/api/ignore/rules` | Get all active ignore rules |
| POST | `/api/ignore/test` | Test if a path would be ignored |

**3. CLI dual-mode**
- Every existing CLI command gains a service-aware wrapper
- Pattern: try HTTP first, catch connection error, fall back to direct mode
- `rag service start` — launch service as background process (daemonize)
- `rag service stop` — send shutdown signal via API, then verify process exit
- `rag service status` — probe health endpoint, show PID, uptime, Qdrant stats

**4. Service state management**
- PID file at configured location
- Graceful shutdown: API endpoint `/api/shutdown` + signal handling
- On Windows: no POSIX signals — use a named event or simply poll a shutdown flag
- Startup sequence: load config → init encoder (slow, 5-10s) → open Qdrant → start
  FastAPI → optionally start watcher → write PID → ready
- Readiness: `/health` returns 503 until startup sequence completes

**5. Ignore rules in service context**
- Service loads `IgnoreRules` at startup
- All indexing/watcher operations go through the service's shared `IgnoreRules` instance
- Config update via API (`PUT /api/config`) reloads ignore rules
- Watcher thread uses the same ignore rules; `.ragignore` file changes trigger reload

### Technical Work

1. Create `src/ragtools/service/` package:
   - `__init__.py`
   - `app.py` — FastAPI app factory
   - `lifecycle.py` — PidFile class, service start/stop logic
   - `qdrant_owner.py` — singleton that holds QdrantClient + Encoder + Lock
   - `watcher_thread.py` — threading.Thread subclass wrapping `run_watch` logic
2. Implement `/health` with startup state machine: STARTING → LOADING_MODEL → READY
3. Implement graceful shutdown:
   - `rag service stop` calls `POST /api/shutdown`
   - Service sets a shutdown event, watcher thread checks it, uvicorn shuts down
   - If API is unreachable, `rag service stop` reads PID file and kills process
4. Thread safety:
   - `QdrantOwner` class wraps client + encoder with `threading.RLock`
   - Indexing jobs use `QdrantOwner.index_file()` which acquires lock
   - Search uses `QdrantOwner.search()` which acquires lock
   - Watcher thread acquires lock only during its indexing burst, then releases
5. CLI wrapper pattern in `cli.py`:
   ```
   def _service_or_direct(api_path, fallback_fn, **kwargs):
       try: return httpx.get(f"http://localhost:{port}{api_path}", params=kwargs)
       except httpx.ConnectError: return fallback_fn(**kwargs)
   ```
6. Add `httpx` to dependencies (already used by MCP, likely already installed)
7. Add `rag service` subcommand group to CLI
8. Background process launch on Windows:
   - Use `subprocess.Popen` with `CREATE_NO_WINDOW` flag
   - Or use `pythonw.exe` if available
   - Redirect stdout/stderr to log file
9. Write tests:
   - `tests/test_service.py` — API endpoint tests (use FastAPI TestClient)
   - `tests/test_lifecycle.py` — PID file management, start/stop
   - `tests/test_dual_mode.py` — CLI behavior with and without service running

### Dependencies
- Phase 1 complete (ignore rules, config refactor, shared component prep)
- `fastapi`, `uvicorn`, `httpx` added to dependencies

### Risks / Hidden Complexity

- **Qdrant client thread safety:** Qdrant Python client may or may not be thread-safe.
  Must test under concurrent search + index load. If not safe, all access must be
  serialized through a single-threaded executor.
- **Windows process daemonization:** Python on Windows has no `fork()`. Background process
  launch requires `subprocess.Popen` with detachment flags. Process management is more
  fragile than Unix.
- **Encoder memory:** SentenceTransformer loads ~100MB into RAM. Service holds this
  permanently. Acceptable for a local tool, but worth noting.
- **Port conflicts:** another app on port 21420 blocks startup. Service must detect this
  and fail clearly, suggesting port change in config.
- **Shutdown race conditions:** if the watcher is mid-indexing when shutdown is requested,
  must wait for the current batch to finish or risk corrupted state.

### Deliverables
- `src/ragtools/service/` package (4 modules)
- Updated `cli.py` with dual-mode wrappers and `rag service` subgroup
- Updated `pyproject.toml` with new dependencies
- Service integration tests
- Updated CLAUDE.md with service architecture notes

### Exit Criteria
- [ ] `rag service start` launches a background process, PID file is written
- [ ] `rag service status` shows "running", PID, uptime, Qdrant stats
- [ ] `rag service stop` shuts it down cleanly, PID file is removed
- [ ] `rag search "query"` works when service is running (via HTTP)
- [ ] `rag search "query"` works when service is NOT running (direct Qdrant)
- [ ] `rag index .` delegates to service when running, direct when not
- [ ] Watcher can be started/stopped via `POST /api/watcher/start|stop`
- [ ] Ignore rules are enforced in all service-driven operations
- [ ] `/health` returns 503 during startup, 200 when ready
- [ ] Concurrent search requests don't crash (thread safety verified)
- [ ] All existing CLI tests still pass in direct mode

</phase_2>

---

<phase_3>

## Phase 3 — MCP Integration Redesign

### Goal
Convert the MCP server from a standalone Qdrant-holding process into a thin stdio proxy
that forwards requests to the running service. If the service isn't running, fall back
gracefully.

### Why This Phase Exists
Today, Claude CLI launches `rag-mcp` as a subprocess. That subprocess loads the encoder
(5-10s), opens Qdrant (exclusive lock), and holds both for its lifetime. With the service
owning Qdrant, the MCP server cannot also open it. The MCP process must become lightweight.

This is a separate phase because:
1. It's a clean, isolated piece of work
2. It can be tested independently
3. Getting it wrong breaks Claude CLI integration — the product's primary use case
4. It requires careful fallback design

### Features Included

**1. MCP proxy mode (primary)**
- MCP server starts, does NOT load encoder or open Qdrant
- `search_knowledge_base` → `GET http://localhost:{PORT}/api/search`
- `list_projects` → `GET http://localhost:{PORT}/api/projects`
- `index_status` → `GET http://localhost:{PORT}/api/status`
- Startup is near-instant (no model loading)

**2. MCP fallback mode (service not running)**
- If `localhost:{PORT}/health` fails on startup, fall back to current direct mode
- Load encoder + open Qdrant directly (current behavior)
- Log a warning: "Service not running, using direct mode. Start service for better
  performance and to avoid lock contention."

**3. MCP status awareness**
- `index_status` tool response includes whether MCP is in proxy mode or direct mode
- Helps Claude (and user) understand the system state

### Technical Work

1. Refactor `src/ragtools/integration/mcp_server.py`:
   - On startup: probe `localhost:{PORT}/health`
   - If reachable: set `_mode = "proxy"`, store port, skip encoder/Qdrant init
   - If unreachable: set `_mode = "direct"`, do current initialization
   - Each tool handler checks `_mode` and routes accordingly
2. Proxy implementation: use `httpx` (sync client) to forward requests
   - MCP tools are synchronous from the tool handler's perspective
   - `httpx.Client` with short timeout (5s for search, 30s for index)
3. Error handling:
   - Service goes down mid-session → MCP returns error to Claude with clear message
   - Do NOT try to auto-switch to direct mode mid-session (Qdrant may still be locked)
4. Update `.claude/settings.json` or `.mcp.json` if MCP server config needs changes
5. Write tests:
   - `tests/test_mcp_proxy.py` — mock HTTP backend, verify proxy behavior
   - `tests/test_mcp_fallback.py` — no backend available, verify direct mode works
   - `tests/test_mcp_tools.py` — tool responses match expected format in both modes

### Dependencies
- Phase 2 complete (service must be running and stable)
- `httpx` already added in Phase 2

### Risks / Hidden Complexity

- **Claude CLI process lifecycle:** Claude CLI launches MCP server and expects it to stay
  alive. If the proxy can't reach the service, it must either fall back cleanly or return
  clear error messages — not crash.
- **Timeout tuning:** search is fast (~100ms), but indexing via MCP could take minutes.
  Need separate timeouts per operation.
- **Service restart during Claude session:** if user restarts the service while Claude is
  using MCP, the proxy must handle connection resets gracefully.
- **Config discovery:** MCP process needs to know the service port. It should read from
  the same config file, or accept it as a CLI argument / env var.

### Deliverables
- Rewritten `src/ragtools/integration/mcp_server.py` with proxy + fallback
- MCP integration tests
- Updated MCP config files if needed

### Exit Criteria
- [ ] With service running: Claude CLI uses MCP, searches work, MCP startup is <1 second
- [ ] Without service: MCP falls back to direct mode, searches still work (with 5-10s delay)
- [ ] MCP process does not open Qdrant when service is running (verified by checking no
      lock file conflict)
- [ ] `index_status` tool reports which mode MCP is operating in
- [ ] Service restart during Claude session doesn't crash MCP (graceful error recovery)

</phase_3>

---

<phase_4>

## Phase 4 — Web Admin Panel

### Goal
Ship a minimal but functional browser-based admin panel served by the FastAPI service.
Config, status, actions, ignore rules management, and search testing — all in one local
web interface.

### Why This Phase Exists
CLI is sufficient for developers, but a web panel is what makes this a product. It's what
lets you configure ignore rules without editing TOML, trigger a rebuild without opening a
terminal, and monitor indexing status at a glance. It's the Syncthing model: install once,
manage from browser.

This comes after the service and MCP phases because the panel is purely a frontend to the
existing API. No new backend capabilities are needed — just UI.

### Features Included

**1. Dashboard page**
- Service status (uptime, PID, mode)
- Indexed projects table (name, files, chunks, last indexed)
- Watcher status (running/stopped, paths watched, last event)
- Quick stats (total files, total chunks, Qdrant collection info)

**2. Paths page**
- List of configured content root paths
- Add new path (directory picker or text input)
- Remove path
- Per-path: file count, last indexed, status

**3. Index page**
- Trigger incremental index (per-project or all)
- Trigger full rebuild
- Index progress indicator (poll-based)
- Last index result summary

**4. Search page**
- Query input box
- Project filter dropdown
- Results displayed with confidence labels, source files, heading hierarchy
- Useful for debugging retrieval quality

**5. Ignore rules page**
- View built-in default patterns (read-only, displayed for reference)
- Edit global ignore patterns (text area or tag-style input)
- Toggle: "Parse .ragignore files" (on/off)
- List of discovered `.ragignore` files with their contents (read-only)
- Test widget: enter a file path, see if it would be ignored and why
- Save button writes to `config.toml` via `PUT /api/config`

**6. Config page**
- View/edit key settings: chunk_size, chunk_overlap, top_k, score_threshold
- Service port (requires restart)
- Log level
- Data directory paths (read-only for now)

**7. Startup page (UI only — backend in Phase 5)**
- Placeholder page showing current startup status
- Toggles for: start on login, auto-start watcher, open browser on startup, delay
- These write to config, but actual Task Scheduler integration is Phase 5

### Technical Work

**Frontend approach: htmx + Jinja2**
- NOT React. This is a local admin dashboard, not a SPA.
- Server-rendered HTML with Jinja2 templates
- htmx for dynamic updates without full page reloads (toggles, forms, status polling)
- Minimal CSS — use a classless CSS framework like Pico CSS or Simple.css (~5KB)
- No build step. No npm. No node_modules. Templates live in `src/ragtools/service/templates/`
- Static assets (if any) in `src/ragtools/service/static/`

**Backend additions:**
1. Mount Jinja2 templates in FastAPI app
2. Mount static files directory
3. Add HTML-serving routes alongside API routes:
   - `GET /` → dashboard
   - `GET /paths` → paths page
   - `GET /index` → index page
   - `GET /search` → search page
   - `GET /ignore` → ignore rules page
   - `GET /config` → config page
   - `GET /startup` → startup settings page
4. API endpoints already exist from Phase 2 — pages call them via htmx

**Ignore rules UI detail:**
- Ignore page loads current rules from `GET /api/ignore/rules`
- Built-in defaults displayed in a `<pre>` block, grayed out, with note "these cannot be changed"
- Global patterns displayed in editable `<textarea>`, one pattern per line
- "Parse .ragignore files" as a checkbox toggle
- "Test path" input: type a path, click test, shows result from `POST /api/ignore/test`
- Save button sends `PUT /api/config` with updated ignore section

### Dependencies
- Phase 2 complete (all API endpoints exist)
- Phase 3 recommended but not strictly required (MCP doesn't affect panel)
- `jinja2` already installed (FastAPI optional dependency)
- `python-multipart` for form handling

### Risks / Hidden Complexity
- **Scope creep.** The feature list above is already ambitious for a "minimal" panel.
  Prioritize: Dashboard + Paths + Index + Ignore. Search and Config are nice-to-have.
  Startup page is just a placeholder.
- **htmx learning curve** if unfamiliar. Mitigation: htmx is ~14KB, well-documented,
  and has a simple mental model (HTML over the wire).
- **Template organization.** With 7+ pages, need a base template with nav and per-page
  templates. Not complex, but needs structure from the start.
- **Real-time status updates.** Dashboard should auto-refresh. Use htmx polling
  (`hx-trigger="every 5s"`) — simple and works. Don't add WebSockets for MVP.

### Deliverables
- `src/ragtools/service/templates/` — Jinja2 templates for all pages
- `src/ragtools/service/static/` — CSS file(s)
- Updated `app.py` with template routes
- Template base layout with navigation

### Exit Criteria
- [ ] `localhost:21420` in browser shows a working dashboard with real data
- [ ] Can add a path, trigger index, see results update
- [ ] Can edit ignore patterns, save, verify they take effect on next index
- [ ] Can toggle watcher on/off from the panel
- [ ] Can run a test search and see results
- [ ] Ignore test widget correctly reports whether a path would be ignored
- [ ] No JavaScript build step required — all templates are server-rendered + htmx

</phase_4>

---

<phase_5>

## Phase 5 — Windows Startup Integration

### Goal
Make the service start automatically when the user logs into Windows. Configurable via
admin panel and CLI. Uses Task Scheduler as the primary mechanism.

### Why This Phase Exists
A local service that requires manual `rag service start` every boot isn't a product — it's
a developer tool. Auto-startup is what makes this feel installed and reliable.

This phase comes after the admin panel because:
1. The panel's Startup page needs to control startup settings
2. The service must be proven stable before auto-starting it
3. Startup registration depends on knowing the executable path (partially addressed here,
   fully in Phase 6 packaging)

### Windows Startup Strategy Comparison

| | Startup Folder | Task Scheduler | Windows Service |
|---|---|---|---|
| **Use for** | Never | Phase 5 (now) | Phase 7+ (future) |
| **Why** | Fragile, shows console window, no retry, no delay, not programmatic | Invisible, configurable delay, retry on failure, scriptable via schtasks | Overkill — requires pywin32, service account, admin install |

**Decision: Task Scheduler.** It gives us invisible startup, configurable delay, automatic
retry, and can be managed programmatically via `schtasks.exe`. No admin privileges needed
for user-logon tasks.

### Features Included

**1. CLI startup management**
- `rag service install` — create Windows scheduled task
- `rag service uninstall` — remove scheduled task
- `rag service install --delay 30` — startup delay in seconds
- `rag service install --no-watcher` — don't auto-start watcher

**2. Scheduled task specification**
- Task name: `RAGTools Service`
- Trigger: at user logon
- Delay: configurable (default 30 seconds)
- Action: `{exe_path} service start --background --from-scheduler`
- Run whether user is logged on: No (run only when logged on)
- Hidden: Yes (no console window)
- Restart on failure: every 60 seconds, up to 3 attempts

**3. Service startup flags**
- `--background` — suppress console output, log to file only
- `--from-scheduler` — indicates launched by scheduler (used for open-browser logic)

**4. Startup behavior sequence**
When launched by scheduler:
1. Load config from `%LOCALAPPDATA%\RAGTools\config.toml`
2. Initialize encoder + Qdrant (5-10 seconds)
3. Start FastAPI on configured port
4. If `startup.start_watcher == true`: start watcher thread
5. If `startup.open_browser == true` AND `--from-scheduler`: open browser to admin panel
6. Write PID file
7. `/health` returns 200 — service is ready

**5. Admin panel Startup page (functional)**
- Toggle: "Start on Windows login" → calls `POST /api/startup/install` or `/uninstall`
- Toggle: "Auto-start watcher" → updates `config.toml` `[startup]` section
- Toggle: "Open browser on startup" → updates config
- Number input: "Startup delay (seconds)" → updates config and re-registers task
- Status indicator: "Scheduled task: installed / not installed"

**6. Config persistence for startup**
```toml
[startup]
enabled = false          # Whether scheduled task should exist
delay_seconds = 30       # Delay after logon before starting
start_watcher = true     # Auto-start watcher
open_browser = false     # Open admin panel in browser on auto-start
```

### Technical Work

1. Create `src/ragtools/service/startup.py`:
   - `install_scheduled_task(exe_path, delay, ...)` — runs `schtasks /create`
   - `uninstall_scheduled_task()` — runs `schtasks /delete`
   - `is_task_installed() -> bool` — runs `schtasks /query`
   - `get_task_info() -> dict` — parse task status
2. `exe_path` detection:
   - If running from installed location: use that path
   - If running from venv: use `sys.executable` + `rag` entry point
   - Store resolved path in config for consistency
3. Add API endpoints:
   - `POST /api/startup/install` — create scheduled task with current config
   - `POST /api/startup/uninstall` — remove scheduled task
   - `GET /api/startup/status` — task installed, next run, last result
4. Add `--background` and `--from-scheduler` flags to `rag service start`
5. Background mode on Windows:
   - Use `CREATE_NO_WINDOW` flag on subprocess if launching from CLI
   - For scheduler: task is already hidden, just redirect stdout/stderr to log
6. Wire up admin panel Startup page to functional API endpoints
7. Write tests:
   - `tests/test_startup.py` — mock `schtasks` calls, verify command construction
   - Test install → verify → uninstall cycle

### Dependencies
- Phase 2 (service must work reliably)
- Phase 4 (admin panel Startup page exists as placeholder, now made functional)

### Risks / Hidden Complexity

- **Path stability:** if the user runs `rag service install` from a venv and later moves
  the venv, the scheduled task breaks. Mitigation: warn if exe path looks like a venv;
  recommend installing the package first.
- **UAC and permissions:** `schtasks /create` for user-logon tasks doesn't need admin.
  But `/create` with `/ru SYSTEM` does. Stay at user level.
- **schtasks XML vs command-line:** complex task configurations may require XML task
  definition. For MVP, the `/create` command-line flags are sufficient.
- **Testing scheduled tasks:** hard to test in CI. Use mock-based tests for command
  construction; manual testing for actual task behavior.

### Deliverables
- `src/ragtools/service/startup.py`
- Functional Startup page in admin panel
- `rag service install` / `uninstall` CLI commands
- Updated config schema with `[startup]` section

### Exit Criteria
- [ ] `rag service install` creates a scheduled task visible in Task Scheduler
- [ ] Rebooting Windows → service is running → `localhost:21420` responds
- [ ] Watcher auto-starts if configured
- [ ] Browser does NOT open unless explicitly enabled
- [ ] `rag service uninstall` removes the scheduled task
- [ ] Admin panel can toggle startup on/off and changes take effect
- [ ] Startup delay is respected (service starts N seconds after logon)

</phase_5>

---

<phase_6>

## Phase 6 — Packaging & Installer

### Goal
Package RAG Tools into a self-contained Windows installer that doesn't require Python,
pip, or any dev tooling on the target machine.

### Why This Phase Exists
Everything up to Phase 5 assumes the user has Python installed and can `pip install -e .`.
For a real product, the user downloads an installer, runs it, and everything works.
Packaging also stabilizes the executable path, which makes Task Scheduler registration
reliable.

### Features Included

**1. Self-contained executable bundle**
- PyInstaller one-dir bundle (NOT one-file — faster startup, easier debugging)
- Bundles: Python runtime, all dependencies, SentenceTransformer model, templates, static
- Entry point: `rag.exe` in the install directory

**2. Inno Setup installer**
- Install to `C:\Program Files\RAGTools\` (or user-chosen directory)
- Create `%LOCALAPPDATA%\RAGTools\` for data/config/logs
- Add `rag.exe` to PATH (optional, user choice during install)
- Create Start Menu shortcut: "RAG Tools Admin" → opens browser to `localhost:21420`
- Install-time option: "Start on Windows login" → runs `rag service install`
- Install-time option: "Start service now" → launches service after install

**3. Uninstaller**
- Stops running service
- Removes scheduled task
- Removes installed files
- Optionally removes data directory (user choice: "Keep your data?" prompt)

**4. Upgrade behavior**
- Installer detects existing installation
- Stops service before upgrading
- Overwrites program files, preserves data directory
- Re-registers scheduled task with new exe path
- Restarts service after upgrade

**5. Config/data directory initialization**
- First run: if `%LOCALAPPDATA%\RAGTools\config.toml` doesn't exist, create with defaults
- If `data\` directory doesn't exist, create it
- Migrate from dev-mode `./data/` if detected (optional — may skip for v1)

### Technical Work

1. PyInstaller spec file:
   - `rag.spec` with one-dir config
   - Include `src/ragtools/service/templates/` as data files
   - Include `src/ragtools/service/static/` as data files
   - Hidden imports for sentence-transformers, torch (these are tricky with PyInstaller)
   - Test that the bundle actually works (SentenceTransformers + PyTorch + Qdrant in a
     frozen bundle is non-trivial)
2. Model bundling decision:
   - Option A: Bundle the model files inside the installer (~100MB addition)
   - Option B: Download model on first run (requires internet)
   - Recommendation: **Bundle the model.** Local-first means no internet dependency.
     Accept the larger installer size (~500MB+).
3. Inno Setup script: `installer.iss`
   - Install directory, PATH modification, Start Menu shortcuts
   - Pre-install: stop service, remove old task
   - Post-install: optionally register task, optionally start service
   - Uninstall: stop service, remove task, remove files
4. Code signing (optional for now):
   - Without signing: Windows SmartScreen will warn. Acceptable for early releases.
   - With signing: need a code signing certificate ($200-400/year). Defer to Phase 7.
5. Test matrix:
   - Fresh install on clean Windows
   - Upgrade over existing install
   - Uninstall with data preservation
   - Uninstall with data deletion

### Dependencies
- Phase 5 complete (startup integration must work before baking into installer)
- PyInstaller must handle the dependency stack (this is the biggest risk)

### Risks / Hidden Complexity

- **PyInstaller + PyTorch + SentenceTransformers = pain.** This combination is notoriously
  difficult to bundle. Large binary size, missing hidden imports, DLL dependencies.
  Mitigation: start testing PyInstaller builds EARLY (ideally a spike in Phase 2).
  Consider Nuitka as a fallback if PyInstaller fails.
- **Installer size.** PyTorch alone is ~200MB. SentenceTransformers + model ~150MB. Qdrant
  native libs ~50MB. Total installer could be 500MB-1GB. This is acceptable for a local
  tool but worth noting. Consider `onnxruntime` + ONNX-exported model as a size-reduction
  strategy for a future version.
- **Anti-virus false positives.** PyInstaller executables are frequently flagged by Windows
  Defender and other AV. Code signing reduces this. Without signing, users may need to
  add an exception.
- **PATH conflicts.** If user has a dev install AND a packaged install, `rag` command could
  resolve to either. Installer should warn if `rag` is already on PATH.

### Deliverables
- `rag.spec` — PyInstaller spec file
- `installer.iss` — Inno Setup script
- Build script (`scripts/build_installer.py` or `Makefile`)
- Tested installer for Windows x64
- Documentation for installation process

### Exit Criteria
- [ ] Fresh Windows machine (no Python): run installer → service starts → `localhost:21420`
      shows admin panel → search works
- [ ] Upgrade: install v2 over v1 → data preserved → service restarts → new version shown
- [ ] Uninstall: remove everything → scheduled task gone → no leftover processes
- [ ] Model loads from bundled files without internet access
- [ ] `rag.exe doctor` passes all checks on an installed system

</phase_6>

---

<phase_7>

## Phase 7 — Distribution, Release Maturity & Future Vision

### Goal
Establish a professional release pipeline, publish to winget, and define the long-term
product roadmap beyond the core phases.

### Why This Phase Exists
Phases 0-6 produce a working, installable product. Phase 7 makes it distributable and
maintainable. It also captures the future vision so that earlier phases don't accidentally
close doors.

### Features Included

**1. Winget distribution**
- Winget manifest files (YAML)
- Published to `microsoft/winget-pkgs` repository
- `winget install RAGTools` works
- `winget upgrade RAGTools` works

**2. Release process**
- Versioned releases on GitHub (semantic versioning)
- GitHub Actions workflow: build → test → package → create release → attach installer
- Changelog generation from git history or conventional commits
- Release checklist documented

**3. Auto-update notification**
- Service checks for new version periodically (configurable, off by default)
- Admin panel shows "update available" banner with link
- No auto-download or auto-install — user decides when to upgrade

### Technical Work

1. GitHub Actions workflow:
   - Trigger: tag push (`v*`)
   - Steps: checkout → setup Python → install deps → run tests → PyInstaller build →
     Inno Setup compile → create GitHub release → upload installer
2. Winget manifest:
   - `RAGTools.yaml` with installer URL pointing to GitHub release asset
   - Submit PR to `microsoft/winget-pkgs`
   - Automate manifest update on new releases (Komac tool or manual)
3. Version management:
   - Single source of truth in `pyproject.toml` / `src/ragtools/__init__.py`
   - Installer version matches package version
   - `/api/version` endpoint returns current version
4. Update check:
   - Service periodically fetches latest release tag from GitHub API
   - Compares with installed version
   - Stores result in memory, surfaces in admin panel
   - Respects a config toggle: `[updates] check_enabled = false`

### Dependencies
- Phase 6 complete (stable installer exists)
- GitHub repository is public (required for winget)

### Risks / Hidden Complexity
- **Winget review process** can take days/weeks. Plan for delays.
- **Code signing** becomes more important for winget (SmartScreen warnings hurt adoption).
- **CI build environment** must match target environment — build on Windows runner.

### Deliverables
- GitHub Actions release workflow
- Winget manifest files
- Version management system
- Update check feature in admin panel

### Exit Criteria
- [ ] `winget install RAGTools` works on a clean Windows machine
- [ ] `winget upgrade RAGTools` upgrades cleanly, preserves data
- [ ] GitHub release is created automatically on tag push
- [ ] Admin panel shows current version and update availability

---

### Future Vision (Post-Phase 7)

These are NOT part of the roadmap. They are identified opportunities for future product
evolution, listed here so earlier phases don't close doors:

**Windows Service mode**
- Convert from Task Scheduler to a proper Windows Service
- Survives logoff, runs as SYSTEM or dedicated account
- Requires `pywin32` or `nssm` wrapper
- Only worth doing if enterprise/multi-user demand exists

**System tray icon**
- Tray icon with status indicator (green/yellow/red)
- Right-click: Start/Stop service, Open admin panel, Quit
- Requires `pystray` or similar
- Nice UX polish but not essential

**Remote access**
- Optional: bind to `0.0.0.0` instead of `127.0.0.1`
- Requires: API authentication (bearer token or basic auth)
- Use case: access admin panel from another device on LAN

**Multi-user profiles**
- Per-user config and data directories
- User authentication on admin panel
- Only relevant if the tool is shared on a multi-user Windows machine

**Advanced ignore rules UX**
- Per-project ignore rules (separate `[ignore]` per `[paths.my_project]`)
- Ignore rule debugger: "why was this file ignored?" with full rule chain visualization
- Ignore rule suggestions: "these 50 files are indexed but look like generated code"
- `.ragignore` file editor in the admin panel
- Drag-and-drop file testing

**Retrieval improvements (post-MVP)**
- Cross-encoder reranking for better result quality
- Hybrid search (sparse + dense vectors)
- SPLADE sparse embeddings
- Query expansion / reformulation
- Chunk graph (link chunks that reference each other)

**ONNX model optimization**
- Export SentenceTransformer to ONNX format
- Use `onnxruntime` instead of PyTorch for inference
- Reduces bundle size from ~500MB to ~100MB
- Faster cold start, lower memory usage

**Multi-format support**
- PDF indexing (extract text, then chunk)
- Docx indexing
- HTML/web page indexing
- Code file indexing (with language-aware chunking)

</phase_7>

---

<critical_decisions_to_lock_now>

## Decisions That Must Be Made Before Phase 1 Starts

| # | Decision | Recommendation | Impact if deferred |
|---|----------|---------------|-------------------|
| 1 | **Single process model** | One process: FastAPI + watcher thread + shared Qdrant. No multi-process Qdrant access. | Phase 2 will be designed wrong |
| 2 | **Config file format & location** | TOML at `%LOCALAPPDATA%\RAGTools\config.toml` (service mode) or `./ragtools.toml` (dev mode). Env vars override. | Config code will be rewritten in Phase 2 |
| 3 | **Ignore file name** | `.ragignore` — familiar pattern, specific enough to avoid conflicts. Not `.indexignore` (too generic) or `.ragtools-ignore` (too long). | Ignore feature implementation will stall |
| 4 | **Ignore matching library** | `pathspec` (MIT, 50KB, implements full gitignore spec). Not regex, not custom glob. | Matching behavior will be inconsistent or limited |
| 5 | **Ignore precedence** | Built-in > global config > .ragignore files. All additive (ignore if ANY rule matches). Negation (`!`) supported in .ragignore files only. | Users will be confused about which rule won |
| 6 | **Service port** | 21420 (fixed default, configurable). Not auto-detect — makes client discovery deterministic. | CLI and MCP won't know where to connect |
| 7 | **Localhost auth** | None. Bind to 127.0.0.1 only. Trusted local user. Add auth only if remote access is added later. | Over-engineering auth wastes time |
| 8 | **Frontend framework** | htmx + Jinja2 + Pico CSS. No React, no npm, no build step. Server-rendered. | Wrong choice here means rewriting the entire panel |
| 9 | **Log strategy** | Python `logging` with `RotatingFileHandler`. 10MB per file, 3 files kept. Logs at `%LOCALAPPDATA%\RAGTools\logs\`. | Logs will fill disk or be unfindable |
| 10 | **One user assumption** | Single local user. No user accounts, no sessions, no multi-user. | Architecture becomes unnecessarily complex |
| 11 | **MCP transport** | Keep stdio (Claude CLI expects subprocess). MCP becomes HTTP proxy, not transport change. | MCP redesign in Phase 3 will be confused |
| 12 | **Windows shutdown mechanism** | HTTP `POST /api/shutdown` + PID file fallback (kill process). No POSIX signals on Windows. Use threading.Event for graceful shutdown. | Service stop will be unreliable |
| 13 | **Watcher unavailable paths** | Log warning, skip unavailable paths, keep watching available ones. Don't crash. Re-check periodically. | Watcher crashes when USB drive disconnected |
| 14 | **Built-in ignore defaults** | Must be a strict superset of current `SKIP_DIRS`. Add common extras: `.cache/`, `*.pyc`, `vendor/`, `bower_components/`. | Backwards compatibility break on indexing behavior |
| 15 | **Data dir detection** | Env var `RAG_DATA_DIR` > `%LOCALAPPDATA%\RAGTools\` (if installed) > `./data/` (dev fallback). Detection: check if running from an installed path. | Config and data end up in wrong places |

</critical_decisions_to_lock_now>

---

<final_recommendation>

## Recommended Implementation Sequence

```
Phase 0: Architecture decisions & documentation     [~3 days]
    ↓
Phase 1: Core stabilization + ignore rules engine   [~1-2 weeks]
    ↓
Phase 2: Service layer (THE critical phase)          [~2-3 weeks]
    ↓
Phase 3: MCP proxy redesign                          [~3-5 days]
    ↓
Phase 4: Web admin panel                             [~2 weeks]
    ↓
Phase 5: Windows startup (Task Scheduler)            [~1 week]
    ↓
Phase 6: Packaging & installer                       [~2 weeks]
    ↓
Phase 7: Distribution & release maturity             [~1 week + ongoing]
```

**The single most important thing:** Phase 2 is where this project either succeeds or
gets stuck. The service layer is a real architecture change, not a feature addition.
Budget accordingly. Everything after Phase 2 is incremental.

**Start a PyInstaller spike in Phase 2** — don't wait until Phase 6 to discover that
PyTorch + SentenceTransformers won't bundle. A 2-hour test early saves weeks of pain later.

**Ship Phase 2 without a web panel, without auto-start, without packaging.** Prove the
service works. Then layer the rest on top of a solid foundation.

</final_recommendation>
