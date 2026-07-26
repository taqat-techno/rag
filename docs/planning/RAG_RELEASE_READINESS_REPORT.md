# Release-readiness report — ragtools v3 architecture

**Date:** 2026-07-26  **Branch:** `rag-v3-dev` (worktree, uncommitted)
**Verdict:** **Release candidate with two verified blockers to platform claims** (§10).
Nothing committed, pushed, tagged or published.

---

## 1. Features completed from the plan

| Area | Status | Evidence |
|---|---|---|
| Managed Qdrant engine (pinned 1.15.5, no Docker) | **done** | `/identity` → `mode: managed, engine_version 1.15.5`; ports 21510/21511 |
| Collection-per-project (`proj_<uuid>`) | **done** | live: 2 project collections, 88,970 points |
| Framework corpora (`fw_<slug>_<digest>`) | **done** | live 2-project run → **1** shared collection |
| `CollectionRouter` — one seam for "which collection" | **done** | replaced 38 hardcoded sites across 11 files |
| Scale warning derived from engine capability | **done** | `scale=ok` at 88,970 pts on HNSW; still fires on embedded |
| `dependency_paths` end-to-end | **done** | exclusion + corpus indexing + adoption purge |
| Dependency path validation | **done** | project-root/ancestor refused, symlinks + `..` normalised, nested collapsed |
| Search scope metadata | **done** | `SearchResult.scope` = `project\|framework`, `scope_source` |
| Streaming index (bounded memory) | **done** | 2.46 GB → **~1.2 GB flat** on 38k files |
| Delete-aware full index + stale-before-upsert | **done** | both paths |
| Index identity guard (migration safety) | **done** | caught a real 27,895-chunk divergence |
| Process-wide index mutex | **done** | watcher/job double-index eliminated |
| Storage-aware `/health` | **done** | proven by killing the engine live |
| Batched deletes + upserts | **done** | TIME_WAIT flat 50–64 (was exhausting 16,384) |
| Durable jobs + interruption recovery | **done** | `interrupted` recorded, resumable |
| Stale-toast suppression / SSE replay boundary | **done** | first connect starts at *now* |
| Detectors: Odoo (`repos_heads`/git), npm, Python, generic | **done** | `tests/test_frameworks.py` |

## 2. Architecture and runtime paths in force

```
config ─► Settings(storage_backend, collection_strategy, dependency_paths)
            │
            ├─ storage.resolve_backend ──► embedded | managed | external
            │      managed ⇒ QdrantSupervisor(qdrant.exe, /readyz gate, version pin)
            │
            ├─ collection_router.build_router ──► CollectionRouter
            │      shared      ⇒ markdown_kb for everything (v2, unchanged)
            │      per_project ⇒ proj_<uuid> (+ linked fw_* on the READ path)
            │
   index ─► QdrantOwner.run_{full,incremental}_index
            │  index mutex → identity guard → _purge_missing
            │  → _stream_index (bounded windows)
            │      → _flush_window: chunk ▸ redact ▸ encode ▸ delete-stale ▸ upsert ▸ state
            │  → _drop_stale_vectors → stamp identity
            │
   deps  ─► QdrantOwner.sync_frameworks
            │  resolve_dependency_roots → describe_dependency → register (dedup by build)
            │  → _index_framework_corpus (streamed) → link → _purge_dependency_from_project
            │
  search ─► resolve_scope (fail-closed) → router.read_collections(own, *frameworks)
            → Searcher.search(collections, collection_scoped) → merge → scope-tagged results
```

The CLI indexer, the service watcher and the MCP direct mode all resolve
collections through the same router; there is no second write path.

## 3. Removed legacy / dead structures

| Removed | Why |
|---|---|
| `indexer.index_by_strategy` | dispatcher for a layout that is now a router answer |
| `indexer.run_per_project_index` | a **second** per-project indexer with no state tracking, no incremental, no delete detection, no progress/cancel, no redaction |
| Two-phase accumulate-then-write in both indexers | replaced by the streaming core |
| Inline result mapping in `Searcher.search` | duplicated `point_to_search_result` and had drifted |
| Sticky save bar + second "Save tool access" button | one save action |
| `.qdrant-initialized`, `ragtools.toml.bak` | local artifacts; `.gitignore` hardened |
| 147 inline `style=` attributes | design system |

**Preserved contracts:** all MCP tool names + `{ok, mode, as_of, data|error}` envelopes,
`/api/*` response keys, `shared` strategy as default, `embedded` backend as default,
`collection_name` setting, `run_full_index`/`run_incremental_index` signatures and
stat keys, fail-closed scope resolution, confirm-token rules.

## 4. Framework dependency & deduplication evidence

Live, two projects vendoring an identical Odoo build:

```
projects synced       : 2
framework collections : 1   ['fw_odoo_746f9b6d65def535']
created flags         : [True, False]        ← second REUSED, not re-indexed
  proj_b35b3a…  project  alpha        1
  fw_odoo_746f…  framework  —          7     ← corpus stored ONCE
  proj_c3115c…  project  beta         1
framework files in project collections: 0
alpha sees framework  : 7 hits, scope_source=fw_odoo_746f9b6d65def535
alpha sees beta code  : 0  (isolated)
```

Identity separation proven for: version, edition (CE vs EE), build id, and
checkout-vs-packaged. Build id is authoritative when present (documented, tested).

## 5. Before / after

| | Before | After |
|---|---|---|
| Collections | 1 (`markdown_kb`) | 2 project + N framework |
| Points (real project) | 88,825 state / **60,930 qdrant** (27,895 missing) | **88,970 / 88,970 — reconciled** |
| Engine | embedded, pure-Python brute force | managed 1.15.5, HNSW + payload indexes |
| Scale banner | "above the 20,000 limit" | absent (limitation removed) |

## 6. Measurements

| Metric | Before | After |
|---|---|---|
| Peak RSS, 38k-file re-index | **2.46 GB, climbing** | **~1.2 GB, flat** |
| `/health` during indexing | **12 s timeouts, all endpoints** | **200 in 1.3–2.6 ms** |
| Progress reporting | `0/None` for ~15 min | `342 → 38,165 / 38,317` |
| TIME_WAIT during index | exhausted 16,384 (WinError 10048/10053) | **50–64** |
| Search @ 88,970 pts | ~2.1 s (embedded, measured earlier) | **39–54 ms** (418 ms cold) |
| Index outcome | port exhaustion / hang | **succeeded** |

## 7. Automated + browser results

* **Full suite: 1554 passed, 14 skipped, 0 failures** (from 1350 at session start).
* **Playwright: 8/8** against the live managed/per-project panel — storage engine
  surfaced, per-collection table, scale warning matches engine, per-project search
  isolation driven through the real UI, 0 console errors.
* Durable job log: last two index jobs `succeeded`; one historical `interrupted`
  correctly recorded; **no new failures**.

New suites this phase: `test_dependency_architecture` (24), `test_frameworks` (19),
`test_index_streaming` (10), `test_storage_health` (7), `test_index_exclusion` (6),
`test_upsert_batching` (13), `test_index_identity` (14), `test_registry_threading` (7),
`test_sse_replay_boundary` (5), `test_settings_single_save` (11), `test_config_isolation`.

## 8. Cross-platform status

* Static audit: **no hardcoded Windows paths** in any new module
  (`frameworks`, `collection_router`, `index_identity`, `registry`, `storage`,
  `storage_managed`, `managed_qdrant`).
* Platform dispatch confined to existing adapters (`devenv`, `process`, `startup`,
  `supervisor`, `tray_startup`, `watchdog`, `notify`, `run`).
* Qdrant asset map covers **windows/darwin/linux × x86_64/arm64**, and refuses
  (rather than guesses) where no build exists.
* Path handling uses `Path.resolve()` throughout — symlinks, junctions, `..`,
  drive-letter case all collapse to one identity (tested).
* **Not executed on Linux or macOS** — see §10.

## 9. Fresh install / upgrade

**Not performed** — see §10.

## 10. Remaining verified blockers

1. **No Linux/macOS execution.** Everything above ran on Windows only. The code
   is platform-clean by inspection and the asset map is complete, but "Windows,
   Linux and macOS are first-class" is currently an *unverified claim*. No CI
   runner or non-Windows environment was available in this session.
2. **No fresh-install or upgrade test.** No package was built and no upgrade from
   the previous release was exercised. The in-place migration path *is* verified
   (identity guard forced and completed a full re-index, reconciled exactly), but
   installer-level behaviour is untested.
3. ~~**Unlink is not implemented.**~~ **CLOSED** — `sync_frameworks` now
   reconciles both directions (`owner._release_frameworks`): an un-declared root
   is unlinked, the corpus is dropped once no project links it (refused while
   one still does), and the project is re-indexed so the files return to its own
   collection. Verified live, round trip, on `rag-docs`: 77 files → declare →
   10 + a 784-point corpus → un-declare → **77 files / 1,163 chunks restored,
   corpus dropped, nothing orphaned.**
4. **Framework corpora are not refreshed by the watcher.** They are indexed at
   `sync_frameworks` time; file changes inside a framework root are not picked up
   until the next explicit sync (by design — refresh is per build identity — but
   there is no scheduled refresh yet).

Blockers 1, 2 and 4 cannot be closed from this machine in this session; all are
scoped, understood, and independent of the shipped code paths.

## 11. Addendum — wiring `dependency_paths` to the UI

The engine was complete and **unreachable**: the field was absent from both API
request models and both project forms, and `sync_frameworks` had no production
caller at all — only tests. Closing that exposed four defects that only appear
once a user can actually drive the feature:

| Defect | Why it mattered | Where |
|---|---|---|
| Generic corpora keyed by **directory name** | Two projects each vendoring `<project>/odoo` — the *common* Odoo layout, since the checkout root is the project itself and is correctly refused — merged into ONE collection, so project A's search returned project B's code | `frameworks.describe_dependency` — now keyed by resolved path |
| `scope` computed then **dropped** by the API | `SearchResult.scope` distinguished project from framework, but both hand-listed serializers omitted it, so "results are labelled" was untrue at the boundary | `owner.search_formatted`, `owner.search_project_context` |
| A `busy` index **no-op recorded as success** | The restore-after-unlink job ran while the watcher (restarted by the same config write) held the index mutex; zeros were logged as "succeeded" and the files stayed missing from search. No retry layer exists underneath | `job_handlers.index_handler` — now waits for the lock, fails loudly at the ceiling |
| An omitted form field read as "clear" | Any caller that did not mention dependencies would erase them; it broke two unrelated mode tests immediately | `pages._form_text` |

The first three were found by *using* the feature, not by reading it — the
generic-name collision and the dropped label were both invisible to the test
suite that shipped with the engine.

**Live round trip** (dev service `:21455`, real project): declare in the panel →
corpus indexed and linked (784 points) → project purged 77 → 10 files → search
returns the corpus tagged `framework` with `scope_source=fw_wiki-src_…` →
un-declare → released and dropped → **77 / 1,163 restored exactly**. Final dev
index 38,453 files / 89,036 points, `scale=ok`, zero orphaned collections.

**Suite:** **1580 passed, 14 skipped, 0 failures** (`tests/test_dependency_ui.py`, 26 new).
