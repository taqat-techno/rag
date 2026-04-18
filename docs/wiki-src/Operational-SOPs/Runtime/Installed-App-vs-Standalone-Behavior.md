# SOP: Installed App vs Standalone Behavior

| | |
|---|---|
| **Owner** | TBD (proposed: ops lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Reconcile behavioral differences between the packaged installer build and the dev/source install. Use this page when a user says "it works on my dev box but not on my installed box" (or vice versa), or when copying config/data between hosts.

## Scope
Covers runtime differences between the two install modes. Does not cover how to install (see [Fresh Install (Dev)](Operational-SOPs-Installation-Fresh-Install-Dev) / [Fresh Install (Packaged)](Operational-SOPs-Installation-Fresh-Install-Packaged)) or the code-level detection flow (see [Install Decision Tree](Architecture-Install-Decision-Tree)).

## Trigger
- Config, ports, or paths differ between two Reg hosts.
- A user on installed mode reports behavior different from dev docs (or vice versa).
- Preparing to migrate config or data between machines.

## Preconditions
- [ ] Know which install mode is active on each host.
- [ ] Ability to run `rag doctor` on each.

## Inputs
- `rag doctor` output from both hosts, for comparison.

## Detection

```
rag doctor
```
prints the detected mode, resolved data dir, config path, and port. Alternatively:
```
python -c "from ragtools.config import is_packaged, get_data_dir; print('installed' if is_packaged() else 'dev', get_data_dir())"
```

## Steps — comparison table

| Aspect | Installed (packaged) | Standalone (dev) |
|---|---|---|
| Detection | `sys.frozen` is `True` | `sys.frozen` not set |
| Binary location | Windows: `C:\Program Files\RAGTools\`  · macOS: app bundle | source checkout |
| Data dir default | Windows: `%LOCALAPPDATA%\RAGTools\data\`  · macOS: `~/Library/Application Support/RAGTools/data/` | `./data/` (CWD) |
| Config file default | Windows: `%LOCALAPPDATA%\RAGTools\config.toml`  · macOS: `~/Library/Application Support/RAGTools/config.toml` | `./ragtools.toml` |
| Service port default | `21420` | `21421` |
| Logging | Rotating file `{data_dir}/logs/service.log`, 10 MB × 3 | stderr |
| PATH registration | Yes (installer adds `rag.exe`) | Venv-scoped |
| Startup task support | Yes (`rag service install`) | Not registered |

Any of these can be overridden per-process via `RAG_*` env vars or TOML — see [Environment Variables](Reference-Environment-Variables) and [Configuration Keys](Reference-Configuration-Keys).

## Validation / expected result
- `rag doctor` reports the expected mode on each host.
- Cross-host config copy works after adjusting paths (or, simpler, using env overrides to match the target mode).

## Failure modes
| Symptom | Likely cause | Fix |
|---|---|---|
| Dev box reads installed config unexpectedly | `RAG_CONFIG_PATH` or `RAG_DATA_DIR` set globally | Unset or re-point. |
| Installed service cannot find CWD config | Installed mode does not read `./ragtools.toml` | Place config at `%LOCALAPPDATA%\RAGTools\config.toml` or set `RAG_CONFIG_PATH`. |
| Config copied from dev to installed fails | Paths reference the dev tree | Use env overrides, or rewrite paths to the installed data dir. |

## Recovery / rollback
N/A — this SOP is diagnostic.

## Related code paths
- `src/ragtools/config.py:is_packaged` (line 50) — mode detection.
- `src/ragtools/config.py:get_data_dir` (line 66) — data dir resolution.
- `src/ragtools/config.py:_default_service_port` (line 205) — port defaults.

## Related commands
- `rag doctor`.

## Change log
- 2026-04-18 — Initial draft.
