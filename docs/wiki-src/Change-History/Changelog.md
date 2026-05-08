# Change History: Changelog

| | |
|---|---|
| **Owner** | TBD (proposed: docs lead) |
| **Last validated against version** | 2.5.2 |
| **Last reviewed** | 2026-04-19 |
| **Format** | Loosely follows [Keep a Changelog](https://keepachangelog.com). Semantic versioning. |

> This page is the human-facing summary. The authoritative record is git tags plus `RELEASING.md` commit messages. Earlier versions (<v2.4.1) can be enumerated via `git tag --sort=-version:refname` once the repo's `safe.directory` issue is resolved — the entries below are what is known from code and existing documentation; older versions are placeholders until git history can be mined.
>
> **Wiki-vs-repo gap:** v2.5.0 shipped a substantial release (supervisor process, Windows Task Scheduler watchdog, desktop-toast notifications, system-tray icon, 22-tool MCP surface with per-tool access control, auto-backups, crash history) but the full diff has not yet been back-filled into this wiki. The v2.5.1 entry below documents only the changes made on top of v2.5.0. For the full v2.5.0 delta see [`CHANGELOG.md`](https://github.com/taqat-techno/rag/blob/main/CHANGELOG.md) in the repo.

## [Unreleased]

Changes on `main` not yet tagged.

---

## [2.5.5] — 2026-05-08

Packaging-only hotfix on top of v2.5.4. Closes the two install-flow gaps surfaced by live testing right after v2.5.4 shipped: the watchdog Scheduled Task wasn't healed on upgrade, and the tray icon didn't appear without a logout/restart.

### Fixed
- **Existing watchdog Scheduled Task is now repaired on upgrade.** v2.5.3 introduced the silent-VBS launcher (`RAGTools-Watchdog.vbs` + `wscript.exe` action) but the Inno Setup `[Run]` block never invoked `rag service watchdog install` post-install, so users who had opted into the watchdog on a pre-v2.5.3 install kept seeing a console window flash every 15 minutes. The installer now runs `rag.exe service watchdog install` *only when an existing `RAGTools Watchdog` Scheduled Task is detected* (new `HasRAGToolsWatchdogTask()` Inno Setup `Code` Check function — uses `schtasks /query` exit code). This re-registers the task with the silent launcher via `schtasks /create /f`. **Users who never opted into the watchdog do not get a new task installed** — current product policy preserved.
- **Tray icon now appears immediately after install/upgrade — no logout/restart required.** Previously the installer wrote `RAGTools-Tray.vbs` to the Startup folder but never launched it; the tray would only appear at next Windows login. The installer now invokes `wscript.exe` on the freshly-written Startup VBS once post-install (hidden + nowait via Inno Setup `Flags: runhidden nowait`). The VBS's built-in 15-second `WScript.Sleep` lets explorer.exe settle before `Shell_NotifyIcon` runs, and the launch is gated on `Tasks: startup` so we never invoke a non-existent VBS for users who declined autostart registration.

### Why these were not caught earlier
v2.5.3's changelog claimed "upgrading and letting the installer re-register" healed the watchdog, but the installer didn't actually do that — the line was missing. The tray-after-install gap was already present in v2.5.0+ but was masked first by the broken pystray bundle (v2.5.0..v2.5.3) and then only became visible once v2.5.4 fixed the bundle. Both gaps are install-flow issues; no `rag.exe` code changes are needed in this release.

### Unchanged from v2.5.4
- All Phase A API contracts (`/health` 200 fields, `/api/watcher/status` observability fields, `scale.level` enum, `rag service status` exit codes 0/1/2).
- The silent-watchdog VBS launcher code path itself (`service/watchdog.py`).
- The 15-second tray autostart delay and `tray.log` rotating file handler.
- The `pystray` + `PIL` bundle inclusion via `[dev,build,tray]` install + `rag.spec` `hiddenimports`.

### Tests
- Full suite: 567 passed, 1 skipped. No new tests — the change is Inno Setup `[Run]` and `[Code]` only, exercised by manual install validation post-CI.

Hotfix on top of v2.5.3. The new `tray.log` introduced this morning surfaced a latent build-pipeline bug that had been silently breaking the system-tray icon on every release since v2.5.0.

### Fixed
- **System-tray icon now actually appears in the bundled installer.** v2.5.0 through v2.5.3 shipped with `pystray` and `Pillow` missing from the PyInstaller bundle, because `release.yml` installed `[dev,build]` only — the `[tray]` extra (which carries `pystray>=0.19.5` + `Pillow>=10.0.0`) was never present in the CI build environment. Running `rag tray` from a packaged install therefore always failed with `ModuleNotFoundError: No module named 'pystray'`, returned exit code 2, and exited silently. The icon never appeared in the system tray for any user who didn't have `pystray` already installed in a separate dev environment. Two-part fix: (a) `release.yml` now installs `[dev,build,tray]` on all three platforms; (b) `rag.spec` lists `pystray`, `pystray._win32`, `PIL`, `PIL.Image`, `PIL.ImageDraw` in `hiddenimports` so PyInstaller bundles them even though `tray.py` imports them lazily.

### Why this wasn't caught earlier
The Phase A `tray.log` rotating file handler shipped in v2.5.3 wrote the import-error trail straight to `…\RAGTools\data\logs\tray.log` on first attempt — the entire diagnostic chain that would normally have flagged this in v2.5.0 didn't exist before this morning. The portability audit catches code patterns, not "shipped extra missing from bundle"; that gap is now documented as a follow-up (CI smoke test on the built artifact, e.g. `rag tray --help` on the post-build executable).

Bundle release: a high-priority Windows UX bugfix (silent watchdog + reliable tray autostart) plus the Phase A API/contract pass that pins `/health`, `/api/watcher/status`, and `scale.level` as stable contracts for downstream consumers (`rag-plugin`, admin panel, external monitors). One CLI behavior change with a loud note below.

### Added
- **`/health` 200 — `version` + `watcher_running`.** Both fields are additive (Decision 16). `version` lets clients detect mismatched RAG Tools versions without a second round trip; `watcher_running` is the cheap one-bit summary of the watcher daemon thread.
- **`/api/watcher/status` — `last_started_at`, `last_error`, `last_error_at`, `consecutive_failures`.** Promotes watcher retry/error state from local-variable scope to thread-instance attributes guarded by a dedicated `_state_lock`. Older clients reading only `running` / `paths` / `project_count` continue to work unchanged.
- **Tray runtime log file.** A rotating `tray.log` is now written under `…\RAGTools\data\logs\` whenever the tray runs. Since the autostart VBS launches the tray with stdout/stderr going nowhere, this is the first time a silent tray failure is recoverable from disk. Captures startup begin/end, pystray import errors, icon-registration milestones, and any uncaught exception.
- **Decision 16 — API contracts are additive-only.** New ADR in `docs/decisions.md` pinning `scale.level` to `{ok, approaching, over}` as a stable enum, the `/health` 200 key set, and the `rag service status` exit codes.
- **Reference: HTTP API** — full response-shape documentation for `/health` (200 / 503 / 5xx), `/api/watcher/status` (with the new observability fields), and the `scale.level` thresholds with recommended consumer treatment per level.
- **`docs/wiki-src/`** two-layer documentation source + publish script + CI dry-run workflow (Phase 1). Full wiki content across Start Here, Core Concepts, Architecture, Operational SOPs, Development SOPs, Runbooks, Reference, Standards & Governance, Templates, Change History (Phases 1-9). First wiki release.

### Changed
- **`rag service status` exit codes — 0 / 1 / 2.** Was always-0 in every state (running, starting, AND down) — CI scripts polling the command had no machine-readable signal. Now: `0` running or transiently starting, `1` down, `2` internal command error. **Behavior change**: any CI that relied on `rag service status` always returning 0 will start failing. Treat this as a correctness fix; the always-0 contract was broken.

### Fixed
- **Watchdog Scheduled Task no longer flashes a console window every 15 minutes.** The `RAGTools Watchdog` task now runs `wscript.exe RAGTools-Watchdog.vbs` (a silent VBS launcher generated alongside the PID files at `…\RAGTools\data\RAGTools-Watchdog.vbs`) instead of invoking the console-subsystem `rag.exe` directly. Re-running `rag service watchdog install` (or upgrading and letting the installer re-register) heals existing affected machines — `schtasks /create /f` overwrites the old visible task in place. The watchdog's actual health logic is unchanged; only the launch wrapper is new.
- **Tray icon now reliably appears after Windows login.** `RAGTools-Tray.vbs` now sleeps 15 s before invoking `rag tray`, outwaiting `explorer.exe`'s systray initialisation. Before the fix, `Shell_NotifyIcon(NIM_ADD)` could lose the early-login race and the tray would exit silently, leaving the user with no icon despite a correctly registered autostart entry.

### Tests
- 12 new tests for the Windows UX bugfix (silent-watchdog VBS contract, tray VBS startup-delay contract, tray-logger setup) plus 15 new Phase A tests (`/health` shape, `/api/watcher/status` observability fields, `scale.level` enum closure, `rag service status` exit codes for all 4 states, watcher state-snapshot helpers under concurrent-writer stress). Full suite: 567 passed, 1 skipped.

---

## [2.5.2] — 2026-04-19

Two small UX fixes reported after the v2.5.1 stable install:

### Added
- **Service-started desktop toast.** Fires once per OS boot after the service is fully up. Dedup via `psutil.boot_time()` + a persistent `boot_marker.json` so routine restarts (crash respawn, supervisor bounce, user-initiated) don't re-fire inside the same boot. Respects the existing `desktop_notifications` toggle.

### Fixed
- **Tray icon missing after reboot.** The installer now invokes `rag.exe tray install` during install and `rag.exe tray uninstall` on removal — previously only the *service* login-startup VBScript was registered, so after reboot the service came up but the tray did not. The underlying `tray_startup.install_tray_task()` code path was correct since v2.5.0; the installer just wasn't calling it.

### Tests
- 5 new tests in `test_notify.py` covering the boot-dedup branches (540 passed, 1 skipped).

---

## [2.5.1] — 2026-04-19

Patch release on top of v2.5.0. Three things shipped:

### Added
- **Linux (Ubuntu) packaging.** First-class release artifact `RAGTools-{version}-linux-x86_64.tar.gz` produced by a new `build-linux` CI job on `ubuntu-22.04`. CPU-only torch (no CUDA dead weight), XDG-compliant data directory (`$XDG_DATA_HOME/RAGTools` or `~/.local/share/RAGTools`), tray-clipboard fallback chain (`wl-copy` → `xclip` → `xsel`). Linux is now **READY** in the portability gate (was `SOURCE_ONLY`).
- **MCP `add_project` tool.** Project-tier, default-ON, proxy-only write tool with 2-second cooldown. Agents can onboard a user-provided folder end-to-end without leaving chat. Deletion remains CLI-only by design. See [Reference: MCP Tools](Reference-MCP-Tools).
- **Plugin system documented.** Resolves Q-5. ragtools adopts the **Claude Code plugin** model via the [taqat-techno marketplace](https://github.com/taqat-techno/plugins); [`rag-plugin` v0.6.0](https://github.com/taqat-techno/plugins/tree/main/rag-plugin) is the operational-console reference implementation. See [Add a New Plugin](Development-SOPs-Plugins-Add-a-New-Plugin).

### Fixed
- **Installer upgrade-over-running-app.** Pre-v2.5.1 upgrades required manual Task Manager intervention because the tray / supervisor / MCP workers held file handles on `rag.exe`. Installer now sets `CloseApplications=yes` + `SetupMutex` + `taskkill /F /IM rag.exe /T` as a belt-and-suspenders fallback, on both install and uninstall paths.

### Notes
- Wiki was still validated against 2.4.2 before this release; only the plugin and v2.5.1 deltas above are back-filled here. The full v2.5.0 feature set (supervisor, watchdog, tray, 22-tool MCP, notifications, backups, crash history) remains to be documented — tracked as a future sweep.

---

## [2.4.2] — current

### Added
- Comprehensive admin-panel HTTP routes including `/api/projects/configured`, `/api/config` (with hot-reload vs restart-required field split), `/api/watcher/start|stop|status`, `/api/map/points|recompute`, `/api/mcp-config`, `/api/activity`, `/api/crash-history`, `/api/shutdown`.
- Scale warnings in `rag doctor` via `compute_scale_warning` — alerts when local-mode Qdrant points approach or exceed the single-process limit.
- `rag doctor` collection enumeration and crash-history surfacing.

### Changed
- MCP server logs strictly to stderr so stdout remains clean for MCP stdio transport.
- `compact` output mode on `/api/search` for token-efficient MCP responses.

### Notes
- **Dev vs installed port:** `_default_service_port` returns `21420` (installed) or `21421` (dev) to allow both modes to coexist on the same host.
- `rag index` is **service-required** — no direct-mode fallback. Other dual-mode commands (`rag search`, `rag status`, `rag projects`, `rag rebuild`) retain the full fallback.

---

## [2.4.1] — schema gate baseline

### Changed
- Qdrant collection schema and/or SQLite state DB schema changed in a way that makes the **new reset paths refuse to run on pre-v2.4.1 data** (see [Pre-v2.4.1 Reset Blocked](Runbooks-Pre-v2-4-1-Reset-Blocked)).

### Migration
- Upgrade from pre-v2.4.1 requires a clean-slate approach until the exact schema delta is documented (tracked as [Q-7](Development-SOPs-Documentation-Open-Questions)). See [Pre-v2.4.1 to Current migration guide](Change-History-Migration-Guides-Pre-v2-4-1-to-Current).

### Notes
- This version is the **reset gate** — v2.4.1+ is required to use `rag reset [--data | --nuclear]`.

---

## [< 2.4.1] — earlier history

Earlier versions are not yet enumerated here. To populate:

1. Resolve the `safe.directory` issue and run `git tag --sort=-version:refname`.
2. For each tag, summarize the delta from `RELEASING.md` commit messages and the changelog entry that was current at tag time.
3. Add one section per version above this line.

## Conventions for new entries

- New entries go under **[Unreleased]** at the top.
- At release time, [Release Checklist](Development-SOPs-Release-Release-Checklist) renames `[Unreleased]` to `[X.Y.Z]` with the date.
- Categories (where used): **Added**, **Changed**, **Deprecated**, **Removed**, **Fixed**, **Security**, **Migration**, **Notes**.
- Keep entries terse. Link to SOPs, runbooks, or ADRs for detail.

## Related

- [Release Checklist](Development-SOPs-Release-Release-Checklist).
- [Architecture Decisions](Standards-and-Governance-Architecture-Decisions) — the ADRs (ADR-1..15) locked 2026-04-06 predate this changelog format.
- `RELEASING.md` and `docs/RELEASE_LIFECYCLE.md` (in-repo).
