# Wiring managed Qdrant + per-project / framework collections into the real runtime

**Status:** plan — implementation follows in this same worktree
**Scope:** the isolated dev environment only. No live service, no live data, no Git, no release.
**Owner-facing symptom this closes:** the dashboard warning
*"The index is larger than this storage engine handles well — 88,825 points, above the local-mode limit of 20,000."*

---

## 1. Measured current state (2026-07-25, from the running dev service + source)

The architecture already exists as **libraries with tests**. Nothing calls them.

| Component | Exists | Reached by the running service |
|---|---|---|
| `storage.resolve_backend` → `embedded \| managed \| external` | yes | **no** — `storage_backend` defaults `"embedded"` |
| `storage_managed.QdrantSupervisor` (spawn, `/readyz` gate, version pin 1.15.5) | yes | no |
| `service/managed_qdrant.{plan_managed_startup,start_managed_qdrant}` | yes | **called at boot** (`app.py:141`) but `plan_managed_startup` returns `should_start=False` because the mode is `embedded` |
| `qdrant.exe` 1.15.5 on disk | yes (84 MB) | no process; nothing on 6333/6334/21500/21501 |
| `identity.project_collection_name` / `framework_collection_name` | yes | **0 references** in `owner.py` / `searcher.py` |
| `registry.ProjectRegistry` (uuid → collection, archive/remove verbs) | yes | never constructed by the service |
| `registry.FrameworkRegistry` + `framework_collections_for()` | yes | **0 consumers** |
| `indexer.index_by_strategy` / `run_per_project_index` | yes | **0 callers in product code** |
| `collection_strategy` setting | yes | defaults `"shared"` |
| Multi-collection search | **does not exist** — `searcher.py:146` hardcodes one collection | — |

**The blast radius:** `settings.collection_name` is read at **38 sites across 11 files**.

```
13  service/owner.py          7  indexing/indexer.py      4  watcher/observer.py
 4  integration/mcp_server.py 2  service/routes.py        2  service/pages.py
 2  cli.py                    1  service/map_data.py      1  service/job_handlers.py
 1  retrieval/searcher.py     1  retrieval/codegraph.py
```

### 1.1 Why per-project collections alone would NOT close the warning

Two independent problems, and the plan must fix both:

- **Engine.** `QdrantClient(path=…)` is a pure-Python reimplementation: brute-force scan, no HNSW, payload indexes are a silent no-op, snapshots raise, one exclusive process lock. The 20,000 guidance is a property of *this engine*. More collections do not change it. Only a real server does.
- **Scan width.** One shared collection means a query scoped to `rag-docs` still scans all 88,825 points. Per-project collections cut that to 1,163 — a real latency win, and the precondition for framework dedup.

Measured on this machine at 50k points: **search 104.0 ms → 8.5 ms (12×)**, **index 485.7 s → 15.8 s (31×)**.

---

## 2. Target architecture

```
                    ┌──────────────────────────────────────┐
   query(project)   │        CollectionRouter              │
  ─────────────────▶│  read_collections(project) → [own,   │
                    │                    fw_a, fw_b]       │
   index(project)   │  write_collection(project)  → own    │
  ─────────────────▶│  all_collections()          → [...]  │
                    └───────────┬──────────────────────────┘
                                │
              ┌─────────────────┴──────────────────┐
              ▼                                    ▼
   strategy = "shared"                  strategy = "per_project"
   every call → markdown_kb             proj_<uuid>  +  fw_<name>_<digest>
   (v2 behaviour, byte-identical)       (registry-backed)
```

**One seam, not 38 edits.** Every site stops asking `settings.collection_name` and asks the router. In `shared` mode the router returns `markdown_kb` for everything, so the legacy path is preserved *by construction* rather than by a parallel code path.

**Storage is orthogonal.** `storage_backend` (embedded/managed/external) and `collection_strategy` (shared/per_project) are independent axes; the router does not care which engine it is on, and the engine does not care how many collections exist. This matters because the fallback when `qdrant.exe` is missing must still be a *working* system.

### 2.1 Collection naming (already implemented in `identity.py`)

| Kind | Name | Keyed by |
|---|---|---|
| Project | `proj_<uuid-no-dashes>` | immutable UUID → survives rename **and** path move |
| Framework | `fw_<slug>_<16-hex>` | `build_id` when known, else `(name, version, edition)` |

Framework dedup is the economic point: a vendored Odoo core is ~92–99 % of `khayrgate`'s 87,660 chunks. Indexed **once** per build identity, then *linked* to every project that uses it — instead of re-embedded per project.

### 2.2 Retrieval semantics

A query scoped to project P reads `[P.collection] + framework_collections_for(P.uuid)`, merges by score, and applies `top_k` **after** the merge. Each hit carries its origin so the UI and the agent can tell "your code" from "framework reference" — this is what `source_class` already encodes, now reinforced by which collection answered.

Fail-closed scope (`retrieval/scope.py`) is unchanged and still authoritative: no project → no search.

---

## 3. Work packages

### W1 — `CollectionRouter` (new: `src/ragtools/collections.py`)

```python
class CollectionRouter:
    def write_collection(project_id) -> str
    def read_collections(project_id | None) -> list[str]
    def all_collections() -> list[str]
    def ensure(project_id, dimension) -> str
    def strategy -> "shared" | "per_project"
```

- `shared`: every method returns/uses `settings.collection_name`.
- `per_project`: resolves through `ProjectRegistry`; unknown project → `KeyError`, never a silent fallback to the shared collection (that would leak one project's vectors into another's answers).
- Constructed once by `QdrantOwner`, passed down. No global.

### W2 — Owner: writes (13 sites)
`run_full_index`, `run_incremental_index`, `rebuild`, `_run_full_index_inner`, `delete_project_data`, `reindex_project`, `get_status`, `_compute_status`.
Per-project indexing walks the registry; incremental keeps its `tracked − current` delete-awareness **per collection**. `get_status` aggregates point counts across `all_collections()`.

### W3 — Retrieval: multi-collection read
`Searcher.search` gains `collections: list[str]`; queries each and merges. `codegraph.py` and `dev_pipeline.py` inherit it. `point_to_search_result` already exists as the shared mapper — use it (the current `Searcher.search` duplicates that mapping inline; delete the duplicate).

### W4 — Framework detection + linking
Detect a vendored framework root from `ProjectConfig.dependency_paths` (field already exists, currently unused). Register in `FrameworkRegistry`, link to the project, index the corpus once into `fw_*`. Odoo detector reads `repos_heads` for the `build_id`; absent → `(name, version, edition)` fallback.

### W5 — Watcher (4 sites)
`observer.py` must route each changed file to its project's collection. Today it scans `content_root` globally and writes to one collection. Per-project: resolve `pid → write_collection(pid)`. A file under a linked framework root is **ignored by the project watcher** — framework corpora are refreshed by build identity, not by file events, or they would be re-indexed N times.

### W6 — MCP (4 sites)
`index_status` reports per-collection counts. `search_knowledge_base` / `search_project_context` / `find_definition` go through the router. `project_status` gains `collection`, `framework_collections`, `points`. Envelope shape unchanged.

### W7 — Service API + UI
- `/api/status` → `collections: [{name, kind, points, project}]`, `storage: {mode, engine_version}`.
- Diagnostics page: storage backend, engine version, per-collection table.
- Projects table: collection name + linked frameworks.
- **Scale warning re-derived from `Capabilities`**, not a hardcoded 20 000: warn only when `capabilities.hnsw is False` (i.e. actually on the brute-force engine). On managed, the warning disappears because the limitation is gone — which is the whole point.

### W8 — Config & security
- `storage_backend`, `storage_url`, `storage_api_key`, `collection_strategy` exposed via `/api/config` (read) — `storage_api_key` **write-only, never returned**.
- Client profiles (`profile_store`) scope by project; enforcement must map to *collections* so a scoped client cannot read another project's collection. This is the security-critical edit: today scoping is a payload filter; with per-project collections it becomes collection selection, and a bug there is a cross-project leak rather than a wrong-filter.
- Managed server binds 127.0.0.1 only (already in `generate_qdrant_config`), telemetry off, and refuses a synced/FUSE storage path.

### W9 — Migration (`shared → per_project`)
One-way, resumable, verified:
1. Pre-flight: `check_migration_model` (embedding model must match), disk space, engine reachable.
2. `sync_projects_from_config` → registry mirrors live projects (already implemented, idempotent).
3. Per project: create collection, re-index from source, **verify by point count**, only then record progress.
4. Legacy `markdown_kb` is **retained** until the operator drops it explicitly (verb 3). Rollback = flip `collection_strategy` back to `shared`.
5. Quality gate: `compare_to_baseline` must not regress `file_recall_at_5` / `mean_file_mrr`.

### W10 — Failure & recovery
| Failure | Required behaviour |
|---|---|
| `qdrant.exe` missing / unsupported platform | fall back to embedded, `/health` degraded with the reason, service still boots |
| Managed server dies mid-run | job fails loudly, marked `INTERRUPTED` on restart, not reported as success |
| Version mismatch vs pin | refuse to trust (`ManagedStartError`) — already implemented |
| Migration interrupted | resumable; a partially-built collection is re-verified by count, never assumed |
| Registry ↔ config drift | `sync_projects_from_config` reconciles; orphan collection surfaced, never auto-deleted |
| Port in use | fail with the port named, do not silently pick another |

### W11 — Legacy removal
Delete only what the new architecture *fully* replaces:
- inline result-mapping duplicated in `Searcher.search` (use `point_to_search_result`)
- `pages.ui_projects` / `ui_status` / `ui_watcher` — dead fragments superseded by the dashboard ones (verify no route/template references first)
- `index_by_strategy`'s "shared" branch collapses into the router once every caller routes
- **Keep:** `collection_name` setting (names the shared collection, still the default), `run_full_index`/`run_incremental_index` public signatures, all MCP tool names and envelopes, `/api/*` response keys — these are contracts.

### W12 — Test matrix
| Layer | Coverage |
|---|---|
| Unit | router in both strategies; unknown project raises; naming stability across rename+move |
| Integration | full + incremental per-project; widen/narrow mode; delete-awareness per collection |
| Retrieval quality | eval harness vs baseline; **cross-project leak test** — a query scoped to A must never return B's chunk |
| Security | scoped client profile cannot read an unlinked collection; `storage_api_key` never serialized |
| MCP | every tool under both strategies; envelope unchanged |
| Migration | shared→per_project verified by count; interrupted-and-resumed; model-mismatch refused |
| Failure | no binary → embedded fallback; server killed mid-index → job FAILED not SUCCEEDED |
| Browser | Playwright: storage mode + engine version on Diagnostics, per-collection table, warning **absent** on managed, present on embedded |

---

## 4. Order of execution

W1 → W2 → W3 (core path works) → W9 migration → W5 watcher → W6 MCP → W7 UI → W8 security → W4 frameworks → W10 failures → W11 removal → W12 full suite.

Each step keeps `collection_strategy="shared"` green, so the legacy behaviour is provably intact at every commit boundary.

## 5. Gates

- **G1** shared-mode suite stays 1385-green after the router lands.
- **G2** per-project mode: index → search → correct results, zero cross-project leakage.
- **G3** managed engine: `/identity` reports `mode: managed`, `engine_version: 1.15.5`.
- **G4** the scale warning is **gone** on managed and still correct on embedded.
- **G5** migration verified by per-collection point count; quality gate not regressed.
- **G6** Playwright green; 0 console errors; contrast audit still 0 failures.
