# SOP: Install Startup Task (Windows)

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Register or remove a Windows Task Scheduler entry so the Reg service starts automatically on login.

## Scope
Windows installed mode only. Does not apply to dev installs or macOS/Linux.

## Trigger
- End user wants the service available after every reboot without manual start.
- Provisioning step on a production Windows host.

## Preconditions
- [ ] Installed mode (`is_packaged()` is true — verify via `rag doctor`).
- [ ] Windows with Task Scheduler.
- [ ] Admin rights to modify the scheduled task.

## Inputs
None.

## Steps

### Register
```
rag service install
```
Creates a Task Scheduler entry that runs `rag.exe service run` at user login, with a delay equal to `startup_delay` (default 30 s). Alignment with config-level `startup_enabled` is advisory — the command creates the task regardless.

### Verify
Task Scheduler UI → navigate to `\RAGTools\Service`. Or CLI:
```
schtasks /Query /TN "\RAGTools\Service" /V /FO LIST
```

### Unregister
```
rag service uninstall
```
Removes the Task Scheduler entry. Does not stop a running service — use [Start and Stop](Operational-SOPs-Service-Start-and-Stop-Service) for that.

## Validation / expected result
- Task visible at `\RAGTools\Service` in Task Scheduler.
- On next login, the service starts after the configured delay.
- `rag service status` shows running within roughly a minute of login.

## Failure modes
| Symptom | Likely cause | Fix |
|---|---|---|
| `install` fails with access-denied | Not running as admin | Re-run the shell as administrator. |
| Task exists but service does not start on login | `rag.exe` not on PATH for the scheduled-task user; or startup delay too long | Verify PATH; adjust `startup_delay` in config. |
| Service starts but port already in use | Port conflict from other apps | [Port Conflict Resolution](Operational-SOPs-Service-Port-Conflict-Resolution). |
| Task not visible in Task Scheduler | Filter/view, or wrong folder | Check the `\RAGTools\` folder in the Task Scheduler tree. |

## Recovery / rollback
- `rag service uninstall` removes the task.
- Manual removal: `schtasks /Delete /TN "\RAGTools\Service" /F`.

## Related code paths
- `src/ragtools/service/startup.py` — Task Scheduler registration logic.

## Related commands
- `rag service install / uninstall / status`.

## Change log
- 2026-04-18 — Initial draft.
