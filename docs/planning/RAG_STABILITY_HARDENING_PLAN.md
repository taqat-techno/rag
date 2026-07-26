# Stability hardening — making ragtools a product you can leave running

**Scope:** the isolated dev worktree. No live service, no live data, no release.
**Driver:** the open items after the collection-architecture work, ranked by what
actually threatens a long-running install.

---

## 1. What is actually unstable (measured, 2026-07-25)

| # | Symptom | Measured | Severity |
|---|---|---|---|
| S1 | Index holds every chunk in memory before writing | **2.46 GB** RSS for 38,286 files; points flat for ~15 min | **Highest — this is an OOM on a bigger project** |
| S2 | Two transient ~10 s unresponsive windows during encode | `/health` 000 at 10 s, twice, recovered | Medium |
| S3 | `run_full_index` leaves vectors for files deleted from disk | orphans persist until a rebuild | Medium (silent wrong results) |
| S4 | Framework auto-detection from `dependency_paths` missing | field exists, nothing reads it | Feature gap |

S1 is the one that decides whether this is a product. Everything else is
comfort. A 38k-file project is not large; the same shape at 150k files does not
fit in memory at all, and the failure mode is an OOM kill mid-index — which,
thanks to the identity guard, at least no longer corrupts state, but still
leaves the index permanently incomplete.

### 1.1 Why memory is unbounded today

Both indexers are two-phase:

```
phase 1   for EVERY file:  read -> hash -> chunk -> append to `pending`
phase 2   for windows of `pending`:  encode -> upsert -> record state
```

`pending` holds the full text of every chunk of every file — the entire corpus,
in Python objects, before a single vector is written. Peak memory is therefore
`O(corpus)`, and no work is durable until phase 2 starts.

That also explains the flat point count: nothing is written for the first ~15
minutes because phase 1 has not finished.

---

## 2. Target: stream in bounded windows

```
pass A (cheap)   list files, resolve relative paths          -> current_paths
                 (needed up-front only to detect deletions)
deletes          tracked - current, batched per collection
pass B (stream)  for each WINDOW of N files:
                     read -> hash -> chunk          (outside the lock)
                     encode -> delete stale -> upsert -> state   (inside)
                     drop the window; next
```

Peak memory becomes `O(window)` — one batch of files — regardless of corpus
size. Work becomes durable every window, so an interruption loses at most one
window rather than everything.

`_INDEX_BATCH_SIZE` already exists and already governs the encode batch; the
change is to make it govern *chunking* too, instead of chunking everything
first.

### 2.1 One core, two entry points

`run_full_index` and `run_incremental_index` are ~90 % the same code. After the
restructure they differ only in a predicate:

| | full | incremental |
|---|---|---|
| which files are work | all | hash changed (or the identity guard distrusts the state) |
| delete-detection | **now yes** (S3) | yes |
| stats keys | `files_indexed` | `indexed` / `skipped` / `deleted` |

So: one `_stream_index(...)`, two thin wrappers that keep their existing return
shapes (those are contracts — `/api/index`, MCP, CLI and ~40 tests read them).

### 2.2 Stale-before-upsert, always

Chunk IDs are deterministic (`sha256(project::path::index)`), so re-indexing an
unchanged file overwrites cleanly. But a file that **shrank** leaves its
higher-index chunks orphaned. The incremental path already deletes stale chunks
before upserting; the full path does not. Doing it in the shared core fixes S3
for both, and it is free — the delete is already batched per collection.

---

## 3. Work packages

- **H1 — stream the index core.** `_flush_window` (chunk → encode → upsert →
  state, bounded) + `_stream_index` driving it. Both public methods delegate.
- **H2 — full index becomes delete-aware.** Falls out of H1; keep the return
  shape.
- **H3 — measure S2 after H1.** The stalls may simply be memory pressure/GC. Do
  not "fix" what H1 removes; re-measure first.
- **H4 — framework auto-detection.** Read `ProjectConfig.dependency_paths`,
  register the corpus, link the project, index once. Odoo detector reads
  `repos_heads` for the dedup `build_id`.
- **H5 — test it properly** (below).

## 4. Test strategy

Peak-RSS assertions are flaky, so the streaming property is tested
**structurally and deterministically**, plus a generous memory sanity check:

| Test | Proves |
|---|---|
| call-order spy: `chunk … encode … chunk … encode` | work is interleaved, not accumulated |
| max live chunk-set size ≤ window | peak is `O(window)` |
| `tracemalloc` peak: 800 files vs 200 files | peak does not scale with corpus |
| interrupted mid-window → next run completes | durability per window |
| full index deletes removed files | S3 |
| shrunk file leaves no orphans (both paths) | stale-before-upsert |
| identical results streamed vs. legacy | no behaviour change |
| framework detected → linked → searchable | H4 |
| full suite + Playwright + live 88k re-index | no regression at scale |

## 5. Gates

- **G1** full suite green, shapes unchanged.
- **G2** peak memory for a 4× larger corpus grows < 2×.
- **G3** live re-index of 38,286 files completes with `/health` responsive
  throughout and state == Qdrant.
- **G4** no new job failures in the durable log.
