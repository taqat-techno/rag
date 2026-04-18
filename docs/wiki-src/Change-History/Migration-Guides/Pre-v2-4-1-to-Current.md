# Migration Guide: Pre-v2.4.1 to Current

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Partial — schema delta details blocked on [Q-7](Development-SOPs-Documentation-Open-Questions) |

> **Partial.** The exact schema change at v2.4.1 (Qdrant collection vs. SQLite state DB vs. config TOML) is not yet documented. This guide covers the two paths that work today — clean upgrade and manual clean-slate. In-place migration will be added when Q-7 resolves.

## Who this applies to

Any install whose `rag version` reports a version **older than 2.4.1**, including installs that have been upgraded-in-place without a clean reset.

## Why you cannot just `rag reset`

The new reset paths (`rag reset`, `rag reset --data`, `rag reset --nuclear`) refuse to run on pre-v2.4.1 data, because the schema changed in a way that makes the old state indistinguishable from corrupt state to the new code. See [Pre-v2.4.1 Reset Blocked](Runbooks-Pre-v2-4-1-Reset-Blocked).

## Path A — clean upgrade (recommended)

1. **Back up the data directory** so nothing is lost irrecoverably:
   - Windows: `robocopy "%LOCALAPPDATA%\RAGTools" "%USERPROFILE%\rag-backup" /E`
   - macOS: `cp -R ~/Library/Application\ Support/RAGTools ~/rag-backup/`
   - Dev: `cp -R ./data ./data.bak/`

2. **Back up the config file** if you have custom settings:
   - Installed (Windows): `%LOCALAPPDATA%\RAGTools\config.toml`
   - Installed (macOS): `~/Library/Application Support/RAGTools/config.toml`
   - Dev: `./ragtools.toml`

3. **Install v2.4.2+** via [Fresh Install (Packaged)](Operational-SOPs-Installation-Fresh-Install-Packaged) or [Fresh Install (Dev)](Operational-SOPs-Installation-Fresh-Install-Dev).

4. **Start the service** and run `rag doctor`. Two outcomes:
   - **OK.** The old data directory was adopted cleanly. Done.
   - **Errors / inconsistent search.** Continue to step 5.

5. **Nuclear reset** via [Nuclear Reset](Operational-SOPs-Repair-Nuclear-Reset) to start with an empty state, then re-index from your original Markdown source.

6. **Restore config** if you backed it up in step 2: copy it back to the resolved config path and restart the service.

## Path B — manual clean-slate (if reset is blocked and you cannot upgrade yet)

1. **Stop the service:**
   ```
   rag service stop
   ```

2. **Back up the data directory** (as in Path A step 1).

3. **Delete the data directory manually.** Do not use `rag reset` — it is blocked by design.
   - Windows: `rmdir /S /Q "%LOCALAPPDATA%\RAGTools\data"`
   - macOS: `rm -rf ~/Library/Application\ Support/RAGTools/data`
   - Dev: `rm -rf ./data`

4. **Start the service** — it recreates an empty data tree.

5. **Re-index** your projects:
   - Admin panel: add each project via **Projects → Add project**.
   - Or CLI: `rag project add --id <id> --path <abs-path>` for each, then `rag index` once the service is running.

## What does not migrate automatically

As of v2.4.2 there is no in-place schema migrator for pre-v2.4.1 data. When Q-7 resolves, a `migrate_pre_v241` function will be specified here. Until then, only the backup and re-index paths above work.

## Verification after migration

- `rag version` shows v2.4.2 (or later).
- `rag doctor` exits 0 with no BLOCKER lines.
- `rag projects` lists the expected projects.
- `rag status` shows plausible chunk counts per project (similar order of magnitude to pre-upgrade; exact counts will differ because chunking params may have changed).
- `rag search "a term you know is indexed"` returns plausible results.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Reset still blocked after upgrade | You upgraded binaries but the data directory still identifies as pre-v2.4.1 | Follow Path B (manual delete) then Path A step 3 onward. |
| Config lost after nuclear reset | Step 6 skipped | Restore from backup. |
| Re-index takes much longer than expected | Full re-embed of everything — expected | Let it finish; monitor via `rag status`. |
| Search quality differs from pre-upgrade | Model or chunking changed between versions | Expected. Evaluate with `scripts/eval_retrieval.py` if regression suspected. |

## Related

- [Pre-v2.4.1 Reset Blocked](Runbooks-Pre-v2-4-1-Reset-Blocked) — the runbook that triggers this guide.
- [Reset Escalation](Architecture-Reset-Escalation) — the full repair model.
- [Release Checklist](Development-SOPs-Release-Release-Checklist) — what a release is expected to preserve vs. replace.
- [docs/RELEASE_LIFECYCLE.md](../../../docs/RELEASE_LIFECYCLE.md) (in-repo) — the lifecycle contract.
- [Q-7](Development-SOPs-Documentation-Open-Questions) — the outstanding schema-delta question.
