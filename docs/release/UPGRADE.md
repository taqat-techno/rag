# Upgrading to ragtools v3.0.0

## What happens to your data

**Your projects, configuration and source files are preserved. Your search index
is rebuilt.**

v3 changes two things that make the old index unusable rather than convertible:
one shared collection becomes one collection per project, and the storage engine
changes. There is no safe transformation between those layouts, and the index
identity guard would refuse to trust the old state database in any case.

| Item | What happens |
|---|---|
| `config.toml` — projects, ids, paths, modes, ignore rules | **migrated** |
| Per-project `dependency_paths` | **migrated** into the shared-dependency catalog |
| Client profiles | **migrated** where the schema allows, otherwise recreated with a warning |
| Activity and audit history | **preserved** |
| `index_state.db` | **rebuilt** |
| Vector collections | **rebuilt** by a full re-index |
| Your source projects and repositories | **never touched** |

The previous data directory is **renamed, not deleted** — `RAGTools.pre-v3-<version>` —
and kept until you run `rag upgrade commit`.

## Before you start

```
rag upgrade scan        # what is on this machine (changes nothing)
rag upgrade plan        # what would happen (changes nothing)
```

`plan` runs every pre-flight gate and reports all blockers at once:

* **disk** — the migration writes the new store while the old one still exists,
  so it needs roughly 3× the estimate. For a 147,000-point index that is ~4 GB.
* **memory** — ~2 GB available.
* **port** — a busy port is expected during an upgrade (your own old service
  holds it); a *foreign* listener is not.
* **projects** — a project whose folder has moved is reported and skipped, never
  removed from your configuration.
* **model** — a changed embedding model invalidates every vector, and finding
  that out after re-indexing is expensive.

## Running it

```
rag upgrade apply
```

Order: stop tray → stop MCP proxies → stop watcher → drain jobs → stop service →
stop supervisor → stop managed Qdrant → verify nothing survives → back up →
install → migrate config → start store → re-index each project → reconcile.

Progress is per project and durable. If the machine reboots mid-way,
`rag upgrade resume` continues from the last completed step.

## Can I go back?

`rag upgrade status` always answers this. There is exactly one boundary:

* **Before the first write to the new store** — full rollback:
  `rag upgrade rollback` restores the previous binaries, autostart registration
  and data directory.
* **After it** — forward-only. Recovery is `rag upgrade resume` or
  `rag upgrade repair`. The previous data directory is still on disk, so a
  manual salvage remains possible until you run `rag upgrade commit`.

## When it reports failure

An upgrade reports success only when **every** project reconciles, or is listed
by name with a recovery command. There is no "mostly migrated" state.

```
UPGRADE INCOMPLETE — 1 gate(s) failed for: khayrgate
  [fail] counts (khayrgate): state DB 88,825 vs Qdrant 60,930 (delta 27,895)
        -> rag index --full --project khayrgate
```

Run the named command, then `rag upgrade resume`.

## What is removed

Superseded components, all of them replaced by the new architecture:

* the `RAGTools Watchdog` scheduled task — restart-on-failure is now a native
  capability of Task Scheduler, systemd and launchd
* `RAGTools.vbs` and `RAGTools-Tray.vbs` Startup-folder launchers
* duplicate `PATH` entries (this machine had the install directory **16 times**)
* stale PID files and old program binaries

Development environments — `RAGTools-dev`, any `rag-v3-*` directory, any
non-`installed` profile — are detected and **never** touched.
