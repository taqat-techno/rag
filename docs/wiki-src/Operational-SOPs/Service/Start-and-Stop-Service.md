# SOP: Start and Stop the Service

| | |
|---|---|
| **Owner** | TBD (proposed: ops lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Start, stop, and check the Reg service from the CLI. This is the canonical way to control the long-running process that owns Qdrant.

## Scope
`rag service start / stop / status`. Does not cover auto-start registration (see [Install Startup Task](Operational-SOPs-Service-Install-Startup-Task)) or lifecycle internals (see [Service Lifecycle](Architecture-Service-Lifecycle)).

## Trigger
- User needs the service running to use the admin panel or to accelerate CLI/MCP.
- User needs to stop the service before a data or nuclear reset.
- Troubleshooting: confirming whether the service is actually up.

## Preconditions
- [ ] Reg installed.
- [ ] No conflicting process on the configured service port — see [Port Conflict Resolution](Operational-SOPs-Service-Port-Conflict-Resolution).

## Inputs
None — commands read values from `Settings`.

## Steps

### Start
```
rag service start
```
Spawns a detached background process and returns control to the shell. First start takes 5-10 s for encoder load; `/health` returns **503** until ready, then **200**.

### Status
```
rag service status
```
Prints running/stopped, PID, and port.

### Stop (graceful)
```
rag service stop
```
Sends `POST /api/shutdown`. Falls back to killing by PID if the HTTP endpoint is unreachable.

### Restart
```
rag service stop
rag service start
```

## Validation / expected result
- After `start`: `curl http://127.0.0.1:<port>/health` returns **200** within 10 s.
- After `stop`: connection refused on the port; PID file removed.
- `rag service status` reflects the actual state.

## Failure modes
| Symptom | Likely cause | Runbook / Fix |
|---|---|---|
| `start` returns but `/health` never reaches 200 | Encoder still loading or failed | Tail `{data_dir}/logs/service.log`. [Service Fails to Start](Runbooks-Service-Fails-to-Start). |
| `start` errors "port already bound" | Another process on port | [Port Conflict Resolution](Operational-SOPs-Service-Port-Conflict-Resolution). |
| `stop` hangs | Shutdown endpoint unreachable | Fallback PID-kill runs up to 30 s; if still alive, manually kill PID from `{data_dir}/service.pid`. |
| Stale PID file from a crashed run | Start path detects and overwrites | No action needed. |
| Two services running | Launched from two different configs/data dirs | Stop one; pick a single canonical config. See [Installed App vs Standalone](Operational-SOPs-Runtime-Installed-App-vs-Standalone-Behavior). |

## Recovery / rollback
- Force-kill via OS if `stop` fails repeatedly.
- Remove stale `{data_dir}/service.pid` manually only if the PID does not correspond to a live process.

## Related code paths
- `src/ragtools/service/run.py` — service main.
- `src/ragtools/service/process.py` — subprocess spawn + PID management.
- `src/ragtools/service/routes.py` — `/health`, `/api/shutdown`.

## Related commands
- `rag service start / stop / status / install / uninstall`.

## Change log
- 2026-04-18 — Initial draft.
