# RAG Tools — Release Lifecycle Rules

**Status: Permanent policy. Every future release must comply.**

This document defines the install / upgrade / uninstall contract that every
shipped version of RAG Tools must honor. These rules exist so that users can
upgrade and remove the app without losing the content they have indexed, the
settings they have configured, or the logs they depend on for support.

---

## 1. The core rule

> **App files are replaceable. User data is persistent.
> Upgrades replace the app, not the user's data.
> Full data deletion is always explicit and opt-in.**

This rule is non-negotiable. No release, hotfix, or refactor is allowed to
break it.

---

## 2. Layered model

### 2.1 Replaceable app layer

These are safe to overwrite on every install and to wipe on every uninstall:

| Resource | Typical location | Why it's replaceable |
|---|---|---|
| `rag.exe` / `rag` binary | install dir | rebuilt per release by PyInstaller |
| `_internal/` bundle | install dir | rebuilt per release |
| `model_cache/` (if bundled with the exe) | install dir | fixed part of the ship |
| `launch.vbs` and other helpers | install dir | versioned with the app |
| HTML templates, CSS, JS | install dir (inside `_internal/ragtools/service/`) | versioned with the app |

### 2.2 Persistent user layer

These **must** survive upgrade and default uninstall:

| Resource | Typical location | Why it must persist |
|---|---|---|
| `config.toml` | persistent data dir | user settings, configured projects |
| `data/qdrant/` | persistent data dir | vector DB — expensive to rebuild |
| `data/index_state.db` | persistent data dir | file-hash tracking |
| `logs/service.log` | persistent data dir | operator diagnostics |
| `service.pid` | persistent data dir | runtime state |
| `~/.cache/huggingface/` (if model not bundled) | user-wide HF cache | shared across apps |

---

## 3. Directory contract

| Purpose | Windows | macOS |
|---|---|---|
| Install dir (replaceable) | `%LOCALAPPDATA%\Programs\RAGTools\` (user install) or `C:\Program Files\RAGTools\` (system install) | `/Applications/RAGTools.app/` |
| Persistent data dir | `%LOCALAPPDATA%\RAGTools\` | `~/Library/Application Support/RAGTools/` |
| Login auto-start | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\RAGTools.vbs` | `~/Library/LaunchAgents/com.taqatechno.ragtools.plist` (roadmap) |

**Resolution is enforced in code**, not by convention. The authoritative
functions are in `src/ragtools/config.py`:

- `is_packaged()` — single source of truth for "installed vs dev"
- `_get_app_dir()` — returns the persistent dir per platform
- `get_data_dir()` — returns the data root; never touches install dir
- `_find_config_path()` — reads from persistent dir only when packaged
- `get_config_write_path()` — writes to persistent dir only when packaged

**Rule: no code may write user data into the install directory under any
circumstance.**

---

## 4. Upgrade behavior

On upgrade the installer MUST:

1. Stop the running service (`rag.exe service stop`).
2. Remove the old startup task entry (`rag.exe service uninstall`).
3. Replace all files in the install directory.
4. Re-register the startup task against the new install dir.
5. Restart the service.
6. Leave the persistent data directory untouched.

On upgrade the service MUST, on first start:

1. Read `config_version` from `config.toml`. If missing, auto-migrate v1 to v2.
2. Read `PRAGMA user_version` from the SQLite state DB. If higher than the app
   supports, refuse to mount and surface a clear error (downgrade detected).
3. Read Qdrant collection metadata. If the embedding dimension differs from
   what this app expects, refuse to mount and prompt the user for explicit
   `rag rebuild`.
4. Never silently wipe, reset, or corrupt persistent user data.

---

## 5. Uninstall behavior

Default uninstall:

- Stop the service.
- Remove the startup task.
- Remove all install-dir files.
- **Do not touch** the persistent data dir.
- Tell the user where their data still lives in case they want to delete it
  manually later.

Full uninstall (opt-in):

- Prompt the user with an explicit question: "Do you ALSO want to DELETE your
  user data?"
- Default button is **NO (keep)**. Enter / Esc / closing the dialog must not
  delete data.
- Only on an affirmative YES may the persistent data dir be removed.

This is enforced in `installer.iss` → `CurUninstallStepChanged`.

---

## 6. Schema versioning

Every persistent store has a version marker. When a store's version is older
than the current app expects, the app migrates forward. When newer, the app
refuses to mount it rather than corrupt it.

| Store | Version carrier | Current |
|---|---|---|
| `config.toml` | top-level `version = N` | 2 |
| `index_state.db` | `PRAGMA user_version` | 1 |
| Qdrant collection | collection metadata / dim check at open time | embedding dim = 384 |

Migration code lives with the store:

- Config: `src/ragtools/config.py` → `migrate_v1_to_v2()`
- State DB: `src/ragtools/indexing/state.py` → `IndexState._migrate_schema()`
- Qdrant: `src/ragtools/indexing/indexer.py` → `ensure_collection()` (dim check)

**Rule: every schema-changing PR must bump the version and add a migration
step in the same commit.**

---

## 7. Startup-task safety

Only the installed/packaged app may register the Windows Startup folder
script. Dev-mode runs (non-frozen Python) must not touch the Startup folder.

Enforced in:

- `src/ragtools/service/run.py` → `_post_startup()` gates `install_task()`
  behind `is_packaged() and sys.platform == "win32"`.
- `src/ragtools/service/startup.py` → `_check_windows()` returns `False` on
  non-Windows and logs a skip message.

## 7a. Supervisor / auto-restart (v2.4.3+)

Since v2.4.3, `rag service start` defaults to launching a **supervisor**
process that spawns and babysits the real service. If the real service
exits with a non-zero code (crash, kill, OOM), the supervisor respawns
it with exponential backoff. If too many crashes happen in a rolling
window, the supervisor gives up and writes a marker file for post-mortem.

**Process hierarchy:**

```
rag service start
    │
    └── [Supervisor process]            writes supervisor.pid
            │
            └── [Real service / uvicorn] writes service.pid
```

**Policy** (see `src/ragtools/service/supervisor.py` → `SupervisorPolicy`):

- Max 5 crashes inside a 300-second rolling window
- Backoff: 2s → 4s → 8s → 16s → 32s (capped)
- Child exits with code 0 (graceful `/api/shutdown`) → supervisor exits 0
- Budget exhausted → `logs/supervisor_gave_up.json` is written and
  supervisor exits with code 1

**CLI surface:**

| Command | Behavior |
|---|---|
| `rag service start` | Default. Launches supervisor detached; supervisor spawns real service. |
| `rag service start --no-supervise` | Legacy. Launches real service directly, no auto-restart. |
| `rag service supervise` | Foreground supervisor (debug use). |
| `rag service run` | Unchanged. Foreground real service, no supervisor. |
| `rag service stop` | Stops both supervisor and service. Kills supervisor FIRST so it cannot respawn the child. |
| `rag service status` | Reports both `pid` (service) and `supervisor_pid`. |

**Rule: do not remove or weaken the supervisor without a strong reason.**
Field reports have shown multiple silent crashes that the supervisor
would have auto-recovered from. Changing the policy (fewer retries,
shorter window) is acceptable and doesn't require documenting here —
the defaults are specified in code, tests pin their values.

---

## 8. Platform gaps and roadmap

| Item | Windows | macOS | Status |
|---|:---:|:---:|---|
| Installer artifact | ✅ `.exe` (Inno Setup) | ❌ tarball only | macOS `.dmg` is roadmap |
| Upgrade hook | ✅ pre-install service stop | ❌ manual | — |
| Default uninstall behavior | ✅ keeps data | ❌ N/A | — |
| Opt-in full uninstall | ✅ MB_DEFBUTTON2 No | ❌ N/A | — |
| Login auto-start | ✅ Startup folder VBScript | ❌ no LaunchAgent | macOS LaunchAgent is roadmap |
| Code signing / notarization | ❌ none | ❌ none | roadmap |

The rules above apply to macOS as soon as a real installer lands. Until then,
macOS users install by extracting the tarball and removing it manually, and
the persistent data dir (`~/Library/Application Support/RAGTools/`) is
correctly kept out of that directory so tarball removal never touches it.

---

## 9. Release gate — per-version checklist

Before cutting any new release, the maintainer MUST verify:

- [ ] `pyproject.toml`, `src/ragtools/__init__.py`, `installer.iss` all show
      the same `X.Y.Z`
- [ ] No new code path writes user data into the install directory
- [ ] Any schema change bumps its version AND ships a migration
- [ ] `pytest` passes on Windows and macOS CI
- [ ] Installer was tested on a machine that already has the previous
      version installed (upgrade path verified)
- [ ] Installer was tested with the "opt-in delete" answered both YES and NO
- [ ] Dev-mode startup (`python -m ragtools.service.run` from source) does
      not touch `%LOCALAPPDATA%\RAGTools\` or register a Startup task
- [ ] `docs/RELEASE_LIFECYCLE.md` is still accurate for this version

If any of those is not true, the release is blocked.

---

## 10. Compliance failures — what counts as a regression

Any of the following must be treated as a P0 regression on `main`:

- A new file created under the install directory that needs to be edited at
  runtime.
- A new code path that reads or writes user data via a relative path.
- A schema change without a version bump and migration.
- An uninstall flow that deletes user data without an explicit confirmation
  where "keep" is the safe default.
- A new release that stops reading the data directory of the previous release
  on the same machine.

---

_Last updated for v2.4.2. Keep this document current — update it in the same
PR that changes behavior it describes._
