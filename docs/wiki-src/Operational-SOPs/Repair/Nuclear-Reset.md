# SOP: Nuclear Reset

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Delete everything Reg owns — data, state, config, and logs. Equivalent to uninstall + reinstall for state purposes.

## Scope
Third and most destructive reset level. Config is destroyed and must be re-entered.

## Trigger
- Data and soft resets did not resolve the issue.
- Config file is corrupt or unreadable.
- Starting over for a clean-room reproduction.
- Preparing a stuck install for uninstall.

## Preconditions
- [ ] Service stopped.
- [ ] Config is either backed up or expendable.
- [ ] Version ≥ v2.4.1.

## Inputs
None.

## Steps

1. **(Optional) Back up config.**
   - Windows: `copy %LOCALAPPDATA%\RAGTools\config.toml C:\path\to\backup`
   - macOS: `cp ~/Library/Application\ Support/RAGTools/config.toml ~/backup/`
   - Dev: `cp ./ragtools.toml ./backup/`

2. **Stop the service:**
   ```
   rag service stop
   ```

3. **Invoke:**
   ```
   rag reset --nuclear
   ```
   Or via Claude CLI: `/rag:rag-reset`, choose `nuclear`.

4. **Confirm:** type `DELETE` exactly when prompted.

5. Reg runs data reset, then deletes the entire installed tree — `%LOCALAPPDATA%\RAGTools\` on Windows, `~/Library/Application Support/RAGTools/` on macOS, or `./data/` on a dev checkout.

6. **Re-establish config** (if backed up): copy it back to the resolved config path.

7. **Start and verify:**
   ```
   rag service start
   rag doctor
   ```

## Validation / expected result
- The app data directory is gone (or freshly empty).
- First `rag doctor` may report "defaults only" until config is re-entered.
- Re-index grows from zero chunks.

## Failure modes
| Symptom | Likely cause | Fix |
|---|---|---|
| Reset leaves binaries in place | Binaries are not Reg-owned data — the installer owns them | Uninstall via **Apps & features** to remove the Program Files tree. |
| Re-run asks for `DELETE` again | Idempotent; running twice is safe | No action. |
| Partial delete due to permissions | AV or Windows held files open | Elevate; close any Reg process still using the tree. |

## Recovery / rollback
Restore from config backup. No other rollback possible.

## Related code paths
- `src/ragtools/cli.py` — `reset --nuclear`.

## Related commands
- `rag reset --nuclear`; `/rag:rag-reset`.

## Change log
- 2026-04-18 — Initial draft.
