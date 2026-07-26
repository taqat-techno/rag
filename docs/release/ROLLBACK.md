# Rollback and recovery

## Which side of the boundary am I on?

```
rag upgrade status
```

There is exactly one boundary — the first write to the new store — and this
command always states which side of it the machine is on. That question must
never require reading code.

## Before the boundary: full rollback

```
rag upgrade rollback
```

Restores the previous binaries, the previous autostart registration and the
previous data directory. Nothing was written to the new store, so nothing is
lost.

## After the boundary: forward-only

The new store has been written to. Rollback would mean discarding it while the
old index has already been superseded, so the supported paths are:

```
rag upgrade resume      # continue from the last completed step
rag upgrade repair      # re-run reconciliation and fix what failed
```

The previous data directory is retained as `RAGTools.pre-v3-<version>` until you
run `rag upgrade commit`. If you must return to v2, install v2 and point it at
that directory.

## Specific failures

| Symptom | Action |
|---|---|
| Counts disagree for one project | `rag index --full --project <id>`, then `rag upgrade resume` |
| Framework files inside a project collection | `rag index --full --project <id>`, then `rag upgrade repair --frameworks` |
| Two collections for one framework build | `rag upgrade repair --frameworks` |
| **Any cross-project leakage** | **Do not release or continue.** This is an isolation failure, not a migration failure |
| Retrieval quality regressed | Keep the previous data directory; investigate before `rag upgrade commit` |
| Qdrant will not start | `rag doctor`; fall back with `RAG_STORAGE_BACKEND=embedded` |
| Interrupted, state unclear | `rag upgrade status` then `rag upgrade resume` — resuming is idempotent |

## Disaster recovery

If the machine is in a mixed state and nothing else works:

1. `rag upgrade scan` — enumerate what is actually present
2. `rag upgrade uninstall` — remove every registration and binary
3. Reinstall
4. Point at the retained `RAGTools.pre-v3-*` directory, or start clean and
   re-add projects — the configuration is a small TOML file and your source
   projects were never modified
