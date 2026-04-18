# Glossary

| | |
|---|---|
| **Owner** | TBD (proposed: docs lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

One-page domain vocabulary. Every term used elsewhere in this wiki should be defined here. If you find a term used in another page that isn't in this glossary, open a PR to add it.

## Terms

- **`.ragignore`** — Per-directory ignore file (gitignore syntax, including `!` negation). One of three ignore-rule layers. See [Architecture Decisions](Standards-and-Governance-Architecture-Decisions).

- **Admin panel** — The htmx + Jinja2 web UI served by the service at `127.0.0.1:21420` (installed) or `127.0.0.1:21421` (dev). No npm, no React.

- **`all-MiniLM-L6-v2`** — The embedding model. 384-dimensional, cosine-distance, normalized. Changing the model requires rebuilding the entire index.

- **Barometer mode** — *BLOCKED: definition required.* Referenced in runtime behavior but not defined in code or docs. Tracked as Q-1 on the [Open Questions](Development-SOPs-Documentation-Open-Questions) page.

- **Chunk** — A span of text within a Markdown file, split at heading boundaries (`##`, `###`, `####`) with paragraph and sentence fallback. Target 400 tokens, 100-token overlap. Each chunk produces one embedding and one Qdrant point.

- **Chunk UUID** — Deterministic chunk ID computed as `sha256(project_id::file_path::chunk_index)` formatted as a UUID string. Re-indexing the same file produces the same IDs (idempotent upsert).

- **Collection (`markdown_kb`)** — The single Qdrant collection holding all chunks for all projects. Project isolation is enforced via payload filter, not separate collections.

- **Confidence band** — Bucket assignment for a retrieval result: **HIGH** (score ≥ 0.7), **MODERATE** (0.5-0.7), **LOW** (< 0.5, but ≥ threshold). See [Confidence Model](Core-Concepts-Confidence-Model).

- **Content root** — The directory whose immediate subdirectories become projects. Controlled by `RAG_CONTENT_ROOT` (default `.`).

- **Data directory** — Where Qdrant files, SQLite state, and logs live. Dev: `./data/`. Installed: `%LOCALAPPDATA%\RAGTools\data\`. Detected by `config.py:get_data_dir()`.

- **Dual-mode CLI** — Service-aware CLI commands probe `127.0.0.1:<service_port>/health` (21420 installed / 21421 dev); if the service is up they forward over HTTP, otherwise they fall back to direct Qdrant. See [CLI Dual-Mode](Architecture-CLI-Dual-Mode).

- **Encoder** — The SentenceTransformer instance wrapping `all-MiniLM-L6-v2`. Thread-safe via `threading.RLock`. Held in memory by the service for its whole lifetime.

- **Full index** — `rag index --full`: drops and recreates the collection, re-embeds everything. Compare with incremental.

- **Heading-enriched text** — Chunk text with its heading hierarchy prepended (e.g. `Architecture > Backend\n\n...`) *only* at embedding time. The stored payload contains the raw text without headings.

- **Incremental index** — Default mode. Skips files whose SHA-256 matches the state DB; re-indexes changed files only.

- **Installed mode vs dev mode** — Installed: binary under `Program Files`, data in `%LOCALAPPDATA%\RAGTools\`, detected via `sys.frozen`. Dev: source checkout, data in `./data/`.

- **MCP (Model Context Protocol)** — The protocol that exposes tools to Claude CLI. Reg exposes `search_knowledge_base`, `list_projects`, `index_status`.

- **MCP proxy mode / MCP direct mode** — On startup, the MCP server probes the service. If reachable: proxy mode (thin HTTP client, instant startup). If not: direct mode (loads encoder + opens Qdrant, 5-10 s startup). Mode is locked for the session.

- **Payload filter** — Qdrant query filter on the keyword-indexed `project_id` payload field. How project isolation is enforced on a single collection.

- **Project / `project_id`** — An immediate subdirectory of the content root. Its name is the `project_id`. Directories starting with `.` or `_` are skipped.

- **QdrantOwner** — Singleton in the service that wraps the Qdrant client and encoder, serialized by `threading.RLock`. The only object allowed to touch Qdrant while the service is running.

- **Reset escalation** — Three levels of repair: **soft** (drop collection, rebuild), **data** (delete data dir), **nuclear** (delete everything including config). See [Reset Escalation](Architecture-Reset-Escalation).

- **Score threshold** — Minimum cosine similarity to include a result. Controlled by `RAG_SCORE_THRESHOLD` (default `0.3`). Below threshold = excluded entirely.

- **Service** — The long-running FastAPI + Uvicorn process on `127.0.0.1:21420` (installed default) or `127.0.0.1:21421` (dev default). Sole owner of the Qdrant data directory. See [Service Lifecycle](Architecture-Service-Lifecycle).

- **Single-process invariant** — Only one process at a time may open the Qdrant data directory. The service is that process when running. See [Single-Process Invariant](Core-Concepts-Single-Process-Invariant).

- **State DB** — `data/index_state.db` (SQLite). Tracks per-file SHA-256 and chunk counts so incremental indexing can skip unchanged files.

- **Startup task** — Windows Task Scheduler entry that launches the service on login. Registered via `rag service install`.

- **Watcher** — Daemon thread in the service using the `watchfiles` library. Monitors enabled projects for `.md` changes, debounces 3 s, triggers incremental reindex.
