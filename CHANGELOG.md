# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.5.2] — 2026-04-19

Small, focused patch on top of v2.5.1 covering two issues reported
immediately after the v2.5.1 install:

### Added — Service-started desktop notification

- **New `notify_service_started` helper** in `service/notify.py`. Fires a
  desktop toast ("RAG Tools is running — Click to open the admin panel")
  once the service is fully up and serving `/health` after login.
- **Dedup is boot-scoped.** Uses `psutil.boot_time()` plus a
  `boot_marker.json` file in the data dir so routine restarts inside the
  same boot session (crash respawn, supervisor bounce, user-initiated
  restart) do not re-fire the toast. A genuine reboot advances the boot
  timestamp and the toast fires again.
- **Respects the existing `desktop_notifications` toggle** and the shared
  `DesktopNotifier` cooldown machinery — no new user-visible settings.
- Wired into `service/run.py:_post_startup()` so it runs for both direct
  and supervised modes.

### Fixed — Tray icon missing after reboot

- **Installer now registers the tray's login-startup VBScript.** Before
  v2.5.2 the installer only called `rag.exe service install`; the
  equivalent `rag.exe tray install` step was missing, so
  `RAGTools-Tray.vbs` was never written to the Startup folder. After
  reboot the service came up but the tray did not. v2.5.2 adds the
  matching `[Run]` entry (gated on the same "Start on login" checkbox
  as the service registration) and the symmetric `[UninstallRun]`
  cleanup so uninstall removes the tray VBScript too.
- No code change to `tray_startup.py` — the CLI command
  (`rag tray install` / `rag tray uninstall`) and the underlying
  `install_tray_task()` function were already correct. The fix is
  purely that the installer now invokes them.

### Tests

- 5 new tests in `test_notify.py` for the service-started helper: first-
  boot fires, same-boot dedup, new-boot re-fires, `desktop_notifications=
  False` suppresses, psutil-unavailable suppresses.
- Full suite: 540 passed, 1 skipped (+5 new).

### Notes

- Manual validation path for the tray fix: install v2.5.2 over v2.5.1,
  reboot, verify both `RAGTools.vbs` and `RAGTools-Tray.vbs` exist under
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`, and confirm
  the tray icon appears after login.

---

## [2.5.1] — 2026-04-18

Adds **first-class Linux (Ubuntu) packaging** to the release pipeline,
plus two targeted improvements discovered during v2.5.0 field use:
installer upgrade flow and an MCP `add_project` tool.

### Added — MCP `add_project` tool

- **`add_project(project_id, path, name?, enabled?)`** — new project-tier
  MCP tool so agents can onboard a folder the user asked about without
  leaving chat. Proxies `POST /api/projects` with full server-side
  validation: path must exist, be a directory, and not duplicate an
  existing project ID or path. Auto-indexes 3 s after the response.
- **Default ON** in the project-tools tier (alongside `run_index`,
  `reindex_project`, etc.). 2-second write-cooldown guard.
- **Proxy-only**: returns `SERVICE_DOWN` in degraded/direct mode —
  config writes require the running service.
- Admin panel "MCP Tool Access" card now lists `add_project` under
  Project tools so users can opt it out per-install.
- Deletion remains CLI-only (`rag project remove`) — destructive by
  design (wipes indexed Qdrant data).

### Fixed — Installer upgrade flow

- **Installer now force-closes running RAG processes before copying
  files.** Pre-v2.5.1, upgrading over a running install required ending
  `rag.exe` tasks manually in Task Manager because the tray / supervisor
  / MCP processes held file handles on `rag.exe`. Three-layer fix:
  - Inno Setup `CloseApplications=yes` + `RestartApplications=no` —
    Windows Restart Manager dialog offers to close running instances.
  - Belt-and-suspenders `taskkill /F /IM rag.exe /T` after the graceful
    `service stop`, covering any processes Restart Manager missed
    (tray, supervisor, orphaned workers). Runs on both install and
    uninstall paths.
  - `SetupMutex` prevents two installers running concurrently.
- **User data in `%LOCALAPPDATA%\RAGTools` is never touched** — only the
  install directory gets replaced.

### Added — Linux platform support

- **`RAGTools-{version}-linux-x86_64.tar.gz`** produced on every tag by a
  new `build-linux` job in `release.yml` (runs on `ubuntu-22.04`).
  PyInstaller one-dir bundle, same layout as the macOS tar.
- **Linux arm in `_get_app_dir`** — Ubuntu/Debian/Fedora/Arch resolve the
  app data directory to `$XDG_DATA_HOME/RAGTools`, falling back to
  `~/.local/share/RAGTools`. Honours the XDG Base Directory spec.
- **Cross-platform tests** — `test.yml` matrix now includes
  `ubuntu-22.04` alongside `windows-latest` and `macos-14`, with
  per-platform pip and HuggingFace cache paths.
- **Clipboard fallback chain** — tray's "Copy URL" action on Linux tries
  `wl-copy` (Wayland) → `xclip` → `xsel` via `shutil.which`, logging a
  warning instead of crashing when none are available. Previous behaviour
  shelled out to `xclip` unconditionally, which failed silently on
  minimal distros.
- **README Linux install section** with Ubuntu-primary instructions
  covering bundle extraction, clipboard package hint, and data-dir
  location.

### Notes

- **Platform status (Portability Audit Release Gate):**
  Windows: `READY`, macOS: `READY`, Linux: `READY` (previously
  `SOURCE_ONLY`).
- **Deferred on Linux**: system tray (Linux has no cross-distro tray
  equivalent with the pystray backends we use), login-startup helper
  (no systemd-user integration yet), Task Scheduler watchdog (Windows-
  only by design).
- **No behavioural changes** on Windows or macOS. `_get_app_dir`,
  `get_data_dir`, and `get_config_write_path` produce identical results
  on those platforms.

---

## [2.5.0] — 2026-04-18

A big reliability + agent-tooling release. Closes the "silent failure" class
from Mahmoud's April field report and opens the agent's MCP surface to
project-scoped operations with per-tool user-controlled access.

### Added — Reliability

- **Supervisor process** respawns the service on crash with exponential
  backoff (5 retries, 5 s → 32 s). After the budget is exhausted, writes
  `supervisor_gave_up.json` for post-mortem.
- **Fatal-crash recorder** in `run.py` captures any exception that would
  otherwise vanish silently, writing a full traceback + memory snapshot to
  `service.log` and a machine-readable `last_crash.json` marker.
- **Windows Task Scheduler watchdog** — auto-registered on packaged first
  launch. Runs `rag service watchdog check` every 15 minutes. If the
  supervisor AND service are both down, launches the service again. Covers
  OOM + reboot + supervisor-death cases the respawn loop can't.
- **Watcher crash detection** — file watcher now writes a `watcher_gave_up`
  marker after 5 failed restarts and fires a desktop notification.
- **Crash banner** in the admin panel renders dismissable alerts for
  service-crash / supervisor-gave-up / watcher-gave-up markers until
  reviewed. Dismiss renames the marker `*.reviewed.json` so it's preserved
  for post-mortem but no longer shown.
- **Launcher active wait** — `launch.vbs` now polls `/health` for up to
  30 s after starting the service, with a restart-once fallback for the
  "crashed shortly after startup" pattern.

### Added — Notifications

- **Desktop toast notifications** (Windows via `winotify`, macOS via
  `osascript`) for:
  - Service crash
  - Supervisor gave up
  - Watcher gave up
  - Project first-index complete
  - Knowledge-base rebuild complete
  - Qdrant collection crossing scale thresholds (approaching / over the
    20 k soft ceiling)
- **AUMID registration** so Windows shows the RAG Tools logo next to the
  notification title, WhatsApp / Slack style. Idempotent on first toast.
- **Notification toggle + test button** on the admin-panel Settings page.
  Respects the toggle: disabled → test button returns "disabled" instead
  of firing.
- **1-hour cooldown** on scale warnings so the watcher's periodic
  re-indexing doesn't spam the action center.

### Added — System-tray icon

- **`pip install ragtools[tray]`** extra bundles `pystray` + `Pillow`.
- `rag tray` — runs an independent tray process. Survives service crashes.
- `rag tray install/uninstall/status` — Windows Startup-folder registration
  so the tray auto-starts on login, silently (no console window).
- Brand logo + colored status badge (Slack/Discord style) for healthy /
  starting / down / unreachable / unknown.
- Right-click menu: Open admin panel, Copy admin URL, Restart service,
  Stop service, View logs, View backups, Quit tray.
- Grace window (45 s) for cold-start before switching to "unreachable".

### Added — Auto-backup

- New `src/ragtools/backup.py` module snapshots the SQLite state DB via
  SQLite's online-backup API (WAL-safe) before:
  - `rag rebuild` (full drop + re-index)
  - Project removal (admin panel or `rag project remove`)
  - Manual `rag backup create`
- `rag backup {list, create, prune, restore}` CLI with timestamped
  directories and `manifest.json` per snapshot.
- Automatic prune to keep the 10 most recent snapshots after each backup.
- `restore_backup` takes a pre-restore safety snapshot, so the restore is
  itself reversible.

### Added — MCP: per-tool access control

- Single `rag-mcp` server with per-tool access dict in `settings.mcp_tools`.
  Core tools always registered; optional tools are user-granted from the
  admin panel's new **"MCP Tool Access"** card.
- **Tiered defaults**: project tools (5 reads + 4 writes) enabled by default —
  the primary agent workflow tier. Debugging / diagnostics tools (9) disabled
  by default — operator-facing, granted individually when troubleshooting.
- Disabled tools are not registered at all: invisible to the agent, zero
  token cost, zero chance of accidental invocation.
- Fresh-install tool count: **3 core + 9 project = 12**. With all grants: **21**.
- **Admin UI groups tools by purpose**: "Project tools" (checked by default,
  shown first) + "Debugging / diagnostics" (unchecked, shown second), with
  per-group "Toggle group" quick-enable buttons.

### Added — MCP: Phase 1 diagnostics (9 optional tools)

- `service_status`, `recent_activity(limit?, level?)`, `tail_logs(source, limit?)`,
  `crash_history()`, `get_config()`, `get_ignore_rules()`, `get_paths()`,
  `system_health()`, `list_indexed_paths(project?, limit?)`.
- All return JSON envelopes `{ok, mode, as_of, data|error, hint?}`.
- First-line docstrings include WHEN / DO NOT USE guardrails to keep
  selection cost low.

### Added — MCP: Phase 2 project inspection (5 optional tools)

- `project_status(project)` — one-call orientation: enabled, path,
  file/chunk counts, last indexed, ignore-pattern count.
- `project_summary(project, top_files?)` — top files by chunk count.
- `list_project_files(project, limit?)` — state-DB file roster per project.
- `get_project_ignore_rules(project)` — the layered ignore rules for one
  project.
- `preview_ignore_effect(project, pattern)` — dry-run simulation: which
  files WOULD be excluded if this pattern were added. Does not modify
  configuration.

### Added — MCP: Phase 3 project writes (4 optional tools, user-granted)

- `run_index(project)` — incremental index, idempotent.
- `reindex_project(project, confirm_token)` — drop and rebuild one
  project's data. Requires `confirm_token == project_id` to defeat blind
  prompt-injected calls; auto-backed-up via the state-DB snapshot path.
- `add_project_ignore_rule(project, pattern)` / `remove_project_ignore_rule`
  — persist ignore-rule changes to `ragtools.toml` without an implicit
  reindex (agent must call `run_index` or `reindex_project` separately).
- All writes log to the activity feed with `source="mcp"` so the admin UI
  shows exactly what the agent did.

### Added — MCP: multi-project search

- `search_knowledge_base(projects=["a","b","c"])` — OR-semantics union
  search across a list of project IDs. One call instead of N.
- Backed by a new `projects` query-string parameter on `GET /api/search`
  and Qdrant `should`-filter support in `Searcher`.

### Added — CLI

- `rag project add-from-glob "D:/Work/*/docs"` — bulk-add projects from a
  glob pattern with a preview table and confirmation prompt. Supports
  `--exclude`, `--name-prefix`, `--dry-run`, `--yes`.
- `rag doctor` now includes **Login startup** and **Watchdog** rows so a
  silent auto-registration failure is impossible to miss.

### Changed

- `stop_service()` waits only 6 s for the process to exit after accepting
  HTTP shutdown, then force-kills. Cut worst-case stop time from ~35 s to
  ~16 s and unblocks external callers with tight `subprocess.run(timeout=10)`.
- Click 8 glob-expansion disabled globally via `windows_expand_args=False`
  so CLI args like `"D:/Work/*"` arrive intact at the Python side.
- Admin-panel Settings page adds **Notifications** card + **MCP Tool
  Access** card. Save handlers persist via `PUT /api/config`.
- `launch.vbs` completely rewritten with `IsHealthy`, `LogLine`,
  `WaitForHealthy` helpers + single-restart fallback.

### Fixed

- Stale `service.pid` after a hard crash is now self-healed: `_read_pid`
  removes files whose PID no longer exists, so `rag service status` and
  downstream callers see accurate "not running" state.
- Qdrant scale warnings surface in the admin UI, in `rag doctor`, and as
  desktop toasts when the 20 k soft ceiling is approached or crossed.
- `_save_projects_to_toml` writes absolute paths so packaged-mode
  post-restart reads don't resolve to the installed service's
  `%LOCALAPPDATA%\RAGTools` directory from the wrong CWD.

### Tests

- **498 passing / 1 skipped** (was 253 at v2.4.2 release). +245 tests
  covering supervisor, watchdog, tray, notify, crash history, backup,
  MCP per-tool registration, project scoping, confirm-token guard,
  multi-project search filter.

### Safety (MCP writes)

- All writes require proxy mode (service running); refuse cleanly with
  an actionable hint in degraded mode.
- `reindex_project` requires `confirm_token == project_id` — defeats
  blind injected calls that don't know the specific project the user
  is working on.
- Destructive operations (`add_project`, `remove_project`, `shutdown`,
  `backup restore`) are **permanently CLI-only** — never reachable from
  the agent.

---

## [2.4.2] — 2026-04-17

Patch release. macOS cross-platform support, MPS-memory crash fix,
full README rewrite.

## [2.4.1] — 2026-04-17

Critical hotfix: post-restart config path resolution in packaged mode
was reading from `C:\Windows\System32` (VBScript-inherited CWD).
Workaround: `get_config_write_path()` always uses
`%LOCALAPPDATA%\RAGTools\config.toml` in packaged mode.

## [2.4.0] — 2026-04-16

- Split-lock indexing (search stays responsive during re-index)
- Cross-file batch encoding
- 4-layer admin-UI loading states
- UI cleanup, codebase cleanup (6.4k lines removed)

## [2.3.0 and earlier]

See the `v2.0.0 → v2.3.1` history section of
[docs/RELEASE_LIFECYCLE.md](docs/RELEASE_LIFECYCLE.md) for the pre-changelog
history.
