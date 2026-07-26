# ragtools v3.0.0 — Release, Upgrade and Cross-Platform Plan

**Status:** implementation-ready plan. No code was written, changed, committed or published to produce it.
**Predecessor:** `RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md` §3.2 lists *"Release engineering, installers, signing,
publication"* and *"Upgrading the installed application"* as **explicitly out of scope, a separate later phase.**
This document is that phase.
**Verdict:** **REQUIRES_DECISION** — see §14. Implementation can begin on Phase 0–2 immediately; six decisions
are load-bearing for packaging and for the release gate.

---

## 1. Current-state investigation (measured, not recalled)

Every row was probed on 2026-07-26 on this machine. Source tags: `[runtime]` live probe, `[repo]` file/git,
`[fs]` filesystem.

### 1.1 What is released versus what exists

| Fact | Evidence | Source |
|---|---|---|
| Released version is **v2.7.0** | `git tag --sort=-v:refname` → `v2.7.0` at `ad4969f` on `master` | `[repo]` |
| Installed build is that release | `%LOCALAPPDATA%\Programs\RAGTools\rag.exe` + `unins000.exe` (Inno) | `[fs]` |
| **All v3 work is UNCOMMITTED** | worktree `rag-v3-dev` HEAD == `ad4969f` (the v2.7.0 tag); `git status` = **155 changed/new files** | `[repo]` |
| Installed build predates S16 | `GET /identity` → **404** on both `:21420` and `:21422` | `[runtime]` |
| v3 tracker | `tasks/todo.md`: S0/S2/S3 done; **S1 open** (A3, A6/B26, G1 gate); S4–S16 "core done, integration REMAINING" | `[repo]` |

> **This is the single most important finding.** There is no v3 branch history — the entire architecture
> (managed Qdrant, per-project collections, framework catalog, dependency UI, chunk inspector) exists only as
> uncommitted working-tree state on one machine. Every release activity depends on fixing that first.

### 1.2 Live footprint the upgrade must replace

| Component | Observed | Source |
|---|---|---|
| Services | `:21420` `markdown_kb` **44,019 files / 147,344 points, scale=over**; `:21422` `royal_preps_kb` 647 / 2,841 | `[runtime]` |
| Processes | 6 × `rag.exe`: 2 × `service run`, 1 × `service supervise`, 1 × `tray`, 2 × `rag serve` (MCP) | `[runtime]` |
| Scheduled task | `RAGTools Watchdog` (Ready) — the only product task | `[runtime]` |
| Startup folder | `RAGTools.vbs` **and** `RAGTools-Tray.vbs` — two autostart entries | `[fs]` |
| **PATH** | install dir appears **16 times**; `where rag` resolves a **different casing** (`Programs\ragtools\rag.exe`) | `[runtime]` |
| Data dir | `%LOCALAPPDATA%\RAGTools` — **1.2 GB**: `qdrant/`, `index_state.db`, `backups/`, `logs/`, 3 PID files, a stray `RAGTools-Watchdog.vbs` | `[fs]` |
| Installed config | 15 projects; **no** `collection_strategy`, **no** `storage_backend`, **no** `[[dependencies]]`, **no** `qdrant_binary` — a pure v2 config | `[fs]` |
| Dev artifacts | `%LOCALAPPDATA%\rag-v3-e2e` (my dev store + qdrant binary), `rag-v3-dev` worktree — **must never be touched by an installer** | `[fs]` |

The 16 duplicated PATH entries are a *shipped defect*: `installer.iss`'s `NeedsAddPath` check has failed on every
upgrade and appended again. It is simultaneously the "stale PATH" and "multiple installations" failure mode.

### 1.3 Platform readiness of the current codebase

| Module | Lines | Windows mechanisms | Linux/macOS | Verdict |
|---|---|---|---|---|
| `service/startup.py` | 205 | Startup folder, VBS, `wscript`, `schtasks` | **none** | Windows-only |
| `service/tray_startup.py` | 126 | Startup folder, VBS, Task Scheduler | **none** | Windows-only |
| `service/watchdog.py` | 462 | Task Scheduler, VBS | **none** | Windows-only |
| `service/supervisor.py` | 286 | — | — | portable |
| `service/process.py` | 402 | — (PID file + subprocess) | — | mostly portable |
| `service/notify.py` | 530 | `winreg` | **darwin + linux branches** | the only 3-platform module |
| `storage_managed.py` | — | — | **full matrix**: win/darwin/linux × x86_64/arm64, Windows-ARM64 explicitly refused | **ready** |
| `tray.py` | — | pystray | pure core is UI-agnostic; `darwin`/`win32` branches present | partial |

**Packaging is Windows-only**: `installer.iss` (Inno Setup), `rag.spec` (PyInstaller), `scripts/build.py`
(downloads model → PyInstaller → Inno). There is no Linux or macOS artifact of any kind.

### 1.4 Prior failure catalogue

`RAG_CLAUDE_AI_ENVIRONMENT_AND_SESSION_INVESTIGATION_REPORT.md` (1,665 lines) carries findings **F-01…F-30+**,
including F-02 (a memory pointing at a dead port), F-03 (a fix recorded as APPLIED that existed only on a
now-dead service), F-04 (one project = 55% of the collection, 7.4× over the local-mode limit), F-05 (`rag-mcp`
not on PATH so the project MCP server fails at every startup). §6 maps preventive controls for every failure
mode this release must not reproduce.

---

## 2. Gap analysis against the approved v3 plan

Legend: **D** delivered and integrated · **C** core exists, integration incomplete · **M** missing.

| § | Approved capability | State | Evidence / remaining work |
|---|---|---|---|
| 2.2 | Pluggable storage `embedded\|managed\|external` | **D** | `storage.resolve_backend`; managed proven live at 1.15.5 |
| 2.3 | Native managed Qdrant, Docker never required | **D** | supervised, `/readyz` + version gate, loopback-only |
| 2.4 | One machine-level service owns one of each runtime | **C** | single-instance guard not enforced across profiles; 6 processes observed |
| 2.5 | Every project gets its own collection | **D** | `proj_<uuid>`, `CollectionRouter`, live 2 projects |
| 2.6 | Framework corpus per build identity, indexed once, linked to many | **D** | `fw_<slug>_<digest>`; live: 1 corpus, 2 projects, 71,542 chunks |
| 2.7 | Default retrieval = project + linked frameworks | **D** | router read-set + `collection_scoped` search |
| 2.8 | Project code outranks framework | **C** | `rerank.py` priority exists; installed-vs-available tiering **M** |
| 2.9 | Cross-project explicit only, fail closed | **D** | `resolve_scope`, `UnknownProject`, verified 403 on foreign collection |
| 2.11 | Client Profiles | **C** | `profiles.py` + `ProfileStore` + capability groups; per-profile MCP process **M** |
| 2.12/13 | Admin MCP preserved, default-off; server-side authz | **C** | registration-time gating + `_ops_capability_error`; per-profile process **M** |
| 2.14 | Onboarding without manual TOML | **C** | catalog + project forms cover most; §15 wizard **M** |
| 2.15 | `docs`/`code`/`general` modes | **D** | `mode_indexes` |
| 2.16 | Jinja2 + htmx UI | **D** | plus Diagnostics, Dependencies, chunk inspector |
| §10 | Managed lifecycle: install-time binary, boot order, restart supervision | **C** | supervisor exists; **binary provisioning at install is M** |
| §11 | Project identity + 3 lifecycle verbs (archive/remove/delete-collection) | **C** | UUID + rename/move-safe naming done; verbs partial |
| §18 | Hybrid dense/sparse/structural retrieval | **M** | `fusion.py` core only; not wired |
| §19 | Reranking backend | **C** | `assert_model_compatible` exists; not enforced on every collection open |
| §21 | Full entity tables + composite `(collection_id, file_path)` key | **C** | migration ladder v1→v2 only |
| §22 | Watcher + incremental | **C** | works; **duplicate-watcher and profile-ownership invariants M** |
| §27 | Ports, identity, registry, single-instance | **C** | `/identity` wired; **registry file + named mutex M** |
| §29 | Migration feasibility | **C** | `reindex.plan_reindex` + quality gate + e2e proven; **legacy-snapshot planner M** |
| §34 | Cross-platform validation | **M** | **never executed on Linux or macOS** |
| §32 | Playwright browser validation | **C** | ad-hoc MCP-driven checks this session; no committed spec suite |

**Delta this session added beyond the tracker:** managed backend wired into `resolve_backend`; per-project
routing across all 38 call sites; framework sync/release reconciliation; dependency catalog + links + MCP tools;
streaming index (2.46 GB → ~1.2 GB flat); index-identity guard; batched upserts/deletes; storage-aware `/health`;
job progress surfacing; chunk inspector. Suite: **1,656 passed / 14 skipped**.

**Release-relevant conclusion:** the *product* is close. The *release engineering* is near-zero for two of three
platforms, and the work is uncommitted.

---

## 3. Target release architecture

### 3.1 Principle

**One product core, thin OS adapters.** No platform conditional may live outside `ragtools.platform.*`. Today 13
modules contain `sys.platform` dispatch; the target is **one**.

```
┌─────────────────────────────────────────────────────────────┐
│ CORE (platform-neutral, ~all existing modules)              │
│  config · dependency_catalog · identity · registry          │
│  storage(+managed) · collection_router · frameworks         │
│  owner (index/search/sync) · runtime_store · job_worker     │
│  service.app/routes/pages · integration.mcp_server          │
└───────────────────────────┬─────────────────────────────────┘
                            │  resolve_adapter()  (refuses unknown platform)
┌───────────────────────────┴─────────────────────────────────┐
│ ragtools.platform.*        windows.py │ linux.py │ darwin.py │
│  paths · autostart · session_autostart · service_control     │
│  scheduler · process · fswatch · notify · uninstall          │
│  singleton                                                   │
└──────────────────────────────────────────────────────────────┘
```

### 3.2 Adapter contracts

| Adapter | Contract | Windows | Linux | macOS |
|---|---|---|---|---|
| `paths` | install/data/log/config/cache roots | `%LOCALAPPDATA%\Programs\RAGTools`, `%LOCALAPPDATA%\RAGTools` | `~/.local/share/ragtools`, `~/.config/ragtools` (XDG) | `~/Library/Application Support/RAGTools` |
| `autostart` | register/unregister/verify **service** at login | Task Scheduler, at-logon, hidden | `systemd --user` unit + `enable-linger` | LaunchAgent plist |
| `session_autostart` | same for **tray** | Task Scheduler, at-logon | XDG `~/.config/autostart/*.desktop` | LaunchAgent, `LSUIElement` |
| `service_control` | start/stop/restart/status | adapter → supervisor | `systemctl --user` | `launchctl kickstart/bootout` |
| `scheduler` | register maintenance timers | Task Scheduler | systemd `.timer` | launchd `StartCalendarInterval` |
| `process` | detached spawn, signal, PID validity | `CREATE_NO_WINDOW` | `setsid` | `setsid` |
| `fswatch` | backend tuning + limits | RDCW | inotify watch-limit probe + guidance | FSEvents |
| `singleton` | one runtime per profile | named mutex | abstract-socket / flock | flock |
| `uninstall` | remove every artifact this adapter created | — | — | — |

**Rule:** every `autostart`/`scheduler` adapter implements `verify()` returning the *set of registrations found*,
so "exactly one" is an assertion the product can make about itself rather than a hope.

### 3.3 Service model decision (recommended)

**User-scoped service on all three platforms, not a machine/system service.** The product indexes the logged-in
user's projects and stores under a per-user data dir; a SYSTEM service cannot see `%LOCALAPPDATA%` or `~`.

* Windows — Task Scheduler task, trigger *at logon of this user*, hidden, `Restart on failure`.
* Linux — `systemd --user` unit with `Restart=on-failure`; `loginctl enable-linger $USER` for headless.
* macOS — `~/Library/LaunchAgents/com.ragtools.service.plist`, `KeepAlive={SuccessfulExit:false}`.

**The 462-line Windows watchdog is deleted.** Restart-on-failure is a native capability of all three mechanisms.
This removes the custom watchdog task, its VBS launcher, and the terminal-flash class of defect outright.

---

## 4. Platform lifecycle designs

### 4.1 Windows

| Concern | Design |
|---|---|
| Autostart (service) | One Task Scheduler task `RAGTools\Service`, at-logon, `-WindowStyle Hidden` equivalent via a native launcher **exe**, not `.vbs`/`wscript` |
| Autostart (tray) | One task `RAGTools\Tray`, at-logon, user session only |
| Removed | `RAGTools Watchdog` task, `RAGTools.vbs`, `RAGTools-Tray.vbs`, `RAGTools-Watchdog.vbs` in the data dir |
| Console flashing | No `cmd.exe`/`wscript` in any trigger; launcher built with `console=False` |
| PATH | Installer **de-duplicates** and writes exactly one entry; uninstall removes exactly its own |
| Single instance | Named mutex `Global\ragtools-<profile>` |
| Elevation | Per-user install; no admin required |

### 4.2 Linux

| Concern | Design |
|---|---|
| Unit | `~/.config/systemd/user/ragtools.service` (`Type=simple`, `Restart=on-failure`, `RestartSec=5`) |
| Headless | `loginctl enable-linger` documented and offered by the installer |
| Tray | XDG autostart entry, installed **only** when a desktop session is detectable; headless installs skip it silently |
| Maintenance | `ragtools-maintenance.timer` → `.service` (or in-service scheduler; see §6.3) |
| Watcher | inotify watch limit probed at startup; a project exceeding it degrades to polling **and says so in health** |
| Packaging | `.tar.gz` (portable), `.deb`, `.rpm` |

### 4.3 macOS

| Concern | Design |
|---|---|
| Agent | `~/Library/LaunchAgents/com.ragtools.service.plist`, `RunAtLoad`, `KeepAlive` on failure |
| Tray | `com.ragtools.tray.plist`, app bundle with `LSUIElement=1` (no Dock icon) |
| Signing | Developer ID Application + `codesign --options runtime` (hardened), **notarized + stapled** |
| Gatekeeper | unsigned builds are unusable in practice → signing is a release blocker, not a nicety |
| TCC | Full Disk Access may be needed to index some locations; detected and surfaced, never silently failing |
| Packaging | `.app` inside a signed `.dmg`, plus a `.pkg` for managed fleets |

---

## 5. Upgrade, cleanup, migration and re-index strategy

### 5.1 Detection of previous installations

`rag upgrade scan` (also run by the installer) enumerates:

* **L1** Inno per-user install `%LOCALAPPDATA%\Programs\RAGTools` (**and lowercase spelling**)
* **L2** `%ProgramFiles%\RAGTools` / `(x86)`
* **L3** pip/venv installs — any `rag` on PATH resolving outside L1/L2
* **L4** data dir `%LOCALAPPDATA%\RAGTools`
* **L5** Linux/macOS equivalents (defined now so v3→v4 has a contract)
* **X** *excluded from all mutation*: `RAGTools-dev`, `rag-v3-e2e`, any `RAG_PROFILE != installed` data dir,
  any path inside a git worktree. Dev instances are never candidates for cleanup.

Output is a machine-readable plan the user can inspect **before** anything is stopped.

### 5.2 Stop order (safety-ordered, idempotent)

1. Tray (user session) → 2. MCP proxy processes → 3. Watcher (in-service flag) → 4. Job worker (drain, mark
`interrupted`) → 5. HTTP service → 6. Supervisor → 7. Managed Qdrant (graceful, then SIGTERM, then kill).

Then **assert**: no listener on the product ports, no `rag`/`qdrant` process from L1/L2, PID files stale-cleared.
Refuse to continue if any survives — a mixed-version machine is the failure mode being prevented.

### 5.3 What is preserved, migrated, or rebuilt

| Data | Action | Reason |
|---|---|---|
| `config.toml` projects, ids, paths, modes, ignore rules | **migrate** | user intent; irreplaceable |
| `dependency_paths` | **migrate → catalog** `[[dependencies]]` + links | validator already does this, read-only and idempotent |
| Client profiles DB | **migrate** if schema-compatible, else rebuild with defaults + warn | credentials must not be silently dropped |
| Activity/audit history | **preserve** (append-only) | forensics |
| `index_state.db` | **rebuild** | keyed for the old layout; identity guard will refuse it anyway |
| Qdrant `markdown_kb` (147,344 pts) | **rebuild** | 1 collection → N; embedded → managed; no safe transform |
| Framework collections | **create** | did not exist before |
| Durable jobs | **preserve records, requeue nothing** | history kept, work re-planned |
| Old binaries, `_internal/`, VBS, watchdog task, PATH dupes, PID files | **delete** | replaced by the new architecture |
| **Source projects / user repos** | **never touched** | hard invariant, asserted in tests |

**The old data dir is renamed, not deleted** → `RAGTools.pre-v3-<version>-<n>/` and retained until an explicit
`rag upgrade commit` (or a configured retention). This is what makes Boundary A rollback real.

### 5.4 Controlled full re-index

Pre-flight gates (all must pass, all reported):

* **Disk** — measured: 147,344 points ≈ 1.2 GB. Budget `3 × estimate + 2 GB` free; refuse otherwise.
* **Memory** — streaming indexer is O(window); require ≥ 2 GB available.
* **Ports** — target ports free, or auto-select and record in identity.
* **Project paths** — every registered project resolves; missing ones are reported and skipped, never silently dropped.
* **Model** — `assert_model_compatible` against the pinned embedding model.

Flow: create collections → **per-project durable job** (progress `done/total`, cancellable) → `sync_frameworks`
for declared dependencies → reconcile.

Reconciliation gates (§9 acceptance):

1. per collection: `state_db_chunks == qdrant_points` (exact count)
2. zero framework-path files inside any `proj_*` collection
3. two projects sharing a build identity → **one** `fw_*` collection
4. a scoped search from project A returns **zero** documents belonging to project B
5. retrieval quality: `compare_to_baseline` does not regress recall@5 / MRR versus `tests/fixtures/eval_baseline.json`

**Success is not reported until every project is reconciled or explicitly listed as failed with a named recovery
action.** A partially-migrated machine reports `degraded` with the exact remaining set.

### 5.5 Rollback boundaries

| Boundary | Position | Capability |
|---|---|---|
| **A — pre-data** | new binaries installed, new data dir not yet written | **Full rollback.** Restore old binaries, re-register old autostart, old data dir untouched. |
| **B — post-data** | first write to the new store | **Forward-only.** Recovery = resume/repair. Old data dir still present for manual salvage until `upgrade commit`. |

Crossing B is explicit and logged. `rag upgrade status` always states which side of B the machine is on — the
question "can I go back?" must never require reading code.

---

## 6. Tray, watcher and scheduled-action lifecycle

### 6.1 Tray

* Starts via `session_autostart` only where a desktop session exists; **headless installs never require it.**
* Reflects: service up/down, storage reachable, watcher running, indexing (with progress), degraded, error.
  Distinct icon states for *down* vs *degraded* — today's binary up/down is why a broken watcher looked healthy.
* Every action (open dashboard, health, start/stop/restart, re-index, logs, exit) goes through
  `service_control` + the HTTP API. **The tray never spawns a service directly** — that is the duplicate-process
  defect. Where the service is absent, the tray asks the adapter to start it, then polls.
* Tray exit ≠ service exit, and the menu says so.

### 6.2 Watcher

* Owned by the service runtime, started by it, never by a separate scheduled task.
* **Exactly one effective watcher per runtime profile** — asserted at startup via `singleton`; a second is
  refused, logged, and surfaced in `/health`.
* Reconnects after: service restart, machine restart, sleep/resume, filesystem disconnect, managed-Qdrant
  restart. Backoff with a ceiling; every reconnect attempt is visible.
* Debounce + batch; handles create/modify/delete/rename/dir-move, ignore rules, dependency exclusions, and
  config changes (project added/removed/mode change/dependency link change).
* **Never** runs an index while `_index_mutex` is held — it takes the non-blocking path and re-arms.
* Failure is durable (job record + health `issues[]`), never a silent stop.

### 6.3 Scheduled actions

**The service owns the schedule; the OS owns only "start me at login".** This removes the noisy keepalive task.

| Action | Owner | Cadence | Lock | On failure |
|---|---|---|---|---|
| Stale-job recovery | service | startup + hourly | job idempotency key | mark `interrupted`, surface |
| State⇄Qdrant reconciliation | service | daily | index mutex | `degraded` + named recovery |
| Storage health probe | service | 60 s (5 s TTL cache) | none | `/health` `storage_unreachable` |
| Framework corpus refresh | service | weekly / on demand | index mutex | job failure record |
| Index maintenance (stale vectors, orphan collections) | service | weekly | index mutex | job failure record |
| Log rotation, crash-marker prune | service | daily | none | log only |
| Update check | service | weekly, **opt-in** | none | silent |
| **Autostart at login** | OS adapter | login | platform native | platform restart policy |

Machine-level (service) actions and user-session (tray) actions are separate registrations with separate names,
so `verify()` can prove there is one of each and no orphan of either.

---

## 7. Packaging and release pipeline

### 7.1 Artifacts

| Platform | Artifact | Notes |
|---|---|---|
| Windows x64 | `RAGTools-Setup-3.0.0-x64.exe` (Inno) | keep Inno; fix PATH + drop VBS |
| Windows ARM64 | `…-arm64.exe` | **managed Qdrant unavailable** (asset matrix refuses); installer must default to `embedded` and say so |
| Linux x86_64/aarch64 | `.tar.gz`, `.deb`, `.rpm` | systemd user units included |
| macOS arm64 + x86_64 | signed, notarized `.app` in `.dmg`; `.pkg` for fleets | universal2 or two builds |

### 7.2 Shared release content

Single version source (`__init__.__version__`, injected into pyproject/installer at build), reproducible build
metadata (git SHA, ISO build time, platform triple) surfaced by `/identity` and `rag version`.

Every release ships: `SHA256SUMS` + detached signature · SBOM (CycloneDX) · third-party licence inventory ·
`CHANGELOG.md` · release notes · **configuration reference** · **upgrade guide** · **rollback/recovery guide** ·
**uninstall guide** · tray assets per platform.

### 7.3 Managed Qdrant distribution — **decision required**

Qdrant is Apache-2.0, so redistribution is permitted. Two viable models:

* **(A) Bundle** the pinned binary per platform — offline install works; installer grows ~30–60 MB per target.
* **(B) Download on first use** with pinned version + **SHA256 verification** — small installer, needs network.

Recommendation: **B by default, A as an `-offline` variant.** Either way the version is pinned
(`PINNED_QDRANT_VERSION = 1.15.5`) and verified before execution. *(Decision D-2, §14.)*

Storage modes are explicit in the installer and in config: `embedded` (no server, honest scale warning),
`managed` (product-supervised), `external` (URL + API key, product supervises nothing).

---

## 8. Implementation phases

Each phase: **entry → actions → tests → rollback boundary → exit gate.** No dates by request.

### Phase 0 — Make the work releasable *(blocking prerequisite)*
* **Entry:** none.
* **Actions:** land the 155-file working tree as a reviewed commit series on `rag-v3-dev`; close S1 (A3, A6/B26)
  and the **G1 gate**; tag `v3.0.0-dev.0`.
* **Tests:** full suite green from a clean checkout — *not* from the working tree.
* **Exit gate:** `git status` clean; CI green on a fresh clone; no test depends on `PYTHONPATH` pointing at a worktree.

### Phase 1 — Platform adapter extraction
* **Entry:** Phase 0.
* **Actions:** create `ragtools.platform.*` with the §3.2 contracts; move Windows logic behind `windows.py`;
  **delete `watchdog.py`**; add `resolve_adapter()` refusing unknown platforms.
* **Tests:** adapter contract suite runs against a fake adapter on every platform; static check that
  `sys.platform` appears **only** under `ragtools/platform/`.
* **Exit gate:** platform-dispatch count outside the package = **0** (today: 13 modules).

### Phase 2 — Linux + macOS adapters
* **Entry:** Phase 1.
* **Actions:** systemd-user and launchd implementations; XDG autostart; `launchctl`/`systemctl` service control;
  inotify-limit probe; FSEvents tuning; `singleton` via flock.
* **Tests:** integration tests on real Linux and macOS hosts/CI (**see D-1**).
* **Exit gate:** service installs, autostarts, restarts on failure, and uninstalls cleanly on both.

### Phase 3 — Upgrade engine
* **Entry:** Phases 1–2.
* **Actions:** `rag upgrade scan|plan|apply|status|resume|commit|rollback`; detection L1–L5 with dev-path
  exclusion; stop order; artifact cleanup incl. **PATH de-duplication**; config v2→v3 migration; data-dir rename.
* **Tests:** synthetic fixtures for every legacy layout, including the 16-duplicate-PATH and dual-casing cases;
  idempotency (apply twice = same state); interrupted-at-every-step resume.
* **Rollback boundary:** **A** — full rollback must be proven here.
* **Exit gate:** upgrade from a real v2.7.0 install leaves zero legacy artifacts, asserted by an automated sweep.

### Phase 4 — Migration + full re-index
* **Entry:** Phase 3.
* **Actions:** pre-flight gates; per-project durable jobs; framework sync; the five reconciliation gates;
  partial-failure reporting; resume after kill.
* **Rollback boundary:** **B** — forward-only past first write; old data dir retained.
* **Exit gate:** on the real 44k-file corpus: counts reconcile exactly; zero framework files in project
  collections; zero cross-project leakage; quality gate passes.

### Phase 5 — Lifecycle: tray, watcher, schedules
* **Entry:** Phase 2.
* **Actions:** tray state model incl. degraded; all actions via `service_control`; single-watcher assertion;
  in-service scheduler; remove keepalive task.
* **Tests:** sleep/resume, storage kill, service kill, watcher kill; duplicate-autostart detection.
* **Exit gate:** exactly one service registration, one tray registration, one watcher — proven by `verify()`.

### Phase 6 — Packaging
* **Entry:** Phases 1–5.
* **Actions:** three build pipelines; signing/notarization; SBOM, checksums, licences; docs set; offline variant.
* **Exit gate:** artifacts install on clean machines for all three platforms.

### Phase 7 — Release-candidate validation
* **Entry:** Phase 6.
* **Actions:** the §9 matrix on clean and upgraded machines.
* **Exit gate:** §11 acceptance gates all green.

---

## 9. Test and platform validation matrix

**Source-level tests are explicitly insufficient.** Every row below runs against the **built, installed artifact**.

| # | Scenario | Win | Linux | macOS | Method |
|---|---|---|---|---|---|
| 1 | Clean install | ✓ | ✓ | ✓ | fresh VM/container |
| 2 | **Upgrade from v2.7.0** | ✓ | n/a | n/a | real installed build |
| 3 | Reboot + sign-in → service autostarts | ✓ | ✓ | ✓ | reboot |
| 4 | Tray autostart + state accuracy (incl. degraded) | ✓ | ✓ | ✓ | manual + MCP |
| 5 | Headless (no desktop session) | — | ✓ | ✓ | container / SSH |
| 6 | Watcher: create/modify/delete/rename/dir-move | ✓ | ✓ | ✓ | automated |
| 7 | Watcher reconnect: restart, sleep/resume, storage restart | ✓ | ✓ | ✓ | automated + manual |
| 8 | Exactly one watcher / autostart / service | ✓ | ✓ | ✓ | `verify()` assertions |
| 9 | Scheduled actions fire, lock, and never duplicate | ✓ | ✓ | ✓ | log + job assertions |
| 10 | Managed Qdrant supervision, kill → recovery | ✓ | ✓ | ✓ | kill -9 |
| 11 | External Qdrant mode | ✓ | ✓ | ✓ | remote instance |
| 12 | Embedded mode + honest scale warning | ✓ | ✓ | ✓ | automated |
| 13 | Full + incremental index | ✓ | ✓ | ✓ | automated |
| 14 | Complete re-index migration, 44k files | ✓ | — | — | real corpus |
| 15 | Framework detect / exclude / link / dedup | ✓ | ✓ | ✓ | two trees, one build id |
| 16 | Search quality, latency, `scope`/`scope_source` | ✓ | ✓ | ✓ | eval harness + baseline |
| 17 | State⇄Qdrant reconciliation | ✓ | ✓ | ✓ | automated |
| 18 | **Zero cross-project leakage** | ✓ | ✓ | ✓ | negative probes |
| 19 | Interrupted upgrade → resume/repair | ✓ | ✓ | ✓ | kill at each step |
| 20 | Insufficient disk / port conflict / Qdrant start failure | ✓ | ✓ | ✓ | fault injection |
| 21 | Corrupt state or identity mismatch | ✓ | ✓ | ✓ | fault injection |
| 22 | Clean uninstall → **zero residue** | ✓ | ✓ | ✓ | filesystem+registry+task sweep |
| 23 | Uninstall → reinstall | ✓ | ✓ | ✓ | automated |
| 24 | Rollback before B / repair after B | ✓ | ✓ | ✓ | scripted |
| 25 | **Playwright** dashboard, projects, dependencies, settings, diagnostics, search, map+chunk inspector, health | ✓ | ✓ | ✓ | committed spec suite |
| 26 | Unit / integration / packaging / installer / upgrade / smoke | ✓ | ✓ | ✓ | CI |

---

## 10. Preventive controls for every known failure mode (§6 of the brief)

| Failure mode | Control | Status |
|---|---|---|
| Port exhaustion / connection churn | batched upserts + `MatchAny` batched deletes; TIME_WAIT asserted < 200 during a full index | **done**, needs a regression assertion |
| Concurrent full/incremental index | process-wide `_index_mutex`; job waits for the lock, fails loudly at a ceiling | **done** |
| Watcher-triggered duplicate re-index | single-watcher assertion + mutex + debounce | partial → Phase 5 |
| O(corpus) memory | streaming windowed indexer (2.46 GB → ~1.2 GB flat) | **done** |
| Health green while Qdrant unreachable | storage-aware `/health` with `storage_reachable` + `issues[]` | **done** |
| State⇄Qdrant divergence | index-identity guard + reconciliation gate | **done** + Phase 4 gate |
| Interrupted jobs silently lost | durable jobs; `busy` no-op now **fails loudly**; stale-job recovery | **done** |
| Stale vectors after shrunk/deleted files | `emptied` tracking + `_drop_stale_vectors` + delete-aware incremental | **done** |
| Framework duplication in project collections | scanner exclusion + purge + reconciliation gate 2 | **done** |
| Cross-project leakage | per-collection isolation, fail-closed scope, 403 on foreign collection | **done** + gate 4 |
| Multiple installs / stale PATH | detection L1–L3 + **PATH de-duplication** + single-entry invariant | **Phase 3** |
| Dev vs installed confusion | `RAG_PROFILE`, dev-path exclusion from installer, `/identity` with `profile` + actual bound port | **done** + Phase 3 |
| Duplicate autostart | one registration per concern; `verify()` returns the found set; upgrade removes legacy VBS + watchdog | **Phase 3/5** |
| Terminal flashing | no `wscript`/`cmd` triggers; console-less launcher | **Phase 1/3** |
| Upgrade preserving what should die | explicit preserve/migrate/rebuild table (§5.3) + post-upgrade residue sweep | **Phase 3** |
| Installer leaving mixed state | staged apply with resume; refuse to proceed if old runtime survives; Boundary A/B | **Phase 3** |

---

## 11. Release-blocking acceptance gates

1. Phase 0 exit: full suite green **from a clean clone**, working tree committed.
2. Zero `sys.platform` outside `ragtools/platform/`.
3. Install → reboot → autostart proven on **all three** platforms.
4. **Real upgrade** from the installed v2.7.0 with the 44k-file corpus: reconciled, quality gate passed.
5. Post-upgrade residue sweep: **no** legacy service, watcher, scheduled task, tray process, VBS, duplicate PATH
   entry, stale executable, or duplicated framework index.
6. Exactly one service + one tray + one watcher registration, proven by `verify()`.
7. Clean uninstall leaves zero residue; reinstall works.
8. Interrupted upgrade resumes to a reconciled state.
9. Signed/notarized macOS artifact; signed Windows artifact.
10. SBOM, checksums, licences, changelog, upgrade/rollback/uninstall docs published.
11. Playwright suite green on all three platforms.
12. No release if any project ends the migration unreconciled without a named recovery action.

---

## 12. Components expected to change

**New:** `ragtools/platform/{__init__,paths,autostart,session_autostart,service_control,scheduler,process,fswatch,notify,singleton,uninstall}.py` + `{windows,linux,darwin}.py` per concern · `ragtools/upgrade/{scan,plan,apply,resume,rollback,cleanup,config_migrate}.py` · `packaging/{linux/systemd,linux/deb,linux/rpm,macos/launchd,macos/pkg}` · `tests/platform/*`, `tests/upgrade/*`, `tests/e2e/playwright/*`.

**Modified:** `service/{startup,tray_startup,supervisor,process,run,notify}.py` (delegate to adapters) · `tray.py` (state model + service_control) · `cli.py` (upgrade verbs) · `config.py` (v3 defaults, XDG paths) · `service/owner.py` (reconciliation entry points) · `runtime_store.py`/`job_worker.py` (scheduler hooks) · `installer.iss` (PATH, no VBS) · `rag.spec` (console-less, per-platform) · `scripts/build.py` (3 pipelines) · `pyproject.toml` (version, extras) · `CHANGELOG.md`, `README.md`, `doc.md`, `CLAUDE.md`.

**Deleted:** `service/watchdog.py` (462 lines) · `scripts/launch.vbs` · all VBS launcher generation.

---

## 13. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **All v3 work uncommitted on one machine** | catastrophic loss | **Phase 0 first.** Nothing else starts until committed. |
| No Linux/macOS hardware or CI | gates 3, 11 unmeetable | **D-1**: provision CI runners or hosts. Blocks the *gate*, not the *work*. |
| macOS signing identity absent | unusable artifact | **D-3**: Apple Developer ID + Windows Authenticode cert |
| 44k-file re-index duration/failure | long, visible upgrade | durable per-project jobs, progress, resume; old data retained past Boundary B |
| Windows ARM64 has no managed Qdrant | silent degradation | installer defaults to `embedded` **and states it** |
| Qdrant version skew (client 1.17 vs server 1.15.5) | warnings today | pin both; assert compatibility at boot |
| `enable-linger` not set on headless Linux | service dies at logout | installer detects and offers; health surfaces it |
| inotify watch limits on large corpora | watcher silently partial | probe at startup; degrade to polling **visibly** |
| Users on the old `markdown_kb` expecting continuity | perceived data loss | old data dir retained; upgrade guide states rebuild explicitly |
| Two live services (21420/21422) on this machine | upgrade ambiguity | per-profile detection; upgrade one profile at a time |

---

## 13a. Execution status — every phase built; validation is where it stops

| Phase | State |
|---|---|
| **0** Close S1 and land the work | **done** — committed as `7f0f4d3` on `rag-v3-dev` (193 files). Not pushed; `master` untouched |
| **1** Platform seam | **done** — dispatch outside it 13 modules → 0, grep-enforced; `watchdog.py` (462 lines) deleted |
| **2** Linux + macOS adapters | **Linux VALIDATED on real systemd (22/22); macOS code + schema-validated, never executed** |
| **3** Upgrade engine | **done** — rehearsed 16/16 against a copy of the real v2.7.0 install |
| **4** Migration gates | **done** — preflight (5), reconcile (5), resumable boundary-aware state |
| **5** Lifecycle | **done** — service-owned schedule; no keepalive anywhere |
| **6** Packaging | **artifacts + metadata done; signing gate implemented and failing closed** (D-3) |
| **7** RC validation | **8 rows automated and passing; 7 remain manual** |

### Executed on this machine

```
Clean install of the BUILT WHEEL (V01)     wheel -> fresh venv -> service -> index -> search
                                           HIGH 0.808, scope field present; torn down clean
Linux adapter (WSL2, real systemd)         22/22   incl. systemd-analyze verify
Upgrade rehearsal (real v2.7.0 config)     16/16   detection, protection, PATH, migration
Cross-project isolation (V14)              0 foreign documents across 4 probes
Storage kill -> degraded -> recovery (V12) honest degraded; new Qdrant; 160,630 points intact
Validation matrix                          9 pass / 0 fail / 6 manual
```

**The gate refuses to pass a row it did not run.** Twice this had to be fixed:
once when `Matrix.validated` reported VALIDATED with nine unrun MANUAL rows, and
again when two checks returned PASS while their own detail said "not run". Both
are the same defect — converting *unverified* into *verified* — and both are now
pinned by tests.

### Defects found by executing rather than reading

| Defect | Found by | Consequence had it shipped |
|---|---|---|
| `StartLimitIntervalSec` in `[Service]` | **real systemd** | crash-loop protection silently discarded; a failing service restarts forever |
| A zombie process reported as alive | **real Linux** | a dead service reads as running; stale PID files never cleaned |
| `_process_alive` ignored the Windows exit code | Windows suite | same class, other OS |
| `_terminate_pid` force-killed on Windows, SIGTERM on POSIX | review + tests | the force path could not kill a hung process |
| A non-startup maintenance task could never become due | suite | daily and weekly maintenance would never have run once |
| Checksum manifest sorted by hash | suite | "reproducible" manifests reordered every build |
| `Matrix.validated` ignored MANUAL rows | suite | **the release gate reported VALIDATED with 9 required rows unrun** |
| A test reached the live Windows scheduler | runtime | would have altered a developer's real login items |

### What is still genuinely blocked

| Blocked | Why | Decision |
|---|---|---|
| macOS execution | no Mac, and macOS cannot be virtualised here. The launchd plist is schema-validated against `launchd.plist(5)` so the *silently-ignored-key* class cannot ship, but nothing has ever run | **D-1** |
| Signing / notarization | needs Apple Developer ID and a Windows certificate. The gate is implemented and **fails closed** — with no verifier configured every requirement reports UNSATISFIED | **D-3** |
| V01 clean install · V03 reboot · V04 tray registration · V13 production re-index · V15 destructive uninstall | each needs a machine that can be broken, or would modify this user's working install | **D-1** |

---

## 14. Verdict — `REQUIRES_DECISION`

The plan is implementable and Phases 0–2 can start immediately. Six decisions are load-bearing:

| # | Decision | Why it blocks | Recommendation |
|---|---|---|---|
| **D-1** | Linux/macOS CI runners or physical hosts | Gates 3 and 11 cannot be met without them; the brief requires validation on built artifacts on all three | GitHub Actions `ubuntu-latest` + `macos-14` (arm64), plus one physical macOS for notarization checks |
| **D-2** | Qdrant bundled vs downloaded | Determines installer size, offline support, pipeline | Download + SHA256 verify by default; ship an `-offline` bundled variant |
| **D-3** | Code-signing identities (Apple Developer ID, Windows cert) | Costs money, needs the owner's accounts; unsigned macOS builds are effectively unusable | Acquire both before Phase 6 |
| **D-4** | Windows ARM64: ship or defer | No managed Qdrant asset exists for it | Defer ARM64 to v3.1; x64 only in v3.0 |
| **D-5** | Retention of the pre-v3 data dir | Disk cost (1.2 GB) vs recoverability | Retain until explicit `rag upgrade commit`, default prompt after 14 days |
| **D-6** | Scope of v3.0 versus v3.1 | §18 hybrid retrieval, per-profile MCP process, onboarding wizard are **C/M** in §2 | Ship v3.0 without them; they are additive and not upgrade-coupled |

**Not blocked:** the architecture, the upgrade engine, the migration strategy and the failure-mode controls are
fully specified and can be built now. **Not ready:** the release gate cannot be satisfied on this machine alone,
and D-2/D-3/D-4 change what gets built in Phase 6.
