# Runbook: Port 21420 In Use

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Related failure codes** | TBD — see [Known Failure Codes](Reference-Known-Failure-Codes) |
| **Status** | Draft |

Covers installed default port **21420** and dev default **21421** interchangeably. This is the reactive triage view; for the operational "change the port" flow see [Port Conflict Resolution](Operational-SOPs-Service-Port-Conflict-Resolution).

## Symptom

- `rag service start` fails at bind with "address already in use" or equivalent.
- `rag doctor` reports **Service: NOT RUNNING** despite a service appearing to be alive on the port.
- Service starts but another process still listens on the same port, leading to garbled health probes.

## Quick check

- Windows (PowerShell): `Get-NetTCPConnection -LocalPort 21420`
- Windows (cmd): `netstat -ano | findstr 21420`
- macOS / Linux: `lsof -iTCP:21420 -sTCP:LISTEN`

Replace 21420 with 21421 on dev.

## Diagnostic commands

| Command | What it tells you |
|---|---|
| `netstat -ano \| findstr <port>` | PID of the current listener |
| `tasklist /FI "PID eq <pid>"` | What the listening PID actually is |
| `rag service status` | Whether the legitimate Reg service is that listener |
| `rag doctor` | End-to-end config + service + collection health |

## Root causes

1. **Reg service already running.** Legitimate — the listener is the service you meant to start. `rag service status` confirms.
2. **Stray Reg process.** A prior `rag service start` did not fully clean up; process still alive.
3. **Another app on the same port.** Dev tool, developer proxy, custom HTTP server — anything can grab 21420/21421.
4. **Wrong mode on wrong port.** Installed mode bound to dev port or vice versa via env override.

## Fix procedure

### If cause = Reg service already running
Do nothing. `rag service start` is a no-op in spirit; use `rag service status` / `rag service restart` if needed.

### If cause = stray Reg process
1. Get the PID from the netstat / lsof output.
2. `rag service stop` — attempts graceful shutdown via `POST /api/shutdown` then PID kill.
3. If still running: kill directly — Windows: `taskkill /PID <pid> /F`; Unix: `kill -9 <pid>`.
4. Retry `rag service start`.

### If cause = another app
Pick one. Either:
- **Move Reg** to another port. Follow [Port Conflict Resolution](Operational-SOPs-Service-Port-Conflict-Resolution) — env override for one-shot, TOML for persistent.
- **Move the other app** to another port. External-tool-specific.

### If cause = wrong mode on wrong port
Unset conflicting env vars (`RAG_SERVICE_PORT`), re-resolve with `rag doctor`, retry. See [Configuration Precedence](Operational-SOPs-Configuration-Configuration-Precedence).

## Verification

- `rag service status` shows running on the expected port.
- `curl http://127.0.0.1:<port>/health` returns **200**.
- Only one listener on that port — `netstat` / `lsof` shows a single line.

## Escalation

If the port appears free yet Reg still fails to bind, capture:
- `netstat -ano` full output.
- Windows Defender Firewall state (`netsh advfirewall show allprofiles` — ensure localhost is not blocked).
- `rag doctor` and `service.log` tail.

## Related
- SOP: [Port Conflict Resolution](Operational-SOPs-Service-Port-Conflict-Resolution).
- SOP: [Start and Stop Service](Operational-SOPs-Service-Start-and-Stop-Service).
- Architecture: [Service Lifecycle](Architecture-Service-Lifecycle).
