# SOP: Repair a Broken Installation

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Decide which repair level to use, then run it. Orchestrator SOP — not the actual reset execution (that lives on each level's own page).

## Scope
All three reset levels plus the diagnostic escalation that precedes them. For the underlying model see [Reset Escalation](Architecture-Reset-Escalation).

## Trigger
- Searches return wrong, missing, or corrupt results.
- Service refuses to start.
- `rag doctor` reports BLOCKER-level findings.
- Post-upgrade behavior is off and restart does not fix it.

## Preconditions
- [ ] Ability to stop the service.
- [ ] Awareness that reset is destructive and requires typing `DELETE` verbatim.
- [ ] Version ≥ v2.4.1 — pre-v2.4.1 installations are gated (see [Pre-v2.4.1 Reset Blocked](Runbooks-Pre-v2-4-1-Reset-Blocked)).

## Inputs
- Output of `rag doctor`.
- If using Claude CLI: the `/rag:rag-doctor` skill walks the same path interactively.

## Steps

1. **Diagnose first.**
   ```
   rag doctor
   ```
   Or invoke `/rag:rag-doctor` from Claude CLI. This is the sanctioned entry point; skipping to reset without diagnosis is a common anti-pattern and destroys evidence.

2. **Classify (pick the lowest escalation that covers the symptom).**
   - Service running but search is wrong → **[Soft Reset](Operational-SOPs-Repair-Soft-Reset)**.
   - Service fails to start with "bad file", SQLite corruption, or partial Qdrant segments → **[Data Reset](Operational-SOPs-Repair-Data-Reset)**.
   - Doctor reports config corruption, or you want a clean-slate reinstall → **[Nuclear Reset](Operational-SOPs-Repair-Nuclear-Reset)**.

3. **Stop the service** if escalating past soft reset:
   ```
   rag service stop
   ```

4. **Execute the selected level.** Each level is documented on its own page and enforces a `DELETE` prompt.

5. **Restart and verify:**
   ```
   rag service start
   rag doctor
   ```

## Validation / expected result
- `rag doctor` exits 0 with no BLOCKER findings.
- Searches return plausible results.
- `rag status` shows chunk counts consistent with prior state (soft / data) or growing from zero (nuclear).

## Failure modes
| Symptom | Likely cause | Fix |
|---|---|---|
| Reset refuses to run on old version | Pre-v2.4.1 gate | [Pre-v2.4.1 Reset Blocked](Runbooks-Pre-v2-4-1-Reset-Blocked). |
| Reset succeeds but problem persists | Root cause was not in state | Re-diagnose. Likely [Configuration Precedence](Operational-SOPs-Configuration-Configuration-Precedence) or a bug — capture evidence. |
| Soft reset did not stop the watcher | Expected — watcher runs through the reset | No action. |
| Data/nuclear reset fails — file in use | Service still running | Stop service; re-run. |

## Recovery / rollback
No rollback — data is destroyed by design. Escalate to the next level up if the chosen level did not resolve the symptom.

## Related code paths
- `src/ragtools/cli.py` — `reset` command.
- `.claude/skills/rag:rag-doctor` — diagnostic skill.
- `.claude/skills/rag:rag-reset` — interactive repair skill.

## Related commands
- `rag doctor`, `rag reset [--data | --nuclear]`, `rag service stop / start`.
- `/rag:rag-doctor`, `/rag:rag-reset`.

## Change log
- 2026-04-18 — Initial draft.
