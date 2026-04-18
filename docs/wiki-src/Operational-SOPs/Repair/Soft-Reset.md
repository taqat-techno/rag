# SOP: Soft Reset

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Drop the Qdrant collection and truncate the state DB, then re-index all projects from scratch. Preserves config and the data directory itself.

## Scope
Smallest of the three reset levels (see [Reset Escalation](Architecture-Reset-Escalation)). Use when search quality regresses but config is trusted.

## Trigger
- `rag doctor` reports stale or missing collection entries.
- Embedding model changed and index must be rebuilt.
- Chunk parameters changed (`chunk_size`, `chunk_overlap`).

## Preconditions
- [ ] Version ≥ v2.4.1.
- [ ] Willing to lose the current index (will be rebuilt).
- [ ] Service may be running or stopped — soft reset works in either mode.

## Inputs
None.

## Steps

1. **Invoke:**
   ```
   rag reset
   ```
   Or via Claude CLI: `/rag:rag-reset` and choose `soft`.

2. **Confirm:** type `DELETE` exactly when prompted.

3. Reg drops the collection, truncates `index_state.db`, and starts a full re-index.

## Validation / expected result
- `rag status` initially shows 0 chunks, then grows during re-index.
- `rag doctor` reports OK once re-index completes.
- Existing projects and config are intact.

## Failure modes
| Symptom | Likely cause | Fix |
|---|---|---|
| Refuses to run | Pre-v2.4.1 gate | [Pre-v2.4.1 Reset Blocked](Runbooks-Pre-v2-4-1-Reset-Blocked). |
| Re-index never completes | Encoder stall, lock contention | Check service log; if stuck, escalate to [Data Reset](Operational-SOPs-Repair-Data-Reset). |
| Config-level issue remains | Root cause was not in state | [Configuration Precedence](Operational-SOPs-Configuration-Configuration-Precedence). |

## Recovery / rollback
No rollback. If soft reset is insufficient, escalate to [Data Reset](Operational-SOPs-Repair-Data-Reset).

## Related code paths
- `src/ragtools/cli.py` — `reset` (soft path).
- `src/ragtools/service/routes.py` — `/api/rebuild` (soft-reset path when service is up).

## Related commands
- `rag reset`; `/rag:rag-reset`.

## Change log
- 2026-04-18 — Initial draft.
