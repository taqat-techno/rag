# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Identity

Local-first RAG system over documentation, with **opt-in** source-code and config indexing. Claude CLI searches a local Qdrant knowledge base first, then completes answers using its own reasoning. **By default only Markdown / README / text is indexed** (preserving the pre-2.6 documentation-only behavior so upgrades don't balloon the index). Set `index_source_code=True` (`RAG_INDEX_SOURCE_CODE=1`) to also index source files (`.py .js .ts .tsx .jsx .java .go .cs .php .html .css .scss .sql .sh`) and config/data (`.json .yaml .yml .xml .toml .ini`, `Dockerfile`, `requirements.txt`, `pyproject.toml`, `package.json`). **Secret-bearing files (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `credentials*`, `.aws/`, ...) are never indexed**, regardless of the setting.

**Stack:** Python 3.12 / Qdrant local mode / Sentence Transformers / Claude CLI (MCP)
**No Docker. No cloud. No containers.**
**Evolving into:** long-running local service + web admin panel + Windows startup integration

## Architecture Decisions (Non-Negotiable)

### Configuration schema and migration
- **`CONFIG_VERSION` lives in `config.py` and is defined exactly once.** Config
  writers PRESERVE the version a file declares and never invent one; only the
  migrator changes it. (Three literals used to disagree: `_save_projects_to_toml`
  wrote `2` from 16 call sites and `_update_toml_config` stamped `1`, so a
  migrated config was demoted by the user's next project edit.)
- **`bootstrap.ensure_config_current()` is the only migration seam**, called
  before `Settings()` is read by any owner — `QdrantOwner.__init__` opens the
  store and creates collections, so a later migration would be looking at a
  store built to the previous layout. Writers migrate (service, CLI,
  supervisor); read paths do not, which is safe only because migration is
  behaviour-preserving.
- **Migration never changes an existing install's collection layout.** Switching
  layout forces a full re-index of every file at first boot; it is offered via
  `rag storage strategy`, never imposed. A config with no projects adopts
  `per_project` because there is nothing to re-index.
- **A clean install writes the canonical v3 config** (`upgrade.migrate.canonical_document`)
  — version, engine and layout all stated explicitly, so the file says what the
  product is doing rather than relying on code defaults.
- **The storage contract is enforced at config load**, not at first use:
  engine ∈ {embedded, managed, external}, layout ∈ {shared, per_project}, all six
  combinations supported, `external` additionally requires `storage_url`.
  `managed` does NOT — the service starts the server and fills the address in.
- **A layout change preserves the previous index.** The new layout writes to new
  collections and nothing drops the old one, so rollback is a config change
  rather than a restore. `rag storage reclaim` frees the old space afterwards,
  and refuses while the new layout is still empty.

### Storage
- **`storage_backend`** selects the engine: `embedded` (default) | `managed` | `external`.
  - `embedded` — `QdrantClient(path=...)`. An in-process **pure-Python** engine:
    brute-force scan (no HNSW), payload indexes are a silent no-op, snapshots
    raise, one exclusive process lock. Qdrant's own guidance caps it at ~20,000
    points, and that ceiling is a property of *this engine*.
  - `managed` — ragtools supervises a **pinned native `qdrant.exe`** (1.15.5) on
    127.0.0.1, no Docker. Real HNSW, real payload indexes, concurrent readers.
    Set `qdrant_binary` or drop the executable in `<data_dir>/bin`.
  - `external` — a server you run yourself (`storage_url`).
  Never guess: an unknown mode raises rather than downgrading silently, and a
  missing managed binary falls back to embedded **with the reason surfaced** on
  `/health`.
- **The managed engine is OWNED, and ownership is proven — never inferred from a
  port.** `service/engine_ownership.py` is the only place that answers "is this
  engine mine?". Two proofs ALWAYS run — the spawned child is alive, and the
  per-installation API key authenticates — and two are defence in depth (the
  LISTEN pid is our child; its image is the binary we launched). The latter two
  need `psutil`, which is NOT a declared dependency, so they are absent on a
  packaged install; the boundary is carried by the first two, which are the ones
  that close the incident. The port check BINDS rather than connects — a server
  with a saturated accept backlog refuses connections, so connect-probing
  reported a busy port as free. A durable manifest
  (`<data_dir>/qdrant-owner.json`) records instance id, pid, executable, storage
  path, ports and start time.
  - **An occupied port is resolved BEFORE anything is spawned** — reattach when
    the manifest vouches for the listener, otherwise refuse and degrade to
    embedded with the reason. Refusing pre-spawn is what makes "a failed
    secondary cannot kill the canonical engine" true by construction.
  - **Nothing is terminated that the manifest does not vouch for.** Killing on a
    port, or on an image name, is how one installation kills another's database.
  - `wait_ready()` proving only that *a port answered* is the v3.1.0 incident:
    a second service adopted the canonical engine and wrote into it. The rule it
    broke was already written down in `service/identity.py` — *"a port number
    alone is never trusted"* — for the service layer, and had never been applied
    to the engine.
- **One canonical managed instance per machine.** A deliberate secondary must
  declare itself twice — non-default `qdrant_http_port`/`qdrant_grpc_port` AND an
  explicit `instance_id`. Either alone is an accident waiting to be adopted.
- **`collection_strategy`** selects the layout: `shared` (default) | `per_project`.
  - `shared` — one collection named `markdown_kb`, project isolation by payload
    filter (`project_id` on every point). The v2 model, unchanged.
  - `per_project` — one collection per project, named from the project's
    **immutable UUID** (`proj_<uuid>`) so it survives rename *and* path moves,
    plus one shared corpus per framework build (`fw_<slug>_<digest>`). A scoped
    query reads only that project's collection and the framework corpora it
    links — another project's vectors are **never queried**, which makes
    isolation a boundary rather than a filter.
- **Which collection(s) a call touches is decided in ONE place** —
  `collection_router.CollectionRouter`. Never read `settings.collection_name`
  directly in new code; ask the router. (Before it existed, that decision was
  re-made at 38 sites across 11 files.)
- **SQLite for state tracking** — `data/index_state.db`, not JSON files.
  Project/framework identity lives in `data/registry.db` and
  `data/frameworks.db`, opened only in `per_project` mode.
- **All persistent state in `data/`** — delete it to start fresh

### Embeddings
- **Model: `all-MiniLM-L6-v2`** (384 dims, cosine distance) — do not change without rebuilding the entire index
- **Always normalize embeddings** — `normalize_embeddings=True`
- **Batch encode** at 64-128 batch size

### Chunking
- **`chunk_file` dispatcher** (`chunking/dispatch.py`) routes each file by classification (`chunking/languages.py`) → markdown / code / config chunker. Always call `chunk_file`, never a specific chunker directly, from the pipeline.
- **Documentation (md/README/text)** — heading-based chunking: split at `##`/`###`/`####`, fallback paragraph → sentence.
- **Source code** (`chunking/code.py`) — code-aware: Python via stdlib `ast` (classes, methods, functions, decorators, imports, constants, docstrings); brace languages (js/ts/java/go/cs/php/css/scss) via a brace-depth scanner; SQL by statement; shell/html generic. **Whole functions/classes are kept in one chunk** — only a single unit larger than `chunk_size` is split, signature-prefixed.
- **Config/data** (`chunking/config_files.py`) — structure-aware: JSON/package.json by top-level key, YAML by top-level key, TOML/INI by `[section]`, others by line packing.
- **chunk_size=400 tokens, chunk_overlap=100 tokens**
- **Prepend context header** (`language file_name > symbol/heading path`) to chunk text before embedding; **store raw text** in payload for display.
- **Per-chunk metadata** stored in the Qdrant payload: `file_name`, `extension`, `language`, `chunk_type` (code|comment|config|documentation), `module` (project name), `class_name`, `function_name`, `symbols`, `imports`, `exports` (public symbols the chunk defines), `signature` (declaration line of the primary function/class).
- **Language parsers are pluggable** — `chunking/code.register_language(language, extractor)` adds a language without editing `chunk_code_file`. Built-ins: Python (`ast`), brace scanner (js/ts/java/go/cs/php/css/scss/rust/kotlin/scala/swift/c/cpp), SQL; unregistered → generic.
- **Deterministic chunk IDs** — `sha256(project_id::file_path::chunk_index)` formatted as UUID (shared helper `chunking/common.make_chunk_id`).

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
- **Logging** — service mode: `RotatingFileHandler` at `{data_dir}/data/logs/service.log` (alongside Qdrant under `data/`; packaged: `%LOCALAPPDATA%\RAGTools\data\logs\service.log`), 10MB, 3 backups.
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
rag selfcheck                     # Verify the INSTALLATION (version, autostart targets, stray processes)
rag upgrade                       # Bring config to the current schema (same code the service runs at startup)
rag upgrade --dry-run             # Show what would change; write nothing
rag storage show                  # Which engine and collection layout are actually in force
rag storage backend managed       # embedded | managed | external — requires a full re-index
rag storage strategy per_project  # shared | per_project — requires a full re-index
rag storage reclaim               # Drop collections the current layout no longer uses (after re-index)
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
- Do NOT hard-code a collection name — resolve it through `CollectionRouter`.
  (Multiple collections are now a supported layout under
  `collection_strategy="per_project"`; `shared` remains the default and behaves
  exactly as before.)
- Do NOT compute a destructive set as a DENY-list. `obsolete_collections` once
  returned `existing - current` — every collection on the server this
  installation's registry did not recognise — and the caller deletes what it
  returns. On a shared engine that is another installation's entire index.
  Delete only what you can prove you created; report the rest by name.
- Do NOT terminate a process because it holds a port or matches an image name.
  Ask `service/engine_ownership.py`.
- Do NOT let a retrieval entry point pass a caller-supplied project straight to
  the searcher. Resolve it through `mcp_authz.scope_for_search` first — the
  capability check covers the tool NAME, not its SCOPE.
- Do NOT add a second indexing pipeline for a new layout. `run_full_index` /
  `run_incremental_index` are the only indexers; a layout changes the router's
  answer, not the pipeline. (A parallel per-project indexer existed briefly and
  was removed: it silently lacked state tracking, incremental runs, delete
  detection, progress/cancellation, and secret redaction.)
- Do NOT change the embedding model without planning a full rebuild
- Do NOT suggest Docker, containers, server-mode Qdrant, cloud services, or hosted solutions
- Do NOT add cross-encoder reranking, hybrid search, or SPLADE — these are post-MVP. (The lightweight **priority reranker** in `retrieval/rerank.py` is allowed — it only adds a small additive bonus by `chunk_type`; it does not re-embed or call a second model.)
- Do NOT open the Qdrant data directory from multiple processes — the service is the sole owner (see `docs/decisions.md` Decision 1)
- Do NOT use React, npm, or any JS build step for the admin panel — htmx + Jinja2 only (see `docs/decisions.md` Decision 6)

## RAG Knowledge Base (MCP Tools)

One MCP server (`rag-mcp`) with per-tool access control. The agent's
visible toolset depends on which tools the user enabled in the admin
panel's "MCP Tool Access" card — disabled tools are never registered
at startup.

### Core tools — always available

- **search_knowledge_base(query, project?, top_k?)** — Search indexed content (docs + code)
- **search_project_context(query, project?, top_k?)** — Codebase-first layered retrieval for development requests (Project Context Mode)
- **find_definition(symbol, project?, top_k?)** — Cross-file code-graph v1: likely definition sites for a symbol (file:line leads). Semantic discovery, LSP-complementary — not authoritative.
- **secret_audit(project?)** — Audit indexed content for secret material (file:line + rule names, never values). Requires the service.
- **list_projects()** — Discover available project IDs
- **index_status()** — Check if the knowledge base is ready

### Project Context Mode (development requests)

Before answering any **development request** — implementing a feature, fixing a
bug, changing an architecture, modifying an API, refactoring, or enhancing a
workflow — you MUST search the project knowledge base first, then ground the
answer in what exists.

**Feature-aware trigger** — if the request contains any of: *add feature,
implement, create endpoint, add API, modify workflow, extend module, enhance
system, architecture review, refactor, bug fix, API modification* — call
`search_project_context` (or `search_knowledge_base`) BEFORE generating an
answer. Detection logic lives in `retrieval/feature_intent.py`
(`detect_dev_intent`). The detector is **load-bearing**: when the query is a dev
request it selects the codebase-first strategy; otherwise the tool falls back to
a flat semantic search (no code-first bias).

**Discovery, not navigation:** `search_project_context` is **semantic
discovery** — it finds *where* relevant code/docs likely live and *what
patterns* exist. It is **complementary to, not a replacement for, an LSP /
language server**. For precise definitions, references, call sites, rename
safety, and diagnostics, use language tooling and read the cited files; treat
retrieved symbols as leads, not an authoritative index.

**Search strategy** (implemented in `retrieval/dev_pipeline.py`):
1. Search project **codebase** embeddings (`chunk_type=code`).
2. Search project **documentation** embeddings.
3. Search **config / architecture / BRD** embeddings.
4. Combine + **rerank** by context priority.
5. Generate the answer from the retrieved project context.

**Context prioritization** (`retrieval/rerank.py`) — prefer, in order:
1. existing project source code → 2. existing APIs → 3. existing workflows →
4. architecture documents → 5. Markdown docs → 6. general LLM knowledge.
**Prefer existing implementation patterns over inventing new designs.**

**Response format for feature requests:**

```
Relevant Files:
* path/file1
* path/file2

Existing Implementation:
* summary of what those files already do

Recommended Changes:
* change 1
* change 2

Sample Code:
* implementation example consistent with existing patterns
```

Cite actual repository file paths from the retrieved chunks whenever possible.

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
| `RAG_INDEX_SOURCE_CODE` | `false` | Global default when a project has no explicit mode: `false` = docs only, `true` = also index source + config/data. **Per-project this is superseded by `ProjectConfig.mode`** — see below. |

### Per-project indexing mode (authoritative)

Each project carries an explicit **`mode`**, not a boolean. Default is `docs`.

| Mode | Indexes |
|---|---|
| `docs` | documentation / Markdown / text only (**default**) |
| `code` | source + config/data files only |
| `general` | both |

Set it via the admin panel, the CLI `rag project mode <id> docs|code|general`, or the MCP tool
**`set_project_mode(project, mode, confirm_token)`** (`src/ragtools/integration/mcp_server.py:253`, `:776`).

> **Do not use `set_project_dev_mode`, `rag project dev-mode`, or `ProjectConfig.index_source_code`
> as a per-project tri-state.** Those names come from the superseded P1–P7 design (commits `0e2fd1f`..`ceb0d88`)
> and were replaced one commit later by `5fb10e8`. They do not exist in the shipped v2.7.0 interface.

A project in `docs` mode returns **no code chunks**; an empty `find_definition` / `search_project_context`
result is therefore *not* evidence that a symbol is absent. Check `project_status(project=...)` → `mode` first.
| `RAG_SECRET_ALLOWLIST` | `[]` | Globs to re-include specific secret-bearing files (default: none) |
| `RAG_QDRANT_HTTP_PORT` | `21500` | Managed-engine HTTP port. Env > config (`qdrant_http_port`) > default. |
| `RAG_QDRANT_GRPC_PORT` | `21501` | Managed-engine gRPC port. Same precedence. |
| `RAG_INSTANCE_ID` | auto | Names this installation's engine. Required (with non-default ports) to run a deliberate second managed instance. |
| `RAG_CLIENT_PROFILE` | unset | Which client profile the MCP process serves. Unset = owner (all tools, all projects). A named-but-absent id fails CLOSED. |

### Shared dependencies (catalog + per-project links)

A project that vendors a framework is mostly not its own code. A **shared
dependency** is a first-class object: declare the folder **once** in the
catalog, then **select** it from every project that uses it. It is indexed once
into a shared collection instead of copied into each project.

Manage it on the **Shared dependencies** page (`/dependencies`); select entries
per project under Projects → edit → *Shared dependencies*. In config:

```toml
[[dependencies]]                       # the catalog — declared once
id = "odoo-18"
name = "Odoo 18 core"
path = "C:\\TQ-WorkSpace\\odoo\\pearl-pixels-18\\odoo"   # absolute

[[projects]]
id = "khayrgate"
dependencies = ["odoo-18"]             # links, by id — many projects, one entry
```

**Two kinds of validity, deliberately separate.** *Catalog* validity (is it a
folder; is it already registered under another id?) is project-independent.
*Link* validity (may THIS project use it?) is not — a folder that is one
project's own root, or a parent of it, is a legal catalog entry and an illegal
link for that project. The project form shows those entries disabled **with the
reason** rather than hiding them.

**`dependency_paths` is a legacy input.** It still loads, and is adopted into
catalog entries + links at load time — then *consumed* (like
`index_source_code` → `mode`), so it never becomes a dead control whose
clearing silently does nothing. Two projects that declared the same folder
collapse to one entry: the dedup the old model reached only by luck.

**Indexed once means once.** A corpus that any project already links is reused,
not re-imported — `sync_frameworks(refresh=True)` forces a fresh import. The
completeness signal is the *link*, not a point count: linking happens strictly
after a successful import, so an interrupted run (points present, no link) is
correctly re-imported rather than stranded half-indexed.

**MCP:** `list_dependencies`, `add_dependency`, `set_project_dependencies`
(REPLACES the list), `remove_dependency` (confirm token + `cascade`). All are in
the `framework_management` capability group.

* **Off unless declared.** Empty for every project by default; nothing changes
  until someone opts in.
* **Requires `collection_strategy = "per_project"`** — a no-op in `shared` mode.
* **Declaring is a three-part move, all or nothing** (`owner.sync_frameworks`):
  the tree is excluded from the project scan, indexed into its own collection,
  and purged from the project's. The scanner excludes it *first*, so skipping
  the sync would delete the content from search entirely — every write path that
  touches `dependency_paths` must schedule `sync_frameworks`.
* **Un-declaring reconciles in reverse**: unlink, drop the corpus once no
  project links it (refused while one still does), re-index the project so the
  files return.
* **Identity.** Deduped by build identity — `build_id` when a detector finds one
  (Odoo `repos_heads`, git HEAD, npm, Python), otherwise by **resolved path**.
  Never by directory name: two projects each vendoring `<project>/odoo` would
  otherwise share one collection and read each other's code.
* **Rejected declarations:** the project root, any ancestor of it, and missing
  paths. Nested declarations collapse to the outermost.
* Search spans the project's own collection plus its linked corpora, and every
  result carries `scope` (`project` | `framework`) and `scope_source`.
* Framework corpora are **not** watcher-refreshed — they update on the next sync.

## Upgrade notes (2.6)

- **Code/config indexing is now opt-in** (`index_source_code` default `False`). If you indexed source on a prior `master` build, the first incremental run after upgrade treats those code files as deletions and purges them from Qdrant + the state DB. Set `RAG_INDEX_SOURCE_CODE=1` to keep indexing code, or run `rag rebuild` for a clean docs-only index.
- **Global `[ignore].patterns` now apply on the direct-API indexing path** (previously dropped when no `ignore_rules` was passed). A file that newly matches a configured ignore pattern is removed on the next incremental run.
- **Secret-bearing files are never indexed** (`.env*`, keys, `credentials*`, `secrets/`, …); use `secret_allowlist` / `RAG_SECRET_ALLOWLIST` to re-include specific paths.

## Entry Points

Defined in `pyproject.toml`:
- `rag` → `ragtools.cli:app` (Typer CLI)
- `rag-mcp` → `ragtools.integration.mcp_server:main` (MCP server direct)
