# Standards & Governance: Architecture Decisions

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Source of truth** | `docs/decisions.md` |

Fifteen decisions, all **Final (locked 2026-04-06)**, govern Phase 1+ implementation. They were made after a critical architecture review that identified the Qdrant single-process constraint as the central design driver.

**To read the full text of any decision, open `docs/decisions.md` — this page is an index.**

## Summary table

| # | Decision | Default | Wiki cross-references |
|---|---|---|---|
| 1 | Single-process model | Service owns Qdrant exclusively | [Single-Process Invariant](Core-Concepts-Single-Process-Invariant), [Service Lifecycle](Architecture-Service-Lifecycle) |
| 2 | Config format & location | TOML, env > `%LOCALAPPDATA%` > `./ragtools.toml` > defaults | [Configuration Resolution Flow](Architecture-Configuration-Resolution-Flow) |
| 3 | Ignore rules | `.ragignore` + `pathspec` + 3-layer precedence | [Watcher Flow](Architecture-Watcher-Flow), [Indexing Pipeline](Architecture-Indexing-Pipeline) |
| 4 | Service port | `127.0.0.1:21420` (installed) / `21421` (dev), no public binding | [Service Lifecycle](Architecture-Service-Lifecycle) |
| 5 | Localhost auth | None | [HTTP API](Reference-HTTP-API) |
| 6 | Frontend | htmx + Jinja2, no JS build step | (admin panel — dedicated Architecture page not yet authored) |
| 7 | MCP proxy | Probe at startup; proxy if service up, direct otherwise; mode locked for session | [MCP Proxy Decision](Architecture-MCP-Proxy-Decision), [Reference: MCP Tools](Reference-MCP-Tools) |
| 8 | Service lifecycle on Windows | `CREATE_NO_WINDOW` + `DETACHED_PROCESS` + PID file + HTTP shutdown | [Service Lifecycle](Architecture-Service-Lifecycle) |
| 9 | Logging | `RotatingFileHandler`, 10 MB × 3 backups, `{data_dir}/logs/service.log` | [File Layout](Reference-File-Layout), [Install Decision Tree](Architecture-Install-Decision-Tree) |
| 10 | Data directory | Dev `./data/`, installed `%LOCALAPPDATA%\RAGTools\` (Windows) or `~/Library/Application Support/RAGTools/` (macOS) | [Install Decision Tree](Architecture-Install-Decision-Tree), [File Layout](Reference-File-Layout) |
| 11 | Startup strategy | Task Scheduler (`rag service install`), not Startup Folder, not Windows Service | [Install Startup Task](Operational-SOPs-Service-Install-Startup-Task) |
| 12 | Encoder thread safety | `threading.RLock` in `QdrantOwner` serializes all encode + Qdrant ops | [Single-Process Invariant](Core-Concepts-Single-Process-Invariant), [Indexing Pipeline](Architecture-Indexing-Pipeline) |
| 13 | Dependencies | `pathspec`, `tomli`/`tomli-w`, `fastapi`, `uvicorn`, `httpx`, `jinja2` | `pyproject.toml` |
| 14 | CLI dual-mode | Service-aware commands probe `/health`; forward on success, fall back on failure (except where noted — see [CLI Dual-Mode](Architecture-CLI-Dual-Mode)) | [CLI Dual-Mode](Architecture-CLI-Dual-Mode) |
| 15 | Watcher unavailable paths | Skip, warn, retry every 60 s; never crash | [Watcher Flow](Architecture-Watcher-Flow) |

## Changing a decision

1. **Do not edit** `docs/decisions.md` after the locked date.
2. To change a decision, add a new decision record (ADR-16+) in `docs/decisions.md` that explicitly supersedes the earlier one.
3. Update this page's summary table and flip the superseded row's wiki cross-references to point at the new ADR.
4. Update any wiki pages that backref the old decision.

## When a new decision is warranted

Open an ADR when the change:
- Alters the single-process invariant.
- Changes the default service port or bind address.
- Introduces a new storage backend or swaps the embedding store.
- Alters the MCP proxy/direct contract (timeouts, retry counts, mode-switch rules).
- Adds network-exposed surfaces.
- Adds JS tooling or a build step to the admin panel.

Smaller refactors within an existing decision's scope do not require an ADR. Use the ADR format when a future reader would ask "why is it like this?" — the ADR answers that question.

## Related

- [Documentation Standards](Standards-and-Governance-Documentation-Standards) — how to maintain decision references in SOPs.
- [Review and Merge Standards](Standards-and-Governance-Review-and-Merge-Standards) — PR process for ADR changes.
