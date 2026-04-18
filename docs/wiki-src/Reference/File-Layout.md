# Reference: File Layout

| | |
|---|---|
| **Owner** | TBD (proposed: ops lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Source of truth** | `src/ragtools/config.py` — `get_data_dir`, `_get_app_dir`, `_find_config_path` |

Where every file Reg creates or reads lives. Behavior differs between dev and installed modes — see [Install Decision Tree](Architecture-Install-Decision-Tree).

## Dev mode (source install, `sys.frozen` is false)

All paths are relative to the current working directory (CWD) of the process — usually the repo root.

```
<cwd>/
├── ragtools.toml                 # Optional config file (not checked in)
├── .env                          # Optional env-var overrides (not checked in)
└── data/                         # Created on first index / service start
    ├── qdrant/                   # Qdrant local storage
    │   └── ...                   # Qdrant segments, WAL, meta
    ├── index_state.db            # SQLite state (per-file hash)
    └── service.pid               # PID file (while service is running)
```

- `logs/` — dev mode logs to stderr by default; no rotating file handler. Override with `RAG_LOG_LEVEL` if needed.

## Installed mode — Windows

Binaries are read-only; user data is per-user.

```
C:\Program Files\RAGTools\        # Binaries (installer-managed, read-only)
├── rag.exe
├── rag-mcp.exe
├── _internal\                    # PyInstaller bundle
│   ├── python312.dll
│   ├── ragtools\
│   └── (model weights if bundled)
└── app.ico

%LOCALAPPDATA%\RAGTools\          # User data (read/write)
├── config.toml                   # User-writable config
├── data\                         # Data root (same shape as dev mode)
│   ├── qdrant\
│   ├── index_state.db
│   └── logs\
│       ├── service.log           # RotatingFileHandler, 10 MB x 3
│       ├── service.log.1
│       └── service.log.2
└── service.pid                   # PID file (while running)
```

- PATH receives `C:\Program Files\RAGTools\`.
- Task Scheduler entry (if registered via `rag service install`): `\RAGTools\Service` task referencing `rag.exe service run`.

## Installed mode — macOS (arm64)

Binaries live in the app bundle or a bundled directory layout. User data:

```
~/Library/Application Support/RAGTools/
├── config.toml
├── data/
│   ├── qdrant/
│   ├── index_state.db
│   └── logs/
│       └── service.log
└── service.pid
```

## Per-project files (checked into user repos)

```
<user-project>/
├── .ragignore                    # Optional per-directory ignore rules (gitignore syntax)
├── ...                           # Subdirectories can carry their own .ragignore
└── *.md                          # Indexed content
```

## What to never delete while the service is running

- `data/qdrant/` — Qdrant holds an exclusive file lock here. Deleting live files will corrupt state. See [Single-Process Invariant](Core-Concepts-Single-Process-Invariant).
- `data/index_state.db` — live SQLite connections may be mid-write.
- `service.pid` — start path relies on it to detect stale processes.

For wipes, stop the service first. See [Data Reset](Operational-SOPs-Repair-Data-Reset) and [Nuclear Reset](Operational-SOPs-Repair-Nuclear-Reset).

## Path resolution summary

| Want the path to... | Dev mode | Installed Windows | Installed macOS |
|---|---|---|---|
| Config file (on read) | `./ragtools.toml` | `%LOCALAPPDATA%\RAGTools\config.toml` | `~/Library/Application Support/RAGTools/config.toml` |
| Config file (on write) | `./ragtools.toml` | `%LOCALAPPDATA%\RAGTools\config.toml` | `~/Library/Application Support/RAGTools/config.toml` |
| Data root | `./data/` | `%LOCALAPPDATA%\RAGTools\data\` | `~/Library/Application Support/RAGTools/data/` |
| Qdrant storage | `./data/qdrant/` | `{data_root}\qdrant\` | `{data_root}/qdrant/` |
| State DB | `./data/index_state.db` | `{data_root}\index_state.db` | `{data_root}/index_state.db` |
| Service log | stderr | `{data_root}\logs\service.log` | `{data_root}/logs/service.log` |
| PID file | `./data/service.pid` | `{data_root}\service.pid` | `{data_root}/service.pid` |

Any of these can be overridden — see [Environment Variables](Reference-Environment-Variables) (`RAG_CONFIG_PATH`, `RAG_DATA_DIR`, `RAG_QDRANT_PATH`, `RAG_STATE_DB`).

## Code paths

- `src/ragtools/config.py:_get_app_dir` (line 55) — platform-specific app data dir.
- `src/ragtools/config.py:get_data_dir` (line 66) — data dir resolution with `RAG_DATA_DIR` override.
- `src/ragtools/config.py:_find_config_path` (line 90) — config file resolution with `RAG_CONFIG_PATH` override.
- `src/ragtools/config.py:_default_qdrant_path`, `_default_state_db` (lines 193-202) — default path factories for the packaged build.
