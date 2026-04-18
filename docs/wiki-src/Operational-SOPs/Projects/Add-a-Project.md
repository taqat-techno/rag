# SOP: Add a Project

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Register a directory of Markdown as a project so Reg indexes, watches, and searches it.

## Scope
Adding a project via admin panel, CLI, or TOML edit. Project-level ignore rules covered in `ignore_patterns` of `ProjectConfig` (see [Configuration Keys](Reference-Configuration-Keys)).

## Trigger
- First-time project onboarding.
- Adding an additional repo or notes tree to an existing Reg install.

## Preconditions
- [ ] Absolute path to the target directory.
- [ ] Directory contains one or more `.md` files.
- [ ] Service running (recommended) so the watcher picks up the new project immediately.

## Inputs
- `id` — short unique identifier (used in storage keys and search filter).
- `path` — absolute directory path.
- Optional: `name` (display), `enabled` (default true), `ignore_patterns`.

## Steps

### Option A — admin panel (recommended)
1. Open `http://127.0.0.1:<port>` (port per [Installed App vs Standalone](Operational-SOPs-Runtime-Installed-App-vs-Standalone-Behavior)).
2. Go to **Projects**.
3. Click **Add project**; fill in `id` + `path`. Save.
4. The watcher restarts; the initial index runs in the background.

### Option B — CLI (dual-mode)
```
rag project add --id my-notes --path /abs/path/to/notes
```
If the service is running, the command forwards via HTTP; otherwise it updates state directly.

### Option C — TOML (advanced / bulk)
Edit the config file:
```toml
[[projects]]
id = "my-notes"
name = "My notes"
path = "/abs/path/to/notes"
enabled = true
ignore_patterns = ["drafts/", "*.tmp"]
```
Restart the service to pick up the change.

## Validation / expected result
- `rag projects` lists the new project.
- `rag status` shows chunks indexed for `my-notes` after the initial index completes.
- `rag search "..." -p my-notes` returns results.
- Watcher picks up subsequent file changes in the path.

## Failure modes
| Symptom | Likely cause | Fix |
|---|---|---|
| Project added but zero chunks indexed | No `.md` files; or all files matched ignore rules | `rag ignore test <file>`; add files; adjust ignore rules. |
| "Path not accessible" warning at startup | Path on an offline network share or USB | Make the path reachable, or remove/disable the project. Watcher retries unavailable paths every 60 s. |
| Duplicate `id` | Another project already uses it | Choose a unique id. |
| TOML change not applied | Service was not restarted | Restart. |

## Recovery / rollback
- `rag project remove --id my-notes`.
- Or edit the TOML and restart.

## Related code paths
- `src/ragtools/config.py:ProjectConfig` (line 33) — schema.
- `src/ragtools/indexing/scanner.py:discover_projects` — v1 legacy auto-discovery.
- `src/ragtools/service/watcher_thread.py` — watcher restart on project change.

## Related commands
- `rag project add / list / remove / enable / disable`.
- `rag projects`.
- `/rag:rag-projects`.

## Change log
- 2026-04-18 — Initial draft.
