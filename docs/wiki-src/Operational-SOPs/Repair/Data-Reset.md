# SOP: Data Reset

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Run a soft reset, then delete the data directory (`./data/` dev, `{installed data dir}` installed), and rebuild.

## Scope
Second escalation level. Destroys all index + state files but preserves config and non-data items in the installed tree.

## Trigger
- Soft reset did not fix the problem.
- Qdrant segments or SQLite state appear corrupt.
- Stale Qdrant lock persists after clean shutdown.

## Preconditions
- [ ] Service stopped (`rag service stop`).
- [ ] Version ≥ v2.4.1.
- [ ] Willing to lose all index and state files.

## Inputs
None.

## Steps

1. **Stop the service:**
   ```
   rag service stop
   ```

2. **Invoke:**
   ```
   rag reset --data
   ```
   Or via Claude CLI: `/rag:rag-reset`, choose `data`.

3. **Confirm:** type `DELETE` exactly when prompted.

4. Reg runs soft reset, removes the data directory, recreates it empty, and starts a full re-index.

5. **Start the service:**
   ```
   rag service start
   ```

## Validation / expected result
- Data directory exists again (empty at start).
- `rag doctor` reports OK once the initial re-index completes.
- Config file is unchanged.

## Failure modes
| Symptom | Likely cause | Fix |
|---|---|---|
| Reset fails "file in use" | Service still running | `rag service stop`, wait, retry. |
| Re-index hangs after reset | Path issue, permission, encoder crash | Check service log; retry; consider [Nuclear Reset](Operational-SOPs-Repair-Nuclear-Reset). |
| Data dir on network share did not delete cleanly | Partial delete on flaky FS | Manually remove remaining files; retry. |

## Recovery / rollback
No rollback. Escalate to [Nuclear Reset](Operational-SOPs-Repair-Nuclear-Reset) if symptoms persist.

## Related code paths
- `src/ragtools/cli.py` — `reset --data`.

## Related commands
- `rag reset --data`; `/rag:rag-reset`.

## Change log
- 2026-04-18 — Initial draft.
