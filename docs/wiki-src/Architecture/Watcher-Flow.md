# Architecture: Watcher Flow

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-06-29 |
| **Related decisions** | `docs/decisions.md` — Decision 12 (thread safety), Decision 15 (watcher unavailable paths), Decision 17 (lifecycle-owned autostart) |

## Context

The watcher gives users "edit a Markdown file, search finds it seconds later" behavior without re-indexing whole projects. It runs as a daemon thread inside the service process — sharing the same `RLock` as the HTTP API — so it cannot conflict with user-triggered indexes.

## Decision link

- `docs/decisions.md` — thread safety, watcher unavailable paths.

## Diagram

```mermaid
flowchart TD
    Start([Service ready]) --> Init["Start watcher daemon thread<br/>subscribe to enabled project paths"]
    Init --> Loop{watchfiles event}
    Loop --> Kind{File kind?}
    Kind -- .ragignore --> Reload[Reload ignore rules cache]
    Kind -- .md --> Ignored{Matches ignore rules?}
    Kind -- other --> Skip[Ignore]
    Ignored -- yes --> Skip
    Ignored -- no --> Debounce[Debounce 3000 ms per path]
    Debounce --> Project["Identify project_id<br/>from content-root layout"]
    Project --> QIndex["Queue incremental index<br/>for this file"]
    QIndex --> Owner["Acquire QdrantOwner lock<br/>encode + upsert"]
    Owner --> State[Update state DB + activity log]
    Reload --> Loop
    State --> Loop
    Skip --> Loop
```

## Walkthrough

1. **Initialization.** The watcher's startup is owned by the service lifecycle: once the encoder + Qdrant are ready, the FastAPI lifespan calls `autostart_watcher()`, which starts the daemon thread and subscribes the `watchfiles` observer to all enabled project paths. This replaced an earlier delayed HTTP self-POST that could miss the readiness window and leave the watcher silently inactive. Autostart is idempotent (no duplicate threads) and respects desired-state — a watcher the user explicitly stopped is **not** auto-restarted.

2. **Event arrives.** `watchfiles` (Rust-based, uses OS primitives: inotify / FSEvents / `ReadDirectoryChangesW`) surfaces file create / modify / delete events.

3. **Kind check.**
   - `.ragignore` changed → reload that directory's ignore rules; no index action.
   - `.md` changed → continue.
   - Anything else → drop.

4. **Ignore filter.** The three-layer ignore engine (built-in defaults, config `[ignore].patterns`, per-directory `.ragignore` files) checks the path. Match → drop.

5. **Debounce.** A 3000 ms per-path debounce collapses editor save-storms (swap-file flurries, save-on-focus-loss) into one action.

6. **Project identification.** The file's first path segment relative to the content root is the `project_id`.

7. **Incremental index.** The file is hashed; if changed, re-chunked and re-encoded; points upserted; state DB updated. The lock is released between files so HTTP requests are not starved.

8. **Unavailable paths.** If a watched directory disappears (network share offline, USB unplugged), the observer logs a warning and retries every 60 s. The service does not crash.

9. **Project changes.** Adding, removing, enabling, or disabling a project restarts a running watcher with the new subscription set. The restart runs in a background thread and calls the lock-free start/stop internals (never the lock-acquiring route handlers), so it cannot self-deadlock on the watcher lock.

## Code paths

- `src/ragtools/watcher/observer.py` — `watchfiles` wrapper, debounce.
- `src/ragtools/service/watcher_thread.py` — daemon thread, retry/give-up lifecycle.
- `src/ragtools/service/routes.py` — `autostart_watcher()`, desired-state, lock-free start/stop internals, derived `state`.
- `src/ragtools/service/app.py` — the lifespan invokes `autostart_watcher()`.
- `src/ragtools/indexing/scanner.py` — ignore-rule reload on `.ragignore` change.
- `src/ragtools/service/owner.py` — shared RLock.

## Edge cases

- **Bulk change (git pull, IDE rename)** — debounce coalesces per path, but a single pull touching 500 files produces 500 queued indexes; they serialize behind the lock.
- **File moved across projects** — treated as delete from the old project and add to the new.
- **Symlinks** — `watchfiles` follows them by default; avoiding infinite loops is the user's responsibility.
- **Permission denied on a subdirectory** — logged and skipped. See [Watcher Permission Denied](Runbooks-Watcher-Permission-Denied).
- **Watcher thread dies** — within a session the thread retries with exponential backoff up to `_MAX_RETRIES`, then gives up (state `gave_up`, a desktop toast, and a `watcher_gave_up.json` marker). A give-up is no longer silent: it shows in `state` on `/api/watcher/status`, the watcher row in `rag doctor` / `/api/system-health`, and `degraded` on `/health`. The service *process* is recovered cross-process by the Task Scheduler watchdog, and a fresh process re-arms autostart. In-process auto-restart of a *given-up* thread (without a service restart) remains a deliberate non-goal.

## Invariants

- The watcher thread does not hold the `QdrantOwner` lock across files — only during encode + upsert of one file.
- The watcher subscribes only to enabled projects.
- An explicit user stop (`desired_run` False) is respected: lifecycle autostart and project-edit restarts will not re-start a watcher the user deliberately stopped (a service restart re-arms it).
- `.ragignore` changes always reload the ignore cache before the next index attempt.
