# Runbook: Qdrant Lock Contention

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Related failure codes** | TBD — see [Known Failure Codes](Reference-Known-Failure-Codes) |
| **Status** | Draft |

## Symptom

One or more of:
- Service start fails with error text mentioning "storage already open", "lock", or "resource busy" against the Qdrant storage directory.
- `rag index` / `rag rebuild` run directly (no service) fail with the same class of error.
- The MCP server in direct mode reports `[RAG ERROR] Failed to ...` pointing at Qdrant.
- Searches return inconsistent results — possible silent corruption from a past violation.

## Quick check

```
rag service status
```
If the service is **running**, then by definition Qdrant is locked by the service — that is correct operation. Do not open Qdrant from another process; use the HTTP API / CLI dual-mode instead.

## Diagnostic commands

| Command | What it tells you |
|---|---|
| `rag service status` | Is the service the legitimate lock holder |
| `tasklist /FI "IMAGENAME eq rag.exe"` (Windows) or `ps aux \| grep rag` (Unix) | Any other Reg processes alive |
| `type {data_dir}\service.pid` | PID claimed by the service |
| `dir {data_dir}\qdrant\` | Qdrant storage files — look for stale `*.lock` |

## Root causes

Ordered by frequency:

1. **Service is running and something else tried direct mode.** Most common. Not a bug — a workflow error. E.g. user ran `rag index` or `rag rebuild` while the service holds the lock and the direct fallback tried to open Qdrant.
2. **Two services running.** Two launches against the same data dir — second one fails on the lock.
3. **Stale lock from a crashed process.** Process exited uncleanly leaving lock artifacts; new launch sees them and refuses.
4. **External Qdrant viewer / script.** A standalone tool opened `data/qdrant/` for inspection.
5. **Silent corruption evidence.** Rare. Past violation produced inconsistent segments; manifests as missing or duplicated points. Detect with `rag doctor` (scale warning or collection-not-found).

## Fix procedure

### If cause = service running, direct mode tried
1. Do not fight it. Use the dual-mode CLI: `rag search`, `rag rebuild`, `rag projects` all route through the service when it is up.
2. For commands that require the service (`rag index`), the service must run — this is by design. See [Add a New Command § Pattern A](Development-SOPs-Commands-Add-a-New-Command#pattern-a--service-required-no-direct-fallback).

### If cause = two services
1. `rag service status` on each host.
2. Stop the unwanted one: `rag service stop`.
3. Only one service per data directory — ever. See [Single-Process Invariant](Core-Concepts-Single-Process-Invariant).

### If cause = stale lock
1. Confirm **no live Reg process** exists: `tasklist /FI "IMAGENAME eq rag.exe"` (Windows) or `ps aux | grep rag`.
2. If the PID file lists a PID that is not running, delete the PID file.
3. Stop all Reg shells.
4. Remove stale Qdrant lock artifacts if any: inspect `data/qdrant/` for `*.lock` files and delete only after confirming no live process.
5. Retry `rag service start`.

### If cause = external Qdrant viewer
Close it. Do not open Reg's Qdrant storage from unrelated tools while Reg might use it.

### If cause = silent corruption suspected
1. Run `rag doctor` — scale warnings, missing collection, or anomalous counts are signals.
2. Escalate to [Soft Reset](Operational-SOPs-Repair-Soft-Reset); escalate further to [Data Reset](Operational-SOPs-Repair-Data-Reset) if soft does not fix it.
3. Do not try to "repair" the Qdrant segments by hand.

## Verification

- `rag service status` shows a single running service on the expected port.
- `rag doctor` shows Collection OK with a plausible points count.
- Direct-mode commands that **should** work (search with service down) open and release cleanly.

## Escalation

Capture:
- `rag service status`, `rag doctor`, `rag version`.
- PID file contents and `tasklist` / `ps` output for Reg processes.
- Directory listing of `{data_dir}/qdrant/`.
- Last 50 lines of `service.log`.

## Related
- Core concept: [Single-Process Invariant](Core-Concepts-Single-Process-Invariant).
- Architecture: [Service Lifecycle](Architecture-Service-Lifecycle), [CLI Dual-Mode](Architecture-CLI-Dual-Mode).
- SOP: [Repair Broken Installation](Operational-SOPs-Repair-Repair-Broken-Installation).
