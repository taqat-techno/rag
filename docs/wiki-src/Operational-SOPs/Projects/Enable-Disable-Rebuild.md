# SOP: Enable / Disable / Rebuild a Project

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Toggle a project between active and inactive without removing it, or force a complete re-index of one project.

## Scope
Per-project operations. Global rebuild is [Soft Reset](Operational-SOPs-Repair-Soft-Reset).

## Trigger
- Temporarily suspend a noisy project (e.g. a heavy drafts area).
- Force a re-index after changing chunking or model settings.
- Clear stale points for a renamed or reorganized project.

## Preconditions
- [ ] Project exists (see [Add a Project](Operational-SOPs-Projects-Add-a-Project)).
- [ ] Service running (recommended).

## Inputs
- Project `id`.

## Steps

### Disable (stop watching + searching)
```
rag project disable --id my-notes
```
- Watcher drops its subscription.
- Existing points remain in the collection but are filtered out by the disabled flag.

### Enable (reverse of disable)
```
rag project enable --id my-notes
```
- Watcher re-subscribes.
- An incremental re-index catches up any changes made while disabled.

### Rebuild (drop + re-embed just this project)
```
rag rebuild --project my-notes
```
- Deletes all points with `project_id = my-notes`.
- Re-embeds every `.md` file in the project from scratch.

## Validation / expected result
- After **disable**: `rag status` shows the project as inactive; `-p my-notes` searches return no results.
- After **enable**: watcher active again; new changes indexed.
- After **rebuild**: chunk count matches a full enumeration of the project tree.

## Failure modes
| Symptom | Likely cause | Fix |
|---|---|---|
| Disable has no effect | Service not running; state updated but no live watcher change | Start the service, or trust that the next service start will apply. |
| Rebuild fails mid-way | Lock contention; encoder crash | Check service log; retry; escalate to [Soft Reset](Operational-SOPs-Repair-Soft-Reset) for the whole collection if repeated. |
| Rebuild leaves orphans | Project was renamed before rebuild | Keep `id` stable; or do a soft reset. |

## Recovery / rollback
- Re-run the inverse command.
- For a botched rebuild: soft reset the whole collection.

## Related code paths
- `src/ragtools/service/routes.py` — `/api/projects/*`, `/api/rebuild`.
- `src/ragtools/config.py:ProjectConfig.enabled`.

## Related commands
- `rag project enable / disable`, `rag rebuild --project <id>`.
- `/rag:rag-projects`.

## Change log
- 2026-04-18 — Initial draft.
