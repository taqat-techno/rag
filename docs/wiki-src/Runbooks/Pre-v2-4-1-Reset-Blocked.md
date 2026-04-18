# Runbook: Pre-v2.4.1 Reset Blocked

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Related failure codes** | TBD — see [Known Failure Codes](Reference-Known-Failure-Codes) |
| **Status** | Draft (partial — details blocked on [Q-7](Development-SOPs-Documentation-Open-Questions)) |

## Symptom

- `rag reset` (or `rag reset --data` / `rag reset --nuclear`) refuses to run with a message indicating a version gate.
- `rag doctor` flags the install as older than v2.4.1.
- Post-upgrade inconsistency: `rag status` shows chunks but search returns empty or malformed results.

## Quick check

```
rag version
```
If the reported version is **< 2.4.1**, the gate is engaged — this is by design.

## Root causes

1. **Install is pre-v2.4.1.** The reset path refuses to operate because the on-disk collection and state schema changed at v2.4.1 and running the new reset against the old schema can silently delete data the new code cannot rebuild.
2. **Data directory was carried forward from a pre-v2.4.1 install.** Binary is new; data is old. Rare but possible if someone upgraded binaries only (e.g. replaced the .exe manually) without a clean install.

## Fix procedure

### Preferred path — upgrade cleanly

1. Back up the data directory:
   - Windows: `robocopy %LOCALAPPDATA%\RAGTools %USERPROFILE%\rag-backup /E`
   - macOS: `cp -R ~/Library/Application\ Support/RAGTools ~/rag-backup/`
   - Dev: `cp -R ./data ./data.bak/`
2. Download and install v2.4.2+ via [Fresh Install (Packaged)](Operational-SOPs-Installation-Fresh-Install-Packaged).
3. Start the service — first run will either adopt or flag the carried-forward data dir.
4. If the start succeeds and search looks right: you are done.
5. If the start fails or search looks wrong: escalate to [Nuclear Reset](Operational-SOPs-Repair-Nuclear-Reset) (re-index from Markdown source; data backup remains).

### Alternate path — manual clean-slate

If upgrading is not yet an option and reset is blocked:
1. Stop the service.
2. Back up the data directory (as above).
3. Manually delete the data directory.
4. Start the service — fresh state.
5. Re-index projects.

### Details TBD

Exactly what changed at v2.4.1 (collection schema, state DB schema, or both) is tracked as [Q-7](Development-SOPs-Documentation-Open-Questions). Once Q-7 resolves, a migration guide at [Change-History / Migration Guides / Pre-v2.4.1 to Current](Change-History-Migration-Guides-Pre-v2-4-1-to-Current) will cover in-place migration instead of clean-slate.

## Verification

- `rag version` reports the new version.
- `rag doctor` exits 0.
- Searches return plausible results; `rag status` shows plausible chunk counts.

## Escalation

If the gate is hit on a v2.4.1+ install (i.e. the gate is misfiring), capture:
- Full `rag version` output.
- `rag doctor`.
- `config.toml` and `index_state.db` version markers if accessible.
- The exact gate message.

## Related
- Architecture: [Reset Escalation](Architecture-Reset-Escalation).
- SOP: [Repair Broken Installation](Operational-SOPs-Repair-Repair-Broken-Installation).
- Change History: [Pre-v2.4.1 to Current migration guide](Change-History-Migration-Guides-Pre-v2-4-1-to-Current) (Phase 9 deliverable).
- Open Questions: [Q-7](Development-SOPs-Documentation-Open-Questions).
