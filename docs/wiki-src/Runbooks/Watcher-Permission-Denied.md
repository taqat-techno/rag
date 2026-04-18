# Runbook: Watcher Permission Denied

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Related failure codes** | TBD — see [Known Failure Codes](Reference-Known-Failure-Codes) |
| **Status** | Draft |

## Symptom

- `service.log` shows repeated "permission denied" / "access denied" lines from the watcher.
- Files in a specific subdirectory are never indexed, even though they exist and are `.md`.
- Watcher logs warnings but does not crash — by design (see [Watcher Flow § Unavailable paths](Architecture-Watcher-Flow)).

## Quick check

```
rag ignore test /abs/path/to/problem.md
```
If the path is ignored by rules, that explains the missing index. Otherwise, a real permission issue.

```
rag status
```
Shows total files / chunks per project; a zero count on a project that has files is a symptom.

## Diagnostic commands

| Command | What it tells you |
|---|---|
| `rag ignore test <path>` | Whether ignore rules are filtering the file |
| `icacls <path>` (Windows) / `ls -l <path>` (Unix) | Filesystem ACL on the target |
| `type {data_dir}\logs\service.log` tail | Exact watcher error messages |
| `rag status` | Per-project chunk counts — spot the outlier |

## Root causes

1. **Restrictive ACL on a subdirectory.** Admin-owned folder, OneDrive offline placeholder, system-protected path.
2. **Network share lost auth.** Credential expired mid-session; `watchfiles` cannot poll.
3. **File in use by another process.** Antivirus or indexer holding a handle; transient.
4. **Path inside a standard skip dir that was **not** actually skipped.** `.git/`, `node_modules/`, `.venv/` — usually filtered by `SKIP_DIRS`; occasional cases slip through nested layouts.
5. **Symlink loop or unreadable symlink target.** `watchfiles` follows links by default.

## Fix procedure

### If cause = restrictive ACL
1. Fix the ACL: grant read/list to the user running the service.
   - Windows: `icacls <path> /grant "<user>":(RX)`
   - Unix: `chmod` / `chown` as appropriate.
2. Or, if the directory should not be indexed: add it to the project's `ignore_patterns` (in `[[projects]]`) or drop a `.ragignore` line in the parent directory.

### If cause = network share lost auth
1. Reconnect the share.
2. The watcher retries every 60 s automatically — no restart required for transient outages. Confirm by watching the log.
3. For persistent issues, either move content off the share or disable the project.

### If cause = file in use by another process
Usually transient. If persistent (AV quarantining `.md`): add the file to AV exclusions, or add the file pattern to `.ragignore`.

### If cause = standard skip dir not actually skipped
1. Capture the exact path from `service.log`.
2. Add the pattern explicitly in project `ignore_patterns` or global `[ignore].patterns` (see [Configuration Keys](Reference-Configuration-Keys)).
3. If this is a generic case that should be a built-in default, file an issue — `SKIP_DIRS` in `scanner.py` should be updated.

### If cause = symlink loop
1. Inspect the target: `dir /AL` (Windows) or `ls -la` (Unix) inside the project path.
2. Remove the bad symlink, or add its pattern to `.ragignore`.

## Verification

- `service.log` no longer logs permission-denied lines for the fixed path.
- `rag status` shows the expected file / chunk count for the project after the next index cycle.
- Watcher picks up a subsequent edit (touch a `.md` file; watch the activity log).

## Escalation

Capture:
- Last 50 lines of `service.log`.
- `rag status` output.
- ACL / ownership of the problem path.
- Project config entry for that project (from `/api/projects/configured`).

## Related
- Architecture: [Watcher Flow](Architecture-Watcher-Flow).
- Reference: [Configuration Keys § Ignore rules](Reference-Configuration-Keys#ignore-rules).
- SOP: [Add a Project](Operational-SOPs-Projects-Add-a-Project) (ignore patterns).
