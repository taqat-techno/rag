# Reference: Environment Variables

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Source of truth** | `src/ragtools/config.py` — `Settings` (prefix `RAG_`) + `get_data_dir` / `_find_config_path` |

Every environment variable Reg consults. Env values override TOML and `.env` — see [Configuration Precedence](Operational-SOPs-Configuration-Configuration-Precedence).

## Settings variables (prefix `RAG_`)

Every `Settings` field exposes an env var by uppercasing the field name and prefixing `RAG_`. See [Configuration Keys](Reference-Configuration-Keys) for types, defaults, and full descriptions.

| Env var | Controls |
|---|---|
| `RAG_QDRANT_PATH` | Qdrant local storage directory. |
| `RAG_COLLECTION_NAME` | Qdrant collection name (`markdown_kb`). |
| `RAG_EMBEDDING_MODEL` | Model ID for Sentence-Transformers. |
| `RAG_EMBEDDING_DIM` | Embedding dimensionality. Must match the model. |
| `RAG_CHUNK_SIZE` | Target tokens per chunk. |
| `RAG_CHUNK_OVERLAP` | Overlap tokens between adjacent chunks. |
| `RAG_CONTENT_ROOT` | Legacy v1 content root. |
| `RAG_CONFIG_VERSION` | Config schema version. |
| `RAG_TOP_K` | Default number of search results. |
| `RAG_SCORE_THRESHOLD` | Minimum cosine score for inclusion. |
| `RAG_STATE_DB` | Path to SQLite state DB. |
| `RAG_IGNORE_PATTERNS` | Global ignore patterns (gitignore syntax). |
| `RAG_USE_RAGIGNORE_FILES` | Honor per-directory `.ragignore` files (`true` / `false`). |
| `RAG_SERVICE_HOST` | Service bind host. Keep `127.0.0.1` — no auth. |
| `RAG_SERVICE_PORT` | Service bind port. |
| `RAG_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |
| `RAG_STARTUP_ENABLED` | Register/respect Windows auto-start. |
| `RAG_STARTUP_DELAY` | Seconds to wait after login before start. |
| `RAG_STARTUP_OPEN_BROWSER` | Open the admin panel on auto-start. |

## Path-resolution variables

Not Pydantic fields — consulted directly by path-resolution code before `Settings` is loaded.

| Env var | Consulted by | Purpose |
|---|---|---|
| `RAG_CONFIG_PATH` | `config.py:_find_config_path` | Explicit path to the TOML config file. Highest priority in config-file resolution. If set but the file does not exist, **no config file is loaded** (not a silent fallthrough). |
| `RAG_DATA_DIR` | `config.py:get_data_dir` | Override the computed data directory. Honored in both dev and installed modes. |

## Platform variables Reg reads

Reg does not set these, but reads them when resolving paths:

| Env var | Platform | Purpose |
|---|---|---|
| `LOCALAPPDATA` | Windows | Base for installed-mode data dir (`%LOCALAPPDATA%\RAGTools\`). |
| `HOME` | macOS | Base for installed-mode data dir (`~/Library/Application Support/RAGTools/`). |

## `.env` file

`Settings` is configured with `env_file = ".env"`. If a `.env` file exists at the process CWD, its values are loaded at a lower priority than real env vars (init > env > TOML > `.env` > defaults). Use this for dev machines; do not commit it.

## Type coercion

Pydantic coerces env strings to field types. Expect:

- Integers and floats parsed directly. `RAG_TOP_K=not-a-number` → startup error.
- Booleans: `true` / `false` / `1` / `0` / `yes` / `no` — case-insensitive.
- Lists (e.g. `RAG_IGNORE_PATTERNS`): JSON array syntax — `RAG_IGNORE_PATTERNS='["drafts/", "*.tmp"]'`.
- Complex types (e.g. `projects`): not settable via env — use TOML.

## Code paths

- `src/ragtools/config.py:Settings` (lines 213-256).
- `src/ragtools/config.py:_find_config_path` (line 90).
- `src/ragtools/config.py:get_data_dir` (line 66).
- `src/ragtools/config.py:_get_app_dir` (line 55) — platform branch.
