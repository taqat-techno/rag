# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

_Nothing yet._

---

## [3.1.0] — 2026-07-28

### The migration this release performs

An installation upgraded from v2 has no `storage_backend` and no
`collection_strategy` — because those keys did not exist when its config was
written, not because anyone chose the values they imply. This release treats
that absence as a **legacy default to migrate**, not a decision to preserve:

* implicit legacy defaults become **`managed + per_project`**, the recommended
  v3 architecture (falling back to `embedded` where the packaged engine cannot
  run, with the reason recorded rather than inferred);
* an **explicit** value is a decision and is preserved — including an explicit
  `embedded` or `shared`, and everything describing an external server: url,
  credentials, ports;
* `[migration].adopted` records which values the migration chose, because
  absence is readable exactly once — after the write, nothing can distinguish a
  value the product picked from one the user did.

**This is a destructive index transition and it takes hours on a real corpus.**
The old shared index is retired and every project and framework corpus is
rebuilt under the new layout. While that runs:

* `/health` reports `status: "migrating"` with per-unit counts, named failures
  and a retry path — it does **not** claim readiness;
* searches raise rather than returning an empty result. An empty answer from a
  half-built index is indistinguishable from "your query matched nothing", at
  the exact moment the content genuinely is not there;
* a restart resumes: progress is per project and per framework, committed as
  each finishes, so completed work is never repeated and failed work is never
  skipped;
* completion is **validated** before the old storage is deleted — a unit that
  held points before and holds none after fails validation;
* `rag upgrade --resume` retries only what is incomplete.

**The managed engine now actually ships.** No previous release packaged a Qdrant
binary, so `managed` was a mode the product could describe and never enter —
every attempt fell back to embedded, correctly reporting a reason nobody read.
The pinned 1.15.5 engine is fetched per platform against checksums committed to
the repository, and the build fails if it is absent.



Activates the v3 architecture and makes the Windows upgrade path provable.

The headline defect is that **the v2→v3 configuration migration had never run.**
`migrate_config()` shipped in 3.0.0, was correct, and was fully unit-tested —
and every reference to it outside its own tests was a re-export. So an
installation upgraded to v3 kept a v2 configuration, the runtime fell back to v2
code defaults, and per-project collections, managed storage and framework
corpora stayed unreachable behind a file nothing rewrote. A clean v3 install
landed in the same place, so this was never only an upgrade defect.

Fixing that alone would not have held: the configuration writers actively undid
it. `_save_projects_to_toml` wrote `version = 2` unconditionally from sixteen
production call sites — every project add, edit, mode change, ignore rule and
dependency change, from CLI, admin panel and MCP alike — so a migrated config
was demoted by the user's next edit and re-migrated on the following boot,
forever.

**3.0.2 was never released; its changes are included here.**

### Added
- **`rag storage`** — `show`, `backend` and `strategy`. The storage engine and
  collection layout were readable everywhere and settable nowhere: no CLI
  command, no field on the config API, no control in the admin panel. The v3
  architecture could be described but not chosen, and the scale warning
  recommended moving to server mode — something the installed product had no
  way to do. Both setters run preflight, state the re-index cost and require
  confirmation.
- **`rag selfcheck`** — verifies the *installation*, not the executable: its
  version, the `ragw.exe` sibling, the uninstall registry entry, that no owned
  process runs from outside the install directory, that every autostart
  registration targets it, and that a responding service reports this version.
  Exits non-zero so a caller can refuse to claim success. The installer runs it
  at `ssDone` and reports a mixed state rather than success.
- **`/health` reports the configuration state** (`config_version`,
  `config_state`) and flags `config_migration_failed`, so "am I actually running
  a v3 configuration?" is answerable without reading the file.
- **A real upgrade regression test from BOTH 2.7.0 and 3.0.1.** The 2.7.0 leg
  cannot fail: its tasks target `rag.exe`, which even the old single-image kill
  handled. 3.0.1 points both tasks at `ragw.exe` — the state a field machine is
  now in — and only that leg proves the fix.
- **The real uninstaller is finally exercised.** Every previous uninstall check
  tested something else: a `pip uninstall` from a sandbox venv, and elsewhere a
  fake `unins000.exe` containing the two bytes `MZ`. `verify_real_uninstall.py`
  runs the actual Inno uninstaller and sweeps with the upgrade's own detection.

### Fixed
- **Configuration migration runs, once, before anything resolves storage.**
  `QdrantOwner.__init__` opens the store and creates collections while
  constructing itself, so the seam is before `Settings()` is read, not after.
  Idempotent, atomic, backed up, lock-protected against concurrent starts, and
  degrades loudly instead of refusing to boot.
- **No production writer can lower the schema version.** One definition of
  `CONFIG_VERSION`; writers preserve what the file declares and never invent
  one.
- **The scale warning describes the risk that exists.** It was computed from the
  *total* across collections — the wrong arithmetic the moment there is more
  than one, and a guaranteed false alarm under the per-project layout that fixes
  the problem. It now reports the largest single collection, names it, respects
  the engine's real capabilities in `rag doctor` and `/api/system-health`, and
  only recommends actions the product can perform.
- **One unreadable path no longer stops indexing for every project.** A junction
  the OS refused to describe (`WinError 448`) raised out of the scan, past a
  per-project loop with no handler, into the single blanket `except` around the
  whole startup sync — where it was logged as "non-fatal" while indexing stopped
  for all projects on every boot. Refusals are now skipped, counted and
  reported, and a project that cannot be scanned costs only itself.
- **Junction loops no longer multiply the index.** `rglob` follows reparse
  points; a self-referential junction produced 23 copies of one file, bounded
  only by the path-length limit. Files are de-duplicated by filesystem identity.
- **The installer stops every image it owns — and only the ones it owns.**
  `ForceKillRagProcesses()` killed `rag.exe` alone, while 3.0.1 had moved both
  tasks to `ragw.exe`, so the processes an upgrade most needs to stop were the
  ones it left alone. It now covers `rag.exe`, `ragw.exe` and `qdrant.exe`, and
  matches by **executable path** rather than image name: `/IM qdrant.exe` would
  kill an `external` backend the user runs themselves, or another product's
  binary of the same common name.
- **Scheduled tasks are ended before processes are killed**, so `RestartOnFailure`
  cannot start a replacement between the kill and the copy.
- **Nothing in the upgrade path waits forever.** The pre-install graceful stop
  runs the *previous* release's binary, whose behaviour this installer cannot
  control; unbounded, one that never returns stopped the upgrade before a single
  file was touched, silently. All such calls are now time-limited, and the
  installer writes an Inno log that CI dumps on timeout. Measured effect: an
  upgrade that ran 40 minutes and timed out now completes in 23 seconds.
- **Windows RestartManager no longer cancels the upgrade it was meant to help
  (`CloseApplications=no`).** RM enumerates open files *before* the installer's
  pre-install phase and shuts them down *after* it, so it acts on a list as
  stale as that phase is long. Anything it then fails to close — including a
  process the installer had already stopped correctly — raises an
  Abort/Retry/Ignore box, and `/SUPPRESSMSGBOXES` answers with the **default**,
  which is Abort. From Inno's own log:

  ```
  Shutting down applications using our files.
  Some applications could not be shut down.
  Defaulting to Abort for suppressed message box (Abort/Retry/Ignore)
  User canceled the installation process.
  ```

  So a silent upgrade was refused, rolled back, and exited 5 — not because
  anything was wrong, but because two mechanisms were closing the same
  processes and the loser cancelled the install. RM predates this installer
  having a kill of its own; it has one now, scoped by executable path, which
  runs before `[InstallDelete]` and `[Files]` and **verifies its own result**
  (kill, wait, re-scan, up to three rounds) rather than assuming a stop request
  was honoured.

### Note on the reported incident — corrected
The 3.0.2 notes stated that the reported v3.0.1 upgrade failure "did not occur".
**That was wrong, and it was wrong because it described the wrong machine.** The
evidence cited (an unchanged `unins000.dat`, no `3.0.x` in the service log, no
`ragw.exe`) was accurate for the development machine, where 3.0.1 was indeed
never installed. The report came from a different machine sharing the same
workspace over Syncthing. On that machine 3.0.1 installed **successfully** — a
matching installer hash, a fully replaced `_internal`, correct registry and
`/health`, in three minutes. The storage alert it showed was not an install
failure at all; it was the correct behaviour of a v2 configuration that nothing
had migrated, which is the defect this release fixes.

**Artifacts remain unsigned.** Signing is still open and is the highest-value
remaining work.

---

## [3.0.1] — 2026-07-27

Fixes four defects found by installing 3.0.0 over 2.7.0 on a real machine.
**Three of the four are reachable only on the upgrade path**, which is why a
release with three green clean installs shipped a service that could not start.

Upgrading from 3.0.0 needs no special steps. If 3.0.0 left you with a service
that crash-loops on `ImportError: safetensors>=0.8.0 is required`, this release
is the fix — the installer now removes the stale payload rather than layering
on top of it.

### Fixed
- **The installer no longer inherits the previous bundle's payload.** It
  extracted the new `_internal` over the old one without removing it, so package
  manifests accumulated across releases — 86 `.dist-info` directories, 27
  packages carrying more than one version. `importlib.metadata` returns the
  first normalized-name match, and `0.7.0` sorts before `0.8.0`, so a stale
  `safetensors-0.7.0.dist-info` beside the correct 0.8.0 made `transformers`
  fail its import-time version guard. The service crashed 76 times and the
  supervisor stopped respawning it. Not user-recoverable: the bundle is
  self-contained, so the `pip install -U` the error message recommends could
  never have helped.
- **Autostart no longer opens a console window at login.** `rag.exe` is a
  console-subsystem image and Task Scheduler creates the process itself, so
  Windows gave it a console — two terminal windows on the desktop at every
  login, one streaming uvicorn logs, and closing it killed the service.
  `CREATE_NO_WINDOW` does not apply: it is a `subprocess.Popen` flag governing
  processes ragtools spawns, and no task-XML setting suppresses a console. The
  bundle now ships `ragw.exe`, a GUI-subsystem sibling — the
  `python.exe`/`pythonw.exe` pattern — and both registrations point at it.
- **Uninstalling can no longer destroy a configuration it failed to preserve.**
  Choosing to delete user data removed `config.toml` along with the index, with
  no backup and nothing in the Recycle Bin. The index costs hours to rebuild;
  the project list, ignore rules and per-project modes cannot be rebuilt at all.
  The configuration is now copied to a timestamped directory outside the data
  root before anything is deleted, the prompt separates what is replaceable from
  what is not, and a backup that fails cancels the deletion.
- **`rag index` no longer reports a healthy indexing run as a stuck one.** A
  queued job waited a fixed 900 s for the index lock and then asserted that
  "another indexing run is stuck", with no evidence. During a startup sync of 25
  projects that run was perfectly healthy and simply slower. Waiting now ends on
  *silence* rather than elapsed time: the running index publishes a heartbeat,
  a job waits as long as that heartbeat keeps moving, and the error says which
  rule fired and what was measured.
- **The post-install tray launch no longer depends on a file v3 deletes.** It
  invoked `RAGTools-Tray.vbs` from the Startup folder — written by v2 and removed
  by v3's upgrade — so on a fresh install it launched nothing and the tray icon
  did not appear until the next login.

### Added
- `scripts/verify_bundle.py` — asserts properties of the **built** bundle:
  that `ragw.exe` is genuinely GUI-subsystem (read from the PE header), that
  `rag.exe` is still console-subsystem, and that no distribution in `_internal`
  ships two versions. Run by `release-validation.yml`. Both fixed defects were
  invisible to the source suite, and 3.0.0 shipped four artifacts that passed
  every name and size check.
- `tests/test_installer_contract.py` — the two Inno Setup invariants as
  structural assertions. Seven of its nine checks fail against 3.0.0.

---

## [3.0.0] — 2026-07-26

**Upgrading rebuilds your search index.** Projects, configuration and source
files are preserved; the vector store is not convertible. See
`docs/release/UPGRADE.md`.

### Added
- **Managed Qdrant** (pinned 1.15.5, no Docker), supervised by the product with
  a `/readyz` gate and version verification. Search at 89k points went from
  ~2.1 s to 39–54 ms, and the local-mode scale warning no longer applies.
- **One collection per project** (`proj_<uuid>`, survives rename and move).
  Cross-project isolation is a boundary rather than a payload filter.
- **Shared dependencies** — declare a vendored framework once in a catalog and
  select it from any project. Indexed once, shared by every project on the same
  build; search returns both, labelled `project` or `framework`.
- **Shared dependencies page**, chunk inspector on the map, running-job progress,
  storage diagnostics.
- **`ragtools.platform`** — one adapter per OS. Windows Task Scheduler, systemd
  user units, launchd agents, XDG autostart.
- **`ragtools.upgrade`** — scan, config migration, PATH repair, pre-flight gates,
  reconciliation gates, resumable state with an explicit rollback boundary.
- **Service-owned maintenance schedule** replacing OS keepalive tasks.
- MCP: `list_dependencies`, `add_dependency`, `set_project_dependencies`,
  `remove_dependency`, `find_definition`, `secret_audit`.

### Changed
- `dependency_paths` is now a legacy input, adopted into the catalog at load.
- Health reports the **store**, not just the process: a reachable service with an
  unreachable Qdrant is `degraded`, not green.
- Indexing streams in bounded windows — peak memory 2.46 GB → ~1.2 GB flat.
- `typer[all]` → `typer` (the extra no longer exists and warned on every install).

### Removed
- **The Windows watchdog scheduled task** (462 lines). Restart-on-failure is
  native to Task Scheduler, systemd and launchd; a polling task that flashed a
  console every fifteen minutes and had no non-Windows counterpart is replaced by
  the platform's own supervision.
- **`.vbs` shims from autostart.** Hiding a console is a process-creation flag,
  not a reason to ship an interpreted shim, so neither the service nor the tray
  registration goes through one any more. The Start Menu shortcut still uses
  `launch.vbs` — it starts the service if needed and opens the panel, which is a
  launcher's job rather than a workaround.

### Fixed
- **Windows autostart could not be registered by a standard user.** Registration
  went through `schtasks /sc onlogon`, which builds a logon trigger with no
  `<UserId>` — "at logon of *any* user" — and only an administrator may create
  that. A per-user product therefore needed elevation to start itself. It now
  registers from a task XML naming the account, which needs no elevation. Also
  settled, having been left at Windows' defaults: the service no longer refuses
  to start on battery, is no longer stopped when the machine switches to
  battery, and is no longer killed at the default 72-hour execution limit. The
  task now carries the native `RestartOnFailure` policy the retired watchdog was
  imitating by hand.
- Removing autostart also prunes the now-empty task folder — Task Scheduler
  keeps it after its last task is deleted, so uninstall left a `RAGTools` node
  behind in the scheduler tree.
- **Autostart started the service on the wrong data root on Windows.** The
  registration declares `RAG_PROFILE=installed`, which a systemd unit and a
  launchd plist can carry and a scheduled task cannot — so on Windows a source
  install fell back to the `dev` profile and the service that came up at login
  served a different index. The profile now travels as `rag service run
  --profile`, which every mechanism carries.
- `/api/projects/{id}/dependencies` reported only the legacy `dependency_paths`
  field, so a project declaring through the catalog read as `declared: []`
  while holding a linked, working corpus. It now also returns
  `declared_dependencies`.
- `StartLimitIntervalSec` was emitted in `[Service]`, where systemd ignores it —
  the crash-loop protection did nothing.
- A zombie process reported as alive, so a dead service read as running and stale
  PID files were never cleaned.
- Force-terminate sent `SIGTERM` on POSIX, so it could not kill a hung process.
- Index identity is verified before a state DB is trusted (caught a real
  27,895-chunk divergence).
- Framework corpora are deduplicated by resolved path when no build id exists —
  two projects each vendoring `<project>/odoo` previously shared one collection.
- Batched upserts and deletes; `TIME_WAIT` no longer exhausts the ephemeral range.

### Known limitations
- macOS has not been executed on real hardware; the launchd agent is schema-
  validated only. `.github/workflows/release-validation.yml` covers it once
  runners are enabled.
- Artifacts are unsigned. macOS builds require Developer ID and notarization
  before they are installable by anyone who did not build them.

---

## [2.7.0] — 2026-06-30 — Per-project "dev mode" + retrieval & security hardening

Source-code indexing is now a **per-project** toggle, not just the global `index_source_code`. Mark a project as a code/dev project (index its source code & config) or docs-only, independent of the global default — settable when adding a project, editable on existing ones, and via the CLI + an MCP tool. Secret-bearing files are always excluded regardless.

### Added
- **`ProjectConfig.index_source_code`** — a tri-state per-project override (`None` = inherit the global / `True` = index code & config / `False` = docs only). Existing projects inherit (zero migration).
- **Admin panel** — a "Dev mode" `<select>` on the add **and** edit project forms, plus a Code/Docs badge per row in the list.
- **CLI** — `rag project dev-mode <id> on|off|inherit` and `rag project add --mode inherit|code|docs`.
- **MCP tool** — `set_project_dev_mode(project, enabled, confirm_token)` (gated, default-on). Disabling purges code chunks, so it requires `confirm_token == project`; routes through the service HTTP API (single-process Qdrant).
- **API** — `index_source_code` on project create/update; a dedicated `POST /api/projects/{id}/dev-mode`; `/api/projects/configured` returns the raw + effective mode.

### Changed
- The scanner and the file watcher resolve `include_code` **per project**. The watcher deepest-match-attributes nested projects, so a code child under a docs parent is watched with its own mode (not the parent's).
- `_save_projects_to_toml` serializes via `model_dump(exclude_none=True)` so every `ProjectConfig` field persists (and a `None` override is omitted — `tomli_w` can't write `None`).

### Fixed
- Changing a project's effective dev mode triggers a **delete-aware** reindex (`reindex_project`), so disabling dev mode purges the project's now-excluded code chunks instead of leaving them stale.
- The project-list overlay spinner no longer gets stuck `active` and block the inline edit-form Save — an `outerHTML` swap (Edit button → edit form) detached the indicator's element so it was never cleared; the handler now clears all active section overlays after any htmx request.

### Tests
- +25 tests in `tests/test_dev_mode.py` across all layers (data model, persistence, pipeline, API/reindex, UI, CLI, MCP). Validated end-to-end against this repo's own code: index → search → toggle-off → purge.

### Added — retrieval & security hardening

- **Content-level secret redaction** (`secret_scan.py`). File-name exclusion is insufficient — secrets get pasted into READMEs/configs/source. The secret **value** is now masked at **index time** (never reaches the embedding or stored payload) and at **serve time** (masks values in points indexed before this shipped), while the **key name** is preserved so "which key does X use?" still answers. Provider patterns (Google/AWS/GitHub/Slack/Stripe/JWT/PEM) plus contextual `key = value` and labeled `Default:` / `API Key:` rules, with hex/base64 entropy floors to avoid false positives.
- **`secret_audit`** MCP tool + `GET /api/secret-audit` — reports `file:line` + rule name for secret material in the index, **never the value**, so you can locate and rotate.
- **`find_definition`** (`codegraph.py`) — cross-file code-graph v1: a symbol → likely definition sites (`file:line`). MCP tool + `GET /api/definitions`. Generic, LSP-complementary **discovery** (leads, not authority).
- **Source-class classification** (`source_class.py`) — orthogonal to Mode: tags each chunk **owned** / **vendored** / **generated**. `project_status` returns a `source_class_breakdown`; results carry the class so vendored/generated content can be visibly tagged and down-ranked.
- **Line anchors** (`chunking/anchors.py`) — every chunk now carries 1-based `line_start`/`line_end`; provenance renders `path:Lstart-end` for jump-to-source.
- **Chunk hygiene** (`chunking/hygiene.py`) — drops content-free chunks (separators, punctuation-only, near-empty) before embedding.
- **Code-first dev pipeline signals** — `dev_search` exposes a `code_indexed` flag and emits an explicit **"Docs mode — code not indexed"** notice, so an empty Project-Context result reads as "not indexed", not "feature absent".

### Changed — retrieval

- `SearchResult` and the search payload carry `line_start`/`line_end`, `source_class`, `chunk_type`, and symbol metadata; the formatter shows line spans, source-class tags, and reranks code-first by context priority (source code > APIs > workflows > architecture > docs).
- `project_status` adds `mode`, `mode_note`, `file_types`, `source_class_breakdown`, and a `stale` flag.

### Tests — hardening

- +11 test modules: `test_secret_scan`, `test_codegraph`, `test_source_class`, `test_chunk_hygiene`, `test_line_anchors`, `test_rerank_quality`, `test_search_metadata`, `test_failsafe_retrieval`, `test_generated_exclusion`, `test_dev_codefirst`, `test_coverage_status`. Full suite **868 passed, 1 skipped**.

---

## [2.6.0] — 2026-06-29 — Diagnostics & observability · lifecycle-owned watcher autostart (M3) · port-owner detection (L5)

### Added — Diagnostics & observability

- **Index-freshness detection (A-008).** `compute_index_freshness()` classifies
  `last_indexed` as never/fresh/stale/unknown against `stale_index_hours`
  (default 24); surfaced on `/api/status`, `/api/system-health`, and `rag doctor`.
- **Watcher health is no longer invisible.** `rag doctor` and `/api/system-health`
  now report watcher running/`last_error`; `/health` adds an additive
  `degraded` + `issues` signal (e.g. `watcher_not_running`) without changing the
  `status` liveness contract.
- **`rag doctor --json`** — stable machine-readable report (install_mode, service,
  index, freshness, watcher, projects, checks, recommended_actions) so tooling no
  longer parses the human table. New Watcher / Index-freshness / Project-path rows.

### Changed — Watcher autostart is lifecycle-owned (M3)

- **The file watcher now starts from the service lifecycle** (the FastAPI
  lifespan calls `autostart_watcher()`), replacing the delayed HTTP self-POST in
  `run.py` that could miss the readiness window and leave the watcher silently
  inactive. Startup is idempotent (no duplicate threads) and never fatal — a
  construct/start failure is recorded and surfaced, not raised.
- **An explicit user stop is respected.** A per-process desired-state flag means
  lifecycle autostart and the project-edit restart never re-start a watcher the
  user deliberately stopped. `/health` no longer flags a user-stopped watcher as
  `degraded`, and `/api/system-health` reports it as "stopped by user".
- **Richer watcher `state`** — adds `stopped` (user intent) and `autostart_failed`
  (a lifecycle autostart that could not construct/start the thread) on
  `/api/watcher/status`, `/api/system-health`, and `rag doctor`. All additive.

### Changed — service-status port-owner detection (L5)

- **`rag service status` no longer mistakes a foreign process for a healthy
  service.** A `200` on `/health` is only trusted as `ready` when the body
  carries the ragtools identity markers (`status=="ready"` + `collection` +
  `version`). A 200 that isn't ragtools-shaped, or any HTTP response on the port
  with no live ragtools PID, is reported as the additive status
  `port_occupied_foreign` (with a best-effort foreign PID); the CLI says so
  clearly and exits `1` (our service is not running). A ragtools `503` during
  startup with a live PID still reads as `starting`. The exit-code contract
  (`0` running/starting, `1` down, `2` internal error) is unchanged.

### Fixed

- **Watcher restart no longer self-deadlocks.** `_restart_watcher_if_running`
  (run after a project edit while the watcher is live) called the lock-acquiring
  route handlers while already holding the watcher lock — a re-entrant acquire on
  a non-reentrant `Lock` that hung the restart thread and every subsequent
  `/api/watcher/status` reader. It now calls the lock-free internals.
- **`/health` (and all routes) now return JSON on an uncaught 5xx** via a global
  exception handler, matching the documented contract (previously Starlette
  returned plain text).
- **Docs:** corrected the service-log path to `{data_dir}/data/logs/service.log`
  (was inconsistently `{data_dir}/logs/...`) in `CLAUDE.md` and `docs/decisions.md`.

---

## [2.5.5] — 2026-05-08

Packaging-only hotfix on top of v2.5.4 (no `rag.exe` code changes). Closes two install-flow gaps found in live testing.

### Fixed
- **Existing watchdog Scheduled Task is repaired on upgrade.** The installer now runs `rag.exe service watchdog install` *only when an existing `RAGTools Watchdog` task is detected* (new `HasRAGToolsWatchdogTask()` Inno check), re-registering it with the silent VBS launcher — no more console-window flash every 15 min. Users who never opted into the watchdog get no new task.
- **Tray icon appears immediately after install/upgrade.** The installer launches the freshly-written Startup VBS once post-install (hidden + `nowait`, gated on the `startup` task), so the icon no longer waits for the next Windows login.

---

## [2.5.4] — 2026-05-08

Hotfix on top of v2.5.3.

### Fixed
- **System-tray icon is now actually bundled.** v2.5.0–v2.5.3 shipped without `pystray` + `Pillow` in the PyInstaller bundle (`release.yml` installed `[dev,build]` only), so `rag tray` from a packaged install failed with `ModuleNotFoundError` and exited silently. Fix: `release.yml` installs `[dev,build,tray]` on all platforms and `rag.spec` lists `pystray` / `PIL` in `hiddenimports`.

---

## [2.5.3] — 2026-05-08

Bundle release: the Phase A API/contract pass plus Windows UX fixes.

### Added
- **`/health` 200 — `version` + `watcher_running`** (additive, Decision 16).
- **`/api/watcher/status` observability fields** — `last_started_at`, `last_error`, `last_error_at`, `consecutive_failures`. Older clients reading only `running` / `paths` / `project_count` are unaffected.
- **Decision 16 — API contracts are additive-only** — pins `scale.level` ⊆ `{ok, approaching, over}`, the `/health` 200 key set, and the `rag service status` exit codes.
- **Reference: HTTP API** documentation; first `docs/wiki-src/` wiki release; a rotating `tray.log` under `…\RAGTools\data\logs\`.

### Changed
- **`rag service status` exit codes — `0` / `1` / `2`** (was always-`0` in every state). Behavior change: any CI relying on the always-`0` contract will start failing — treat this as a correctness fix.

### Fixed
- **Watchdog Scheduled Task no longer flashes a console window every 15 min** — it runs `wscript.exe RAGTools-Watchdog.vbs` (a silent launcher) instead of the console-subsystem `rag.exe`.
- **Tray icon reliably appears after login** — the tray VBS sleeps 15 s to outwait `explorer.exe`'s systray initialisation.

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
