# SOP: Hook Safety Rules

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Partial — see banner below |

> **Partial documentation.** The primary hook surface Reg participates in is Claude Code's `UserPromptSubmit` hook, wired by the rag-plugin bundle (not this repo). Implementation details for how the plugin writes to Reg's activity log are tracked as [Q-4 on the Open Questions page](Development-SOPs-Documentation-Open-Questions). This SOP documents what is in-repo today and the safety rules any hook — plugin-side or future in-repo — must honor.

## Purpose
Constrain how Reg accepts inbound signals from Claude Code hooks and other lifecycle events — specifically, what they may write, what they must not do, and how to roll them back.

## Scope
In-repo observability / lifecycle surfaces:
- `activity_log` (`src/ragtools/service/activity.py`) — in-memory event queue feeding the admin-panel log (see [Reference/HTTP-API `GET /api/activity`](Reference-HTTP-API#activity)).
- `crash_history` (`src/ragtools/service/crash_history.py`) — persistent crash markers (see [Reference/HTTP-API `GET /api/crash-history`](Reference-HTTP-API#crash-history)).

Does not cover:
- Claude Code's `UserPromptSubmit` hook's internal logic (plugin-side; see Q-4).
- External webhook receivers — not implemented as of v2.4.2.

## Trigger
- Proposing a new hook integration (plugin-side or in-repo).
- Reviewing a PR that writes to `activity_log` or `crash_history`.
- Diagnosing noise in the admin-panel activity log.

## Preconditions
- [ ] Reviewer has read [Single-Process Invariant](Core-Concepts-Single-Process-Invariant) and [Service Lifecycle](Architecture-Service-Lifecycle).
- [ ] Proposed hook has a named owner and an opt-in user-facing toggle (if it emits beyond-local traffic).

## The rules

### R-1 — Hooks are observers, not actors

A hook may read Reg state (via the HTTP API) and write to local observability surfaces (`activity_log`, `crash_history`). It must not:
- Mutate the index.
- Alter config.
- Start, stop, or restart the service.
- Trigger network I/O beyond loopback.

If the desired behavior needs mutation, it belongs in a skill or CLI command, not a hook.

### R-2 — All writes go through the documented activity/crash surfaces

- For transient events that the admin panel should show: call `log_activity(level, source, message)` from `ragtools.service.activity`.
- For persistent failure markers (service crash, supervisor give-up): write via the `crash_history` module — never poke the underlying files directly.

Hooks **must not** create files in the data dir except via these modules. `crash_history` has a 30-day filter and a dismiss-with-`.reviewed` contract; bypassing it leaves stale markers that haunt the UI.

### R-3 — Localhost-only

Reg's service binds `127.0.0.1`. Hooks that reach the service must use `127.0.0.1:<service_port>` and `httpx` with short timeouts (≤ 5 s for reads, ≤ 120 s for index/rebuild). They must not attempt any public-network call.

### R-4 — Opt-in for anything that logs user content

If a hook's output might contain a user prompt, query, file path, or other potentially sensitive data:
- The hook must be **off by default**.
- Toggle must be user-controllable via `rag:rag-config` (or the direct admin-panel equivalent).
- Documentation must call out what is logged and where.

### R-5 — No side channels in error paths

Error paths must not write to the filesystem outside the data dir, spawn subprocesses, or attempt retries that exceed the caller's timeout. A failed hook logs an error and returns — it does not cascade.

### R-6 — Every hook maps to an SOP

If the hook is user-facing, its behavior must be described in an Operational SOP. Hidden behavior is a support liability.

## Steps — proposing a new hook

1. **Write the intent down.** What event, what is written, what triggers it off.
2. **Verify R-1..R-6 apply.** Any rule that does not fit is a design smell — stop and reconsider.
3. **Implement against the documented surfaces.** `log_activity` or `crash_history`; never ad-hoc files.
4. **Wire the opt-in.** Default off. User-controllable.
5. **Document.** Add an entry to the SOP in question; update this page if new rules emerge.
6. **Test.** Hook enabled + disabled; hook error path; hook under service restart.

## Validation / expected result
- Admin-panel activity log reflects the hook's events when enabled, nothing when disabled.
- No files in the data dir outside of `logs/`, `qdrant/`, `index_state.db`, crash markers.
- Service unaffected by hook errors.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Activity log floods | Hook writes per keystroke instead of per event | Add debouncing or coarse event granularity. |
| Crash markers accumulate without dismissal | Hook creates markers outside the `crash_history` module | Refactor to use the module; clean up orphan files manually once. |
| Service cannot stop cleanly | Hook keeps a non-daemon thread or long network call alive | Set threads as daemon; respect shutdown events. |
| User reports "Reg is logging my prompts" unexpectedly | Hook shipped on by default or opt-in UI is misleading | Flip default to off; fix the UI. |

## Recovery / rollback
- Disable the hook via `rag:rag-config` or the admin-panel setting.
- If the hook is in-repo: revert the PR that introduced it.
- Dismiss stale crash markers via `POST /api/crash-history/{key}/dismiss`.

## Related code paths
- `src/ragtools/service/activity.py` — `log_activity`, `activity_log.get_recent`.
- `src/ragtools/service/crash_history.py` — `list_unreviewed_crashes`, `dismiss_crash_marker`.
- `src/ragtools/service/routes.py` — `/api/activity`, `/api/crash-history`, `/api/crash-history/{dismiss_key}/dismiss`.
- `.claude/skills/<plugin>/rag-config` (plugin-bundled) — user-facing opt-in controls.

## Related commands
- `/rag:rag-config` — toggle hook-related options.
- `GET /api/activity`, `GET /api/crash-history`.

## Change log
- 2026-04-18 — Initial draft (partial — Q-4 outstanding).
