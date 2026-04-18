# SOP: Port Conflict Resolution

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Diagnose and fix the case where the service cannot start because its configured port is already bound.

## Scope
Default ports **21420** (installed) and **21421** (dev), or any overridden `service_port`. Does not cover firewall/networking (service binds localhost-only).

## Trigger
- `rag service start` fails with a bind error.
- `rag doctor` reports "port in use".
- Two Reg installs on the same host colliding on a port.

## Preconditions
- [ ] `rag doctor` output identifying the target port.
- [ ] Ability to list local TCP listeners.

## Inputs
- The configured port (from `rag doctor`, or `[service].port` in the TOML config).

## Steps

1. **Confirm the port is in use.**
   - Windows (PowerShell): `Get-NetTCPConnection -LocalPort 21420`
   - Windows (cmd): `netstat -ano | findstr 21420`
   - macOS / Linux: `lsof -iTCP:21420 -sTCP:LISTEN`

2. **Identify the offending process.**
   - Windows: look up the PID from `netstat -ano` in Task Manager.
   - macOS / Linux: `ps -p <pid>`.

3. **Decide.**
   - If it is a stray `rag` process → stop it (`rag service stop` or kill by PID).
   - If it is another app you need → change Reg's port.

4. **Change the port — option A (env var, one-shot):**
   ```
   setx RAG_SERVICE_PORT 21430     # Windows (new shells)
   export RAG_SERVICE_PORT=21430   # macOS/Linux
   ```
   Start a new shell for the change to be visible to processes launched from it.

5. **Change the port — option B (TOML, persistent):**
   Edit the config file (see [Configuration Keys](Reference-Configuration-Keys)):
   ```toml
   [service]
   port = 21430
   ```
   Restart the service.

6. **Restart and verify.**
   ```
   rag service start
   rag service status
   ```

## Validation / expected result
- `rag service status` shows running on the expected port.
- `curl http://127.0.0.1:<port>/health` returns **200**.
- The previously-occupying process is either stopped or on a different port.

## Failure modes
| Symptom | Likely cause | Fix |
|---|---|---|
| Change to TOML has no effect | Service not restarted | Restart the service. |
| Change to env has no effect | Service was launched from a shell that does not see the env var | Relaunch from the shell where the env is visible. |
| CLI/MCP still probe old port | They read their own `Settings` at launch | Ensure the same config source is visible to CLI/MCP as to the service. See [Configuration Precedence](Operational-SOPs-Configuration-Configuration-Precedence). |
| New port also bound | Rare but possible on congested hosts | Pick another port; confirm first via step 1. |

## Recovery / rollback
- Revert the override; restart the service.

## Related code paths
- `src/ragtools/config.py:_default_service_port` (line 205).
- `src/ragtools/service/run.py` — bind step.

## Related commands
- `rag doctor`, `rag service status / start / stop`.

## Change log
- 2026-04-18 — Initial draft.
