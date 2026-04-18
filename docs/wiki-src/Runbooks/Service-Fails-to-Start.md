# Runbook: Service Fails to Start

| | |
|---|---|
| **Owner** | TBD (proposed: ops lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Related failure codes** | TBD — see [Known Failure Codes](Reference-Known-Failure-Codes) |
| **Status** | Draft |

## Symptom

One or more of:
- `rag service start` returns, but `rag service status` stays **stopped** or **starting** for more than 60 s.
- `curl http://127.0.0.1:<port>/health` never returns **200**.
- `rag doctor` shows **Service: NOT RUNNING** after an explicit start.
- Service process exits immediately on launch (no PID file, or PID file appears and then disappears).

## Quick check

```
rag service status
rag doctor
```

Then tail the log:
- Installed: `type "%LOCALAPPDATA%\RAGTools\data\logs\service.log"`
- Dev: look at stderr from the launching shell, or start the service in foreground: `python -m ragtools.service.run`

## Diagnostic commands

| Command | What it tells you |
|---|---|
| `rag service status` | Is the service already running, stopped, or stuck starting |
| `rag doctor` | Python, dependencies, service, data dir, state DB, collection scale status |
| `Get-NetTCPConnection -LocalPort 21420` / `21421` (PowerShell) | Who, if anyone, is bound to the service port |
| `dir {data_dir}\qdrant\` | Qdrant storage exists; check for orphaned lock files |
| `type {data_dir}\logs\service.log` | Startup errors (encoder load, Qdrant open, config parse) |
| `python -m ragtools.service.run` (dev) | Run in foreground to see errors directly |

## Root causes

Ordered by frequency:

1. **Port in use.** Another process holds 21420/21421. → [Port 21420 In Use](Runbooks-Port-21420-In-Use).
2. **Qdrant lock contention.** Another process (service, CLI direct, external viewer) already owns the data dir. → [Qdrant Lock Contention](Runbooks-Qdrant-Lock-Contention).
3. **Encoder failed to load.** First-time encoder download blocked (no network, firewall), corrupt Hugging Face cache, or disk full.
4. **Data dir not writable.** `RAG_DATA_DIR` points at an unwritable path, or `%LOCALAPPDATA%\RAGTools\` lost permissions.
5. **Packaging defect (installed mode).** PyInstaller hidden-import missing — `ModuleNotFoundError` during import.
6. **Stale PID file + live process.** Prior run left a PID file that now refers to an unrelated live process; service thinks it's already running.
7. **Config parse error.** `ragtools.toml` / `config.toml` has invalid syntax or types; Pydantic validation fails at startup.

## Fix procedure

### If cause = port in use
Go to [Port 21420 In Use](Runbooks-Port-21420-In-Use).

### If cause = Qdrant lock
Go to [Qdrant Lock Contention](Runbooks-Qdrant-Lock-Contention).

### If cause = encoder load failure
1. Confirm internet access to `huggingface.co`.
2. Clear the Hugging Face cache if corrupt: delete `~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/`.
3. Pre-warm the model from a shell with network access, then copy the cache to the target machine if offline.
4. Retry `rag service start`.

### If cause = data dir not writable
1. Check the resolved data dir with `rag doctor`.
2. Fix permissions, or override with a writable path via `RAG_DATA_DIR`.
3. Retry start.

### If cause = PyInstaller missing import
1. Installed build only. Reproduce by running `rag service run` (not `start`) so errors surface.
2. If a hidden import is missing, the fix is in `rag.spec` — this is a release defect, not a user-fixable issue. File an issue with the traceback.

### If cause = stale PID file + live other process
1. `type {data_dir}\service.pid` — note the PID.
2. Confirm whether the PID corresponds to a live Reg process (`tasklist /FI "PID eq <pid>"`).
3. If not, delete the PID file and retry.
4. If the PID is live but **not** Reg, kill it only if it is clearly leftover (then delete the PID file).

### If cause = config parse error
1. Run `rag doctor` — the error surface shows the offending field.
2. Fix the TOML. See [Configuration Keys](Reference-Configuration-Keys).
3. Retry.

## Verification

- `rag service status` shows **running** with a valid PID and port.
- `curl http://127.0.0.1:<port>/health` returns **200** with `{"status": "ready", "collection": "markdown_kb"}`.
- `rag doctor` shows green across Service, Data directory, and Collection.
- No repeating error lines in the recent tail of `service.log`.

## Escalation

If none of the causes match and `service.log` does not explain the failure:
1. Capture: `rag doctor` output, the last 100 lines of `service.log`, `rag version`.
2. Try starting in foreground: `python -m ragtools.service.run` — capture the full traceback.
3. File an issue with all three artifacts.

## Related
- SOP: [Start and Stop Service](Operational-SOPs-Service-Start-and-Stop-Service).
- Architecture: [Service Lifecycle](Architecture-Service-Lifecycle).
- Reference: [Known Failure Codes](Reference-Known-Failure-Codes).
