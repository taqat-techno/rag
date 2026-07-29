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
- Path: `{data_dir}/data/logs/service.log` (under the `data/` subdir alongside Qdrant — derived as `qdrant_path.parent/logs/`; packaged: `%LOCALAPPDATA%\RAGTools\data\logs\service.log`)
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

## Decision 16 — API Contracts Are Additive

**Decision:** Public HTTP endpoints (`/health`, `/api/status`, `/api/projects`, `/api/watcher/status`, `/api/system-health`) and the `scale.level` enum are **stable contracts**. Patch and minor releases may **add** keys/fields/levels but must not remove or rename existing ones, nor change the type of an existing key. Breaking changes require a major-version bump and a release note.

**Specifically pinned (Phase A):**
- `scale.level` ⊆ `{"ok", "approaching", "over"}`. The set is closed; a fourth level is a breaking change.
- `/health` 200 keys: `status`, `collection`, `version`, `watcher_running`. Future additive fields are allowed.
- `/health` non-200 responses always carry FastAPI's default `{"detail": "..."}` JSON body.
- `/api/watcher/status` keys: `running`, `paths`, `project_count`, `last_started_at`, `last_error`, `last_error_at`, `consecutive_failures`. Older clients that only inspect the first three continue to work. *(Additive since, per Decision 17: `state`, `desired`, and — only on an autostart failure — `autostart_error`/`autostart_error_at`.)*
- `rag service status` exit code: `0` running or starting, `1` down, `2` internal error. CI scripts may rely on these. *(L5: a foreign process occupying the port is reported via the additive `status: "port_occupied_foreign"` and maps to `1` — our service is not running — so a `200` from a non-ragtools server is never read as healthy. The exit codes themselves are unchanged.)*

**Test enforcement:** `tests/test_scale_warning.py` includes `test_scale_level_enum_is_closed_set` so any silent fourth level fails CI. Route tests assert the documented field set is present.

**Rationale:** The plugin layer, admin panel, and external monitors all read these endpoints today. Stable contracts mean each side can ship independently — covered by the cross-repo decision report (May 2026).

**Tradeoff:** Some legitimate refactors become more expensive. Acceptable: the cost of a contract break (downstream consumers stop working) is far higher than the cost of an extra field.

---

## Decision 17 — Watcher Autostart Is Lifecycle-Owned (M3)

**Decision:** The file watcher is started by the **service lifecycle** — the FastAPI lifespan calls `autostart_watcher()` once the encoder + Qdrant are ready — not by a delayed HTTP self-`POST /api/watcher/start` from `run.py`. Autostart is idempotent (a live thread is left untouched), respects a per-process **desired-state**, and never raises: a construct/start failure is recorded and surfaced via the derived watcher `state` (`autostart_failed`) and `/health` `degraded`, rather than crashing startup.

**Desired-state:** An explicit user stop (`POST /api/watcher/stop`) sets `desired_run = False`; lifecycle autostart and the project-edit restart will not re-start a watcher the user deliberately stopped. It is per-process — a service restart re-arms autostart (a restart is itself an operator action). A user-stopped watcher reports `state: "stopped"`, is **not** `degraded` on `/health`, and shows "stopped by user" on `/api/system-health`.

**Concurrency:** Start/stop logic lives in lock-free internals (`_start_watcher_locked` / `_stop_watcher_locked`) that assume the caller holds `_watcher_lock`; the lifespan, the route handlers, and the project-edit restart all funnel through them. This also removed a latent re-entrant-lock deadlock where the background restart held `_watcher_lock` and then called the lock-acquiring route handlers.

**Rationale:** The old self-POST had a ~30s readiness window; a miss left the watcher silently inactive (report finding A-007) with no signal. Lifecycle ownership removes the window and the race, and the new `state`/`degraded` make any remaining failure visible.

**Non-goals:** In-process auto-restart of a *given-up* watcher thread without a service restart (the Task Scheduler watchdog recovers process death cross-process); cross-restart persistence of desired-state.

**Test enforcement:** `tests/test_watcher_lifecycle.py` and the watcher/lifespan cases in `tests/test_service.py`.

---

## Decision 18 — Configuration Migration Runs at a Single Bootstrap Seam

**Decision:** The v2→v3 config migration is invoked from exactly one place —
`ragtools.bootstrap.ensure_config_current()` — called **after `Settings()` is
constructible but before any `QdrantOwner` exists**, and only from entry points
that legitimately write (`service/run.py`, `service/app.py`, `cli.py`). Read
paths (`retrieval/searcher.py`, `selfcheck.py`, the MCP server) never migrate.

**Why the ordering is load-bearing:** `QdrantOwner.__init__` opens the store and
calls `ensure_collection()` while constructing itself. A migration that ran
afterwards would already be looking at a store built to the previous layout, so
"migrate, then construct" is a correctness requirement, not a style preference.

**Why only writers:** a read path that rewrites the user's configuration as a
side effect is a worse defect than the one being fixed. It is safe to exclude
them because migration is deliberately behaviour-preserving — see below — so a
reader looking at an unmigrated file resolves exactly the same backend and
layout it would resolve afterwards.

**Migration never changes an existing install's collection layout.** Switching
`shared` → `per_project` invalidates every file hash in the state DB (correctly:
see `index_identity`), so the next index run re-embeds the entire corpus — tens
of thousands of files, at first boot, on a machine the user has just upgraded.
It also does not reliably fix the size ceiling it appears to fix: splitting one
collection into twenty-five only helps if no *single* project exceeds the limit,
and a project that vendors a framework can exceed it alone. The layout is
therefore reachable (`rag storage strategy`), recommended when it would genuinely
help, and never imposed. A config with **no projects** adopts `per_project`,
because there is nothing to re-index.

**Version stamping is single-sourced.** `CONFIG_VERSION` is defined once, in
`config.py`. Config writers preserve whatever version a file declares and never
invent one; only the migrator changes it. Previously three literals disagreed —
`_save_projects_to_toml` wrote `2` from sixteen production call sites — so a
migrated config was demoted by the user's next edit and re-migrated on the
following boot, forever.

**Failure is loud, not fatal.** A configuration that cannot be rewritten (read-only
volume, permissions, another process holding the lock) must not stop the service
starting. It degrades to the values already resolved, reports on `/health`
(`config_state`, and `config_migration_failed` in `issues`) and in `rag doctor`,
and retries next boot.

**Concurrency:** an exclusive-create lock file beside the config, with a stale
timeout. The service, tray, MCP server and a CLI command can all start within the
same second of a login; atomic writes protect one writer from interruption, not
two writers from each other.

**Test enforcement:** `tests/test_bootstrap_migration.py` asserts the file is
really rewritten by the seam, that the ordering holds (by AST, in the real entry
points), that concurrent starts cannot corrupt it, and that behaviour is
unchanged for an existing install. `tests/test_config_version_stamp.py` asserts
no production writer can lower the version.

**Tradeoff:** CLI-only users pay one extra TOML read per command. Accepted: the
alternative was a migration that never ran, which is what shipped in 3.0.0 and
3.0.1.

---

## Decision 19 — Managed-Engine Ownership Is Proven, Never Inferred

**Context.** v3.1.0 shipped a managed native Qdrant. `QdrantSupervisor.wait_ready()`
health-gated by polling `/readyz` and accepting any 200. It never consulted the
child it had spawned.

On a machine running two RAG Tools services, both generated a managed config on
the same hardcoded port 21500. The second engine failed to bind and its child
exited — and the second SERVICE then polled that port, got 200 from the FIRST
instance's engine, matched the pinned version (every instance ships 1.15.5, so
the version discriminates nothing), and wrote its collections into a store it did
not own. When the true owner later shut its engine down, both services were left
without a reachable Qdrant.

**Decision.** Ownership of the managed engine is established by evidence this
installation recorded itself, never by the fact that a port answered.

* `service/engine_ownership.py` is the ONLY module that answers "is this engine
  mine?". Four proofs, cheapest first: the spawned child is alive; the
  per-installation API key authenticates; the LISTEN pid is our child; that
  pid's image is the binary we launched.
* A durable manifest at `<data_dir>/qdrant-owner.json` records instance id, pid,
  executable, storage path, ports and start time.
* An occupied port is resolved BEFORE any spawn: reattach when the manifest
  vouches for the listener, otherwise refuse and degrade to embedded with the
  reason surfaced. Refusing pre-spawn is what makes "a failed secondary cannot
  kill the canonical engine" true by construction — there is no failed child.
* Nothing is terminated that the manifest does not vouch for.
* One canonical managed instance per machine. A deliberate secondary declares
  itself twice — non-default ports AND an explicit `instance_id`.

**Why not a lock file.** The contended resource is the TCP port. A held port is
self-cleaning in a way a lock file is not, and a bind conflict is the honest name
for the conflict.

**Why the API key.** It is the only proof that does not depend on being able to
enumerate processes, and the only one that also defends against a *foreign*
Qdrant — another application's — holding the port. It converts the question from
an inference into an authenticated fact.

**Precedent.** The rule was already written down one layer up, in
`service/identity.py`: *"Fields a client checks before issuing any request. A
port is deliberately not among them — a port number alone is never trusted."*
Decision 19 applies that same rule to the engine.

---

## Decision 20 — Destructive Sweeps Use Allow-Lists

**Context.** `relayout.obsolete_collections()` computed `existing - current`:
every collection on the server that this installation's registry did not
recognise. `_retire_old_storage()` deletes what it returns, and
`rag storage reclaim` computed the same difference.

On a shared engine that set is the other installation's entire index. During the
v3.1.0 incident the canonical index survived only because validation never
passed, so the destructive step was never reached. The safety came from an
unrelated bug.

**Decision.** A destructive set is computed from what this installation can
prove it created — the configured shared collection, the registry's project
collections (archived included), and the framework registry's corpora.

Anything else is REPORTED by name and left alone. A `proj_<uuid>` with no
registry row is indistinguishable from another installation's live project, so
the honest answer is to name it, not to guess.

**Cost accepted.** A collection genuinely orphaned by this installation (project
removed from the registry) is leaked rather than reclaimed. Leaking disk is
recoverable; deleting somebody's index is not.

---

## Decision 21 — Installation Integrity and Runtime Readiness Are Different Verdicts

**Context.** The Windows installer ran `rag selfcheck` and printed one fixed
sentence for every non-zero exit: *"a process from the previous version was still
running… some files were skipped… restart Windows, then run this installer
again."* Of the eleven checks `run_selfcheck` performs, five fail for RUNTIME
reasons on a machine whose files are byte-perfect. A storage outage — or a
rebuild that was simply still running — told the user to reboot and reinstall
over a healthy migration.

**Decision.** `rag selfcheck` classifies each failure and exits with the
category, not a bit: `0` clean, `1` integrity, `2` runtime, `3` migrating,
`4` warning. `--json` emits the same verdict machine-readably.

The installer only chooses words. Classification lives in the product because
Pascal Script is the worst available place to decide what a failing check means,
and the CLI already knew.

* **integrity** keeps the file-replacement message — it was never wrong, only
  wrongly applied to everything else.
* **migrating** is an information dialog: installed successfully, rebuild
  continues on its own.
* **runtime** names the likely cause and says plainly that reinstalling will not
  help.

**Invariant preserved.** Incomplete is still not ready. A pending rebuild remains
a FAILING check; only the remedy — and therefore the category — differs.

---

## Decision 22 — The Engine Has One Owner, For Its Whole Life

**Context.** v3.2.0 shipped a class called `QdrantSupervisor` that supervised
nothing after startup. Its handle was assigned once (`app.py:192`) and next read
in the shutdown branch (`app.py:252`). The only two `.poll()` call sites in the
entire engine stack were startup gates. So when the managed engine exited — four
minutes into a migration on one machine, seven idle hours into a session on
another — the service kept answering `/health` while every storage operation
failed, indefinitely, with nothing anywhere recording the exit.

**Decision.** `service/engine_lifecycle.py` owns the child process end to end,
and it is the *only* owner. The outer service supervisor restarts the **service**;
this restarts the **engine**; neither reaches into the other's job.

* **Death is observed, not inferred.** A waiter thread blocks in `proc.wait()`.
  Polling a socket answers "is it reachable"; waiting on the child answers "is it
  gone", and with which code.
* **Intent precedes action.** `request_stop()` sets the stopping flag *before* it
  signals. A deliberate stop and a crash are indistinguishable from an exit code,
  so the ordering is the only thing that separates them — and reversing it turns
  every shutdown into a restart storm.
* **Bounded, loud restarts.** 3 attempts, 2/15/60 s backoff, then
  `restart_exhausted` as a reported state. An unexplained crash must not become an
  unexplained restart loop.
* **The manifest is invalidated at the moment of observed death**, not at
  shutdown, closing the window in which a dead pid is still vouched for.

**Ordering rule.** Instrumentation ships before supervision. A restart loop built
around a crash nobody can explain only converts a silent death into a silent
recovery — so Decision 23 is a prerequisite, not a companion.

---

## Decision 23 — A Child Process Is Never Given an Inherited Handle

**Context.** `subprocess.Popen(cmd)` with no `stdout=` inherits the parent's
handles. Under `ragw.exe` — a GUI-subsystem build with no console — the parent
has none, so CPython's `_get_handles` creates an anonymous pipe, hands the child
the write end, and closes the read end immediately. The child then holds a write
handle to a pipe with **no reader**.

This was measured, not reasoned about: such a child receives `ERROR_BROKEN_PIPE`
— *"The process tried to write to a nonexistent pipe."* Writes fail immediately;
they do not buffer or block. So the engine's every log line failed, for its whole
life, and the cause of two crashes became undiagnosable by construction.

**Decision.** Every spawned child gets an explicit sink. For the engine that is
`data/logs/qdrant.log` (10 MB × 3, rotated **at start**, because the writer is a
child holding the handle and renaming a file underneath it is how a log silently
stops). A log that cannot be opened degrades to `DEVNULL` and is reported on
`/health`. It never degrades to inheritance.

**Two flags, and the second one matters as much as the first.** A supervised
child gets `CREATE_NO_WINDOW` (the engine is a CONSOLE-subsystem image and would
otherwise be handed a console) and must **never** get `DETACHED_PROCESS` —
detaching it would silently break the `proc.wait()` Decision 22 depends on. That
choice lives in `PlatformAdapter.child_process_flags()`, not behind a
`sys.platform` test in the storage module: this project keeps every platform
branch in `ragtools.platform`, and its own AST sweep enforces that.

---

## Decision 24 — Destructive Operations Prove Their Preconditions First

**Context.** Four independent entry points reached `owner.rebuild()` —
`/ui/rebuild`, `POST /api/rebuild`, `rag rebuild`, and the job worker — and not
one asked whether the operation could succeed. The service already knew: `/health`
was reporting `storage_unreachable` at that moment. The rebuild took a backup,
started dropping collections, and surfaced a refused connection as HTTP 500.

**Decision.** `service/destructive.py` is one gate consulted by all four, because
four copies of a check is how three of them stay correct. It refuses when storage
is unreachable, a migration is active, an index is running, or another destructive
operation holds the lock — **before anything mutates**, including before the
backup, which was previously the first thing to happen.

A refusal is a *conflict*, not an error: the request is well-formed and the server
is temporarily unable to honour it. `POST /api/rebuild` returns **409**; the CLI
returns a categorised exit code; the UI returns an error fragment (htmx does not
swap a 4xx, and a refusal the user cannot see is worse than one they can).

**Ordering invariant.** `index_state.db` is deleted only after every target
collection is *proven* to exist. "`recreate_collection` did not raise" is a weaker
claim than "the collection is there", and the backup covers the state DB — it does
not cover vectors.

---

## Decision 25 — Both Halves of a Compatibility Pair Move Together

**Context.** v3.2.0 shipped `qdrant-client 1.18.0` against `qdrant 1.15.5` —
three minors apart, outside the client's own support window, warning on every
startup. Nobody chose that. The server was pinned by a source constant; the
client by an unbounded `>=1.12.0` resolved fresh at build time. A warm developer
machine sat on 1.17.1 and never saw it.

**Decision.** The client requirement is bounded against `PINNED_QDRANT_VERSION`,
and `scripts/check_qdrant_compat.py` runs on all three platforms before packaging.
It rejects an **unbounded requirement on principle**, not merely a bad resolution —
a build that is green by luck is not green. `psutil` is likewise declared rather
than opportunistically imported, so an ownership proof cannot exist or not exist
depending on the build venv.

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
| 16 | API contracts additive-only | `scale.level` enum closed; route fields stable | Yes |
| 17 | Watcher autostart ownership | Lifecycle-owned (lifespan); desired-state respected | Yes |
| 18 | Config migration seam | One call, before any owner; writers only; layout never forced | Yes |
| 19 | Managed-engine ownership | Proven by child + API key + pid + image; manifest-gated | Yes |
| 20 | Destructive sweeps | Allow-list only; unattributed collections reported, never deleted | Yes |
| 21 | Selfcheck verdicts | Category exit codes 0/1/2/3/4; installer branches, never classifies | Yes |
| 22 | Engine lifecycle ownership | One owner, whole life; waiter thread; intent-first stop; bounded restart | Yes |
| 23 | Child process stdio | Explicit sink or DEVNULL — never inheritance | Yes |
| 24 | Destructive preconditions | One shared guard, checked before any mutation; 409 not 500 | Yes |
| 25 | Client/server pin | Bounded together; build gate on all platforms | Yes |


---

## Decision 26 — Whatever Ends Startup Stops What Startup Started

**Context.** `lifespan` started the managed engine, then loaded the encoder. A
DNS failure in the encoder raised, uvicorn reported `STARTUP_FAILURE`, and the
teardown — which lived after `yield` — never ran. The engine survived its parent
with its ownership manifest still vouching for it.

**Decision.** The startup sequence is wrapped in `try`/`finally`. The teardown
that runs on a clean shutdown is the same teardown that runs on a failed one.

**Consequence.** An orphaned engine is not merely untidy: its manifest makes the
next boot *reattach*, which is a materially different code path — no child handle
to wait on, and until v3.4.0 no engine log either. One missing `finally` chose
the harder path for every subsequent start.

---

## Decision 27 — A Bundled Model Is Loaded Without the Network

**Context.** The installer ships a complete Hugging Face cache. `Encoder`
constructed `SentenceTransformer` with a bare Hub repo id, which resolves against
the Hub on every construction — re-validating even files the cache has already
recorded as absent in `.no_exist`. A transient DNS outage took the whole service
down.

**Decision.** Resolve the local cache, load with `local_files_only=True` and the
Hugging Face offline switches set, and reach the network only on a genuine,
classified cache miss. A model that cannot be loaded raises `ModelUnavailable`.

**Consequence.** Startup does not depend on name resolution. And the failure has
a name, so `last_crash.json` can say `subsystem: encoder` — the previous record
said `SystemExit: 3`, which sent the investigation to the storage engine.

---

## Decision 28 — A Status Is a Claim; a Count Is Evidence

**Context.** A machine held one migration unit marked `done` beside 25
collections holding zero points, and 24 units blocked for a reason that had
stopped being true hours earlier. Nothing re-examined either.

**Decision.** `relayout.reconcile()` runs before every resume and makes the
record agree with the store. Where they disagree the count wins — **except when
the count could not be taken**, which demotes nothing. Verified work is
preserved, a lifted block is cleared, the plan store is backed up first, the same
plan is continued, and nothing is deleted.

**Consequence.** "I could not ask" and "there is nothing there" lead to opposite
decisions. Conflating them is what disabled search on a correctly-rebuilt v3.1.0
machine, and the rule is now stated once rather than rediscovered per caller.

---

## Decision 29 — An Empty Unit Must Explain Itself

**Context.** Framework units recorded `points_after = 0` as a literal, and
`validate` objected only to `before > 0 and after == 0`. So any unit the
inventory captured at zero could complete holding nothing.

**Decision.** A unit is `done` only when it holds points **or** carries a
recorded `empty_reason`. The reason is decided from the SOURCE, never from the
store: no indexable files is legitimately empty; a missing project path is a
`failed` unit.

**Consequence.** A migration cannot finish over a collection that never received
a vector. The emptiness of the store is the thing being explained, so it cannot
also be the explanation.

---

## Decision 30 — A Known Condition Is Never an Anonymous 500

**Context.** `/api/search` raised `MigrationInProgress` — a purpose-built
exception carrying a full progress report — and the blanket handler in
`create_app` turned it into `500 {"detail": "Internal Server Error"}`. The MCP
server handled the identical condition correctly, so two interfaces gave opposite
answers to one question.

**Decision.** Handlers are registered per exception TYPE in `service/errors.py`,
not per route. 409 for "the state of this resource forbids it"; 503 with
`Retry-After` for "a dependency is down". Every body carries a stable code, the
current state, and a remediation.

**Consequence.** A per-route `try` is a thing to forget on the next route — and
the route that mattered was the one nobody remembered. A persisted block reason
is reported as `blocked_reason_recorded`, because presenting a two-hour-old
error as current state is how a health payload loses its credibility.
