# v3 Activation — Pre-Release Investigation

Branch `fix/windows-upgrade-chain` @ `61fe02f` · version `3.0.2` (unreleased) · 2026-07-28
Evidence: repository state, live probes on this machine, CI run `30307815530`, forensic report from `LAKOSHA-TAQAT`.
No code was changed, no release tagged, no machine modified during this investigation.

---

## 1. Executive summary

The v3 architecture is **built but not activated**. Nearly every component the next release
needs already exists, is tested in isolation, and is correct: the config migration, the upgrade
state machine with an explicit rollback boundary, the disk/port preflight, the collection router
with its cross-project read refusal, the index-identity guard, and the managed-Qdrant planner
with its fallback. What is missing is a **driver**: no production code path invokes any of it.

The result is that a v3.0.1 installation behaves as a v2 product. The storage alert seen on
`LAKOSHA-TAQAT` (83,922 points, one collection, `scale: over`) is not an installation failure —
it is the correct behaviour of a v2 configuration that nothing ever migrated.

Two additional problems make "just call `migrate_config()`" insufficient:

* **The config writers actively undo it.** `_save_projects_to_toml` hard-writes `version = 2`
  at 16 production call sites. Migration would be reverted by the first project edit and re-run
  on every subsequent boot.
* **The scale alert becomes wrong after migration.** It is computed from the *sum* across all
  collections, so a per-project layout would report `over` permanently while every individual
  collection sat far below the limit — converting a true warning into a guaranteed false alarm.

Separately, CI on the current branch is **red on two jobs**, and one of them is a genuine
regression in the unreleased 3.0.2 installer work: the new installer **hangs for 40 minutes**
when upgrading over a *running* v2.7.0 — precisely the scenario that work was written to fix.

**Nothing here is releasable yet.**

---

## 2. Confirmed current architecture

| Concern | Where it lives | State |
|---|---|---|
| Config schema + load | `config.py` (`Settings`, `TomlConfigSource`) | Working; `CONFIG_VERSION` lives elsewhere |
| v1→v2 project migration | `config.py:670 migrate_v1_to_v2` | **Never called** |
| `index_source_code`→`mode` | `config.py:149` Pydantic validator | **Wired** (load-time adopt-and-consume) |
| v2→v3 config migration | `upgrade/migrate.py:59 migrate_config` | **Never called** |
| Upgrade state machine | `upgrade/state.py` (10 steps, `BOUNDARY_STEP`) | Exists, unused |
| Upgrade preflight | `upgrade/preflight.py` (disk, ports, measured `BYTES_PER_POINT=8500`) | Exists, unused |
| Upgrade reconcile | `upgrade/reconcile.py` | Exists, unused |
| Collection routing | `collection_router.py` | **Wired**, correct |
| Index identity guard | `index_identity.py` → `owner.py:1345` | **Wired**, correct |
| Managed Qdrant planner | `service/managed_qdrant.py` | **Wired**, correct fallback |
| Platform adapters | `platform/{windows,linux,darwin}.py` | **Full parity** on all 4 new methods |
| Scale warning | `owner.py:64 compute_scale_warning` | Capability-aware; 2 callers drop it |

The `upgrade/` package is a well-designed, complete migration subsystem with no entry point.
`rag upgrade` does not exist. CLI commands are: `index, search, status, doctor, rebuild,
projects, watch, version, selfcheck, serve` plus groups `ignore, service, project, backup,
tray, wiki, client`.

---

## 3. Confirmed defects

### D-1 · `migrate_config()` has zero production callers — **BLOCKER**

Every reference is a test or a script:

```
src/ragtools/upgrade/migrate.py:59      definition
src/ragtools/upgrade/__init__.py:25,41  re-export
tests/test_upgrade_scan.py              7 call sites   (not shipped)
scripts/rehearse_upgrade.py:30,155,165                 (not shipped)
scripts/verify_uninstall_residue.py:103                (not shipped)
```

Consequence: config stays `version = 2`, and the runtime falls back to `config.py:407
collection_strategy = "shared"` and `config.py:410 storage_backend = "embedded"`. A clean v3
install lands in the same place — this is not only an upgrade defect.

`config.py:442 config_version: int = 1` is parsed but never compared against
`upgrade/migrate.py:30 CONFIG_VERSION = 3`, so nothing warns either.

### D-2 · The config writers stamp `version = 2` back onto disk — **BLOCKER**

`service/pages.py:1481` — `existing["version"] = 2`, unconditional, written atomically to the
real config file. Reached from **16 production call sites**:

```
cli.py:1046,1076,1226,1265,1302
service/pages.py:1192
service/routes.py:810,869,908,928,959,1154,2186,2222
```

i.e. every project add / remove / edit / mode change / ignore-rule change / dependency change,
from CLI, admin UI and MCP alike.

`service/pages.py:1434` — `existing.setdefault("version", 1)` stamps **version 1** onto any
config file that has no version key.

`service/owner.py:1704` — `update_projects()` sets in-memory `config_version = 2`.

**Measured behaviour** (probe, this machine, `python 3.12.10`):

```
start        : 2   strategy: None
post-migrate : 3   strategy: per_project   changed: True  added: [storage_backend, collection_strategy]
post-UI-edit : 2   strategy: per_project        <-- pages.py:1481
next boot    : changed: True  from: 2  added: []  -> rewrites the file every boot
```

The v3 *keys* survive; the *version* oscillates. Any naive wiring therefore produces a config
rewritten on every single service start, and makes "has this been migrated?" unanswerable from
the version field.

### D-3 · Scanner: one bad path aborts indexing for every project — **BLOCKER**

`indexing/scanner.py:97`:

```python
for path in directory.rglob("*"):
    if not path.is_file():
        continue
```

No exception handling. `scanner.py:237` calls this inside the per-project loop of
`scan_configured_projects`, also unguarded. `service/run.py:114-115` then wraps the entire
startup sync in `except Exception as e: logger.warning("Startup sync failed (non-fatal): %s", e)`.

Blast radius: **one untraversable path → all projects lose their startup sync**, logged as
"non-fatal". Field-observed on `LAKOSHA-TAQAT` at 16:58:29 as `WinError 448 — the provided
mount point is not trusted`.

**Measured refinement** — the fix must target the right call:

* `rglob` iteration is *resilient*. A directory with all access denied was silently skipped and
  the walk continued past it (`a_first.md`, `m_denied`, `z_last.md` all yielded; no exception).
* Therefore the raise comes from `path.is_file()` — the stat that resolves the reparse point —
  not from the generator. Wrapping the `is_file()` call is sufficient; the tree is not lost.

### D-4 · Junction loops cause duplicate traversal — **HIGH**

Measured on this machine with a self-referential junction:

```
entries walked: 46    max depth reached: 23
discover_indexable_files() -> 23 files      (23 copies of ONE file)
```

`rglob` follows junctions. The bound is the path-length limit, not loop detection:
`LongPathsEnabled = 0` here, so it stopped at depth 23. On a machine with long paths enabled
the bound is far higher. Nothing deduplicates by resolved path — `discover_indexable_files`
returns raw paths.

### D-5 · The installer kills Qdrant processes it does not own — **BLOCKER**

`installer.iss:209,215` — `taskkill /F /IM qdrant.exe /T` matches **by image name across the
whole machine**. This kills:

* a user's `external` backend — which the architecture explicitly states is "a server you run
  yourself" and must not be owned;
* an unrelated Qdrant belonging to another product;
* the managed instance of a *different* ragtools install.

The product already knows how to do this correctly: `selfcheck.py:141` deliberately *excludes*
qdrant from its owned-process check because "storage lives under the data dir, not the program
dir", and `platform/windows.py:182 owned_processes()` returns `(pid, image, path)` so callers
can scope by path. The installer is the one place that ignores this.

### D-6 · `real-upgrade` only tests the case that cannot fail — **BLOCKER**

`scripts/verify_upgrade_install.py:163` accepts `--from-version` (default `2.7.0`), but
`release-validation.yml:256` only ever passes `2.7.0`.

v2.7.0's scheduled tasks target `rag.exe`. The *old* single-image kill already handled that, so
this path passes with or without the 3.0.2 fix. The discriminating case is **v3.0.1 → new**,
where both tasks target `ragw.exe` — the exact state the forensic report documents on
`LAKOSHA-TAQAT` ("`ragw.exe` (PID 3076) and the task-registered images are now the running
set... This is exactly the scenario 3.0.2 fixes"). That case is untested.

`RAGTools-Setup-3.0.1.exe` is published (604,747,070 bytes), so the input exists.

### D-7 · CI is red on the current branch — **BLOCKER**

Run `30307815530`, branch `fix/windows-upgrade-chain`:

```
success  built bundle contract (windows)        <- new job, passes
success  clean install (windows / ubuntu / macos-14)
success  Linux adapter / macOS adapter / autostart lifecycle
success  suite (ubuntu-latest) / suite (macos-14)
success  upgrade rehearsal (synthesised layout)
failure  suite (windows-latest)
failure  packaged v2.7.0 -> this build, on a real machine
```

**D-7a — the 3.0.2 installer hangs over a running v2.7.0.** Job `90116186593`:

```
21:51:27  >>> installing v2.7.0                        (completes in ~86 s)
21:52:54  [PASS] previous release installed (2.7.0)
21:53:02  [PASS] previous release is serving
21:53:07  [PASS] previous release has live processes holding its binaries — 2 process(es)
21:53:07  >>> installing v3.0.2 (over v2.7.0)
22:33:07  subprocess.TimeoutExpired ... timed out after 2400 seconds
22:36:21  Terminate orphan process: pid (1776) (RAGTools-Setup-3.0.2.tmp)
```

The previous installer completes in 86 seconds; the new one hangs for 40 minutes with the old
processes alive — the precise scenario the 3.0.2 work targets. This is a regression in
unreleased work, not a pre-existing fault. Cause is **not** established; see §11 Phase 0.

**D-7b — a machine-dependent test.** `tests/test_selfcheck.py:154`:

```
E  AssertionError: the registry check silently skipped on Windows
   Check(name='recorded install version', ok=True,
         detail='this platform records no installed version', skipped=True)
1 failed, 1866 passed, 17 skipped in 890.02s
```

Confirmed by differential measurement:

* On CI (clean runner, no RAGTools installed) → **fails**.
* On this machine → `pytest tests/test_selfcheck.py` → **10 passed**, because
  `adapter().recorded_version()` returns `'2.7.0'` from the real uninstall key.

The test encodes "a product is installed" as if it were "the registry check works". The deeper
flaw is in the adapter contract: `recorded_version() -> Optional[str]` cannot distinguish *"this
OS has no package database"* (Linux/macOS) from *"this OS has one and nothing is registered"*
(clean Windows). `owned_processes()` already models this correctly — `None` = could not look,
`[]` = looked and found none. `recorded_version` needs the same three-state treatment.

### D-8 · Uninstall and residue are not release-gating — **BLOCKER**

`release-validation.yml:261,277` — both `verify_uninstall_residue` and `upgrade-rehearsal`
carry `continue-on-error: true`, so they cannot fail the build. The stated release rule is that
uninstall and residue validations must *pass*; a step that cannot fail does not pass, it reports.

Worse, the **real Inno uninstaller is never executed anywhere in CI**. `verify_uninstall_residue.py`
uninstalls a *wheel from a sandbox venv*; `rehearse_upgrade.py:68` writes a fake stub
(`unins000.exe` ← `b"MZ"`). The actual `unins000.exe` produced by `installer.iss` is untested.

### D-9 · `migrate_v1_to_v2` is also never called — **HIGH**

`config.py:670`, zero production callers. Same defect class as D-1, one schema version older —
evidence this is a systemic wiring gap rather than a one-off omission.

---

## 4. False assumptions ruled out

| Assumption | Verdict | Evidence |
|---|---|---|
| "The v3.0.1 install failed / was blocked" | **False** | Forensic report: correct SHA-256, `_internal` 0 files older than install date, 33 `.dist-info`, 0 duplicates, registry `3.0.1`, `/health` `3.0.1`. |
| "Managed Qdrant is not implemented" | **False** | `service/managed_qdrant.py` complete; pinned version; a managed instance is running on this machine now (`qdrant.exe` PID 36792, 3 collections). |
| "Per-project collections are unimplemented" | **False** | `collection_router.py` complete and wired; refuses cross-project reads. Unreachable only because nothing sets the strategy. |
| "The storage alert is an install defect" | **False** | It is the correct output for an unmigrated v2 config. |
| "A layout flip would silently skip re-indexing" | **False** | `index_identity.py` is wired at `owner.py:1345` and forces a re-index on strategy/backend change. |
| "`rglob` dies on a bad path, losing the tree" | **False** | Measured: unreadable directories are skipped, iteration continues. Only `is_file()` raises. |
| "The 3.0.2 installer work is validated" | **False** | Its own CI job fails (D-7a). |

---

## 5. Production entry points that load config

Twelve `Settings()` construction sites, none of which migrate:

```
cli.py:56                     every CLI command (and `tray`, via _settings())
indexing/indexer.py:312       direct-API full index
indexing/indexer.py:406       direct-API incremental index
integration/mcp_common.py:134 MCP state
integration/mcp_server.py:139 MCP module import
integration/mcp_server.py:1271 MCP state construction
integration/mcp_server.py:1778 MCP ops state
retrieval/searcher.py:75      searcher fallback
selfcheck.py:205              port lookup
service/app.py:133            service (FastAPI lifespan)
service/run.py:146            service main()
service/supervisor.py:268     supervisor
```

**Ordering constraint.** `QdrantOwner.__init__` opens the store at `owner.py:195`
(`settings.get_qdrant_client()`) and *creates collections* at `owner.py:233-234`
(`ensure_collection`). Migration must therefore complete strictly **before** owner construction,
or the old shared store is created before the new layout is known.

---

## 6. Current vs intended behaviour

| # | Current | Intended |
|---|---|---|
| 1 | v2 config persists forever | Boot migrates to v3 once, idempotently |
| 2 | `version=2` rewritten by 16 writers | One writer, stamping `CONFIG_VERSION` |
| 3 | `shared` + `embedded` always | Declared strategy honoured |
| 4 | No way to set backend/strategy | Supported setter on at least one interface |
| 5 | Scale alert = sum of all collections | Per-collection, engine-aware |
| 6 | Alert recommends an unreachable action | Recommends something the user can do |
| 7 | One bad path stops all indexing | Isolated to that path; counted; reported |
| 8 | Installer kills every `qdrant.exe` | Kills only processes under `{app}` / own data dir |
| 9 | Upgrade tested from 2.7.0 only | 2.7.0 **and** 3.0.1 |
| 10 | Real uninstaller untested | Exercised and swept |

---

## 7. Cross-platform impact

Adapter parity is **complete** — `windowed_executable_name`, `recorded_version`,
`owned_processes`, `background_executable` are implemented in `windows.py`, `linux.py` and
`darwin.py`, and `tests/test_platform_adapters.py` contains an AST-based guard
(`test_no_platform_branch_survives_outside_this_package`) that fails if platform knowledge leaks
out of the package. That guard already forced `selfcheck.py` to be rewritten behind the adapter
and should not be weakened.

Gaps that are Windows-only by omission, not by design:

* **D-1..D-4 and D-9 are platform-neutral** — the migration and scanner defects affect all three
  platforms equally.
* **`release.yml` builds all three** (`build-windows`, `build-macos` on `macos-14`,
  `build-linux` on `ubuntu-22.04`); v3.0.1 shipped `.exe`, `macOS-arm64.zip`,
  `linux-x86_64.tar.gz` and a portable zip.
* **No real packaged upgrade test exists for Linux or macOS.** `clean-install` covers the wheel
  on all three; `real-upgrade` is Windows-only. systemd/launchd binary replacement over a running
  unit/agent is unvalidated.
* **D-3's fix must not be Windows-shaped.** `EACCES`, dangling symlinks, and races are the
  POSIX equivalents of `WinError 448`; the guard must catch `OSError` generally, not a winerror.
* **`qdrant.exe` is not packaged on any platform** — absent from `rag.spec`, `installer.iss`
  `[Files]`, `scripts/build.py` and `release.yml`. Managed mode is unreachable out of the box
  everywhere. `find_qdrant_binary` (`managed_qdrant.py:91`) falls back to embedded with the
  reason surfaced, which is correct behaviour — but it means `managed` cannot be a default.

---

## 8. Data-loss and rollback risks

| Risk | Severity | Current mitigation |
|---|---|---|
| Layout flip skips every file against an empty store | **Was critical** | **Solved** — `index_identity.py`, wired at `owner.py:1345`. Observed once (38,213 files "skipped" against a store holding a fraction) and guarded since. |
| Migration writes a torn config | Medium | `atomicio.atomic_write_bytes(..., backup=True)` — temp + fsync + replace. Sound. |
| Two processes migrate concurrently | **High** | **None.** No file-lock primitive exists in the tree. 12 `Settings()` sites and service + MCP + CLI can run at once. |
| Re-index runs out of disk mid-flight | Medium | `upgrade/preflight.py` computes it (`BYTES_PER_POINT=8500`, `DISK_SAFETY_MULTIPLE=3.0`, 2 GiB floor) — but nothing calls it. |
| Old index deleted before new one is ready | Medium | `upgrade/state.py` defines `BOUNDARY_STEP = STEP_INDEXING` and a retain-and-rename policy — but nothing drives it. |
| Installer kills an unrelated/external Qdrant | **High** | None (D-5). |
| User config lost on upgrade | Low | `BackupConfig` in `installer.iss` copies before `DelTree`; a failed backup cancels the deletion. |

The rollback *model* is already designed and documented in `upgrade/state.py`. The risk is not
that it is wrong — it is that no code executes it.

---

## 9. Recommended product decisions

**PD-1 — What should migrated users get?** `V3_DEFAULTS` (`migrate.py:35-41`) currently says
`embedded` + `per_project`, with a comment arguing embedded is "the honest default" because
managed "would download a binary they did not ask for". That reasoning holds and is reinforced
by §7: no Qdrant binary ships, so `managed` cannot be a default without a packaging change that
adds a second large binary to already-600 MB artifacts.

But `embedded` + `per_project` **forces a full re-index of every existing install** (83,922
points on `LAKOSHA-TAQAT`) and does not by itself fix the scale alert — it changes which number
is wrong (see D-2/§3 and Area 5). Three options:

| Option | Effect on existing users | Fixes the alert? |
|---|---|---|
| **A.** `embedded` + `per_project` (current default) | Full re-index, once, driven and resumable | Only if the alert is made per-collection |
| **B.** `embedded` + `shared` (change nothing but the version) | No re-index; v3 features stay off | No |
| **C.** `managed` + `per_project` | Full re-index **and** ship/download a Qdrant binary | Yes, genuinely (HNSW ⇒ no ceiling) |

**Recommendation: A, with the per-collection alert fix as a hard co-requirement** — it is the
smallest change that makes the alert truthful, needs no new binary, and the re-index is already
guarded by `index_identity` and (once wired) `preflight`. C is the right long-term answer for
users at this scale and should be a follow-up, not a blocker.

**PD-2 — Where does the strategy setter live?** Today: nowhere. `diagnostics.html:64,72`
displays `collection_strategy` read-only; `storage_backend` appears in no template and no CLI
command; `ConfigUpdateRequest` (`routes.py:1462-1470`) has neither field. Recommend a CLI
setter first (`rag storage backend|strategy`), because it is the interface the migration and
recovery paths can use non-interactively.

**PD-3 — Is signing a release gate?** Artifacts are `NotSigned` (confirmed on `LAKOSHA-TAQAT`).
Only the user can supply certificates. Recommend: documented limitation, not a gate — but it
must be stated explicitly rather than left implicit.

---

## 10. Proposed implementation architecture

**A single bootstrap seam, called once, before anything resolves storage.**

```
ragtools/bootstrap.py
    ensure_config_current(*, allow_write: bool) -> BootstrapResult
```

* **One canonical caller ordering** — `Settings()` → `ensure_config_current()` → `QdrantOwner()`.
  Never inside `TomlConfigSource`: migration writes, and `Settings()` is legitimately constructed
  in read-only contexts (`searcher.py:75`, `selfcheck.py:205`).
* **`allow_write` splits the twelve sites into two classes.** Writers: `service/run.py`,
  `service/app.py`, `cli.py`, `supervisor.py`. Readers (`searcher`, `selfcheck`, MCP, indexer
  direct-API): migrate **in memory only** and log if the on-disk file is stale. This prevents
  four processes racing to rewrite one file.
* **A real mutex.** No lock primitive exists today. Use an exclusive-create lock file under
  `data_dir` with a stale-timeout, or reuse the SQLite runtime store's transaction. A loser
  waits and re-reads rather than writing.
* **Idempotence by construction** — `migrate_config` is already pure and idempotent
  (`test_upgrade_scan.py:383-384` proves it). Atomicity is already provided by
  `atomic_write_bytes(..., backup=True)`.
* **Failure is loud, not fatal.** A migration that cannot write must not stop the service from
  starting; it must degrade to in-memory v3 semantics, surface on `/health` and `rag doctor`,
  and be retried next boot.

**The single-writer fix (D-2).** Delete the hard-coded `version` literals and stamp
`CONFIG_VERSION` from one place. This is a precondition for D-1, not an optional tidy-up —
wiring migration without it produces a config rewritten on every boot forever.

**Then, and only then, drive what already exists**: `rag upgrade` as the explicit driver over
`upgrade/{preflight,state,reconcile}.py`, with `--dry-run` reusing the same `MigrationResult`.

---

## 11. Ordered implementation phases

**Phase 0 — Make CI honest (blocks everything else).**
0.1 Diagnose D-7a. Add `/LOG=<path>` to the `verify_upgrade_install.py` install invocation and
dump the Inno log on timeout — this converts "hung somewhere" into "hung at line N" and is the
cheapest possible discriminator between the ranked hypotheses (Restart Manager scan under
`CloseApplications=yes`; the pre-existing `Exec(rag.exe, 'service stop'/'service uninstall',
ewWaitUntilTerminated)` against a v2.7.0 binary; `VerifyInstallation`'s `selfcheck` at `ssDone`).
Fix the cause. Do not raise the timeout to make it green.
0.2 Fix D-7b: give `recorded_version()` a three-state contract and make the test assert the
behaviour, not the developer's machine.
0.3 Remove `continue-on-error` from the uninstall/residue step (D-8) and add a job that runs the
**real** `unins000.exe`.
0.4 Add `--from-version 3.0.1` as a second `real-upgrade` matrix leg (D-6).

**Phase 1 — Single config writer (D-2).** One stamping site; delete the 3 literals.

**Phase 2 — Bootstrap seam (D-1, D-9).** `ensure_config_current`, the lock, the 12 call sites,
`/health` + `doctor` surfacing. Fold `migrate_v1_to_v2` into the same seam.

**Phase 3 — Scale-alert correctness.** Per-collection metric; pass `capabilities` at
`cli.py:348` and `owner.py:165`; rewrite the message so it only recommends reachable actions.

**Phase 4 — Scanner fault isolation (D-3, D-4).** Guard `is_file()` with `OSError`; per-project
try/except in `scan_configured_projects`; skip counters surfaced; loop detection by resolved
path; matching Linux/macOS tests.

**Phase 5 — Installer process ownership (D-5).** Path-scoped kill via the adapter.

**Phase 6 — Setters (PD-2) and the `rag upgrade` driver.**

**Phase 7 — Cross-platform upgrade parity.** systemd/launchd real-package upgrade jobs.

---

## 12. Test and validation matrix

Legend: **U** unit · **I** integration · **E** e2e · **P** installer/packaging · **G** release gate

| # | Property | Kind | Exists? |
|---|---|---|---|
| 1 | A v2 config booted through the **real app** becomes v3 | I | **No** |
| 2 | Clean install generates a valid v3 config | I | **No** |
| 3 | No production writer can restore version 2 | U | **No** |
| 4 | Migration precedes backend/router init | I | **No** |
| 5 | Concurrent startup cannot corrupt the config | I | **No** |
| 6 | Migration failure is recoverable and visible | I | **No** |
| 7 | Projects + framework links survive migration | U | Partial (`test_upgrade_scan.py`) |
| 8 | Required re-index triggers exactly once | I | Partial (`test_index_identity.py`) |
| 9 | Interrupted re-index resumes or rolls back | I | **No** (model exists, undriven) |
| 10 | Shared store replaced by the v3 layout | E | **No** |
| 11 | Scanner errors isolated to one path/project | U+I | **No** |
| 12 | Scale warning correct per backend × strategy | U | Partial (engine-aware; not per-collection) |
| 13 | Real packaged upgrade replaces running binaries | P | **Exists but FAILING** (D-7a) |
| 14 | `ragw.exe` autostart after a real logon | G | **Manual** — runners are admins |
| 15 | Dirty `_internal` removed | P | `bundle-contract` covers the build; dirty-tree case still unproven |
| 16 | User config/data survive upgrade and uninstall | P | Sandbox only (D-8) |
| 17 | All three platforms pass clean-install **and** upgrade | G | Clean-install yes; upgrade Windows-only |

Windows validation matrix — required legs: clean install · v2.7.0→new · **v3.0.0→new** ·
**v3.0.1→new** · install-over-same-version · repair · downgrade attempt · restart · first logon ·
uninstall · residue sweep. Currently executed: clean install, v2.7.0→new (failing).

---

## 13. Release gating criteria

A release may proceed only when **all** hold:

1. `release-validation` green with **no** `continue-on-error` on uninstall/residue.
2. `real-upgrade` passes from **both** 2.7.0 and 3.0.1.
3. Matrix rows 1–6, 11 implemented and passing.
4. A booted v2 config demonstrably becomes v3 and **stays** v3 after a project edit.
5. Scale alert verified for all four backend × strategy combinations.
6. Real `unins000.exe` executed and swept.
7. Non-admin autostart and real-logon rows executed and recorded (still manual).
8. Signing status explicitly stated.

---

## 14. Must block the next release

D-1, D-2, D-3, D-5, D-6, D-7a, D-7b, D-8. Plus the Phase-3 alert fix **if** PD-1 resolves to
option A, since migrating to `per_project` without it guarantees a permanent false alarm.

## 15. May be deferred

D-4 (junction loops — bounded today by `LongPathsEnabled=0`; revisit if that changes) ·
managed-Qdrant packaging and PD-1 option C · `rag upgrade` interactive UX · progress reporting
in tray/UI · cross-platform real-upgrade jobs (Phase 7) · code signing.

## 16. Verdict

**`REQUIRES_PRODUCT_DECISION`**

The engineering path is clear and mostly wiring rather than construction. But **PD-1** —
whether every existing installation is put through a full re-index on first boot of the next
release — is a genuine product decision with direct user impact, and it determines whether the
scale-alert rework is a release blocker or a follow-up. PD-2 and PD-3 need answers before the
implementation task is scoped.

Independently of that decision, **Phase 0 must happen first**: the current branch's own CI is
red, and one failure is a real hang in the unreleased installer work.
