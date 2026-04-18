# Reference: Configuration Keys

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Source of truth** | `src/ragtools/config.py` — `Settings` class (Pydantic `BaseSettings`) |

Every Reg configuration key. Each key can be set three ways — env var, TOML config file, or left at default — with the precedence documented in [Configuration Precedence](Operational-SOPs-Configuration-Configuration-Precedence).

## Index
- [Qdrant](#qdrant)
- [Embedding](#embedding)
- [Chunking](#chunking)
- [Content and projects](#content-and-projects)
- [Retrieval](#retrieval)
- [State](#state)
- [Ignore rules](#ignore-rules)
- [Service](#service)
- [Startup (Windows)](#startup-windows)
- [Process-level only](#process-level-only)

## Qdrant

| Pydantic field | Env var | TOML | Default | Notes |
|---|---|---|---|---|
| `qdrant_path` | `RAG_QDRANT_PATH` | top-level `qdrant_path` | dev: `data/qdrant`  · installed: `{data_dir}/data/qdrant` | Qdrant local storage directory. |
| `collection_name` | `RAG_COLLECTION_NAME` | top-level `collection_name` | `markdown_kb` | Single collection for all projects. Do not change casually. |

## Embedding

| Pydantic field | Env var | TOML | Default | Notes |
|---|---|---|---|---|
| `embedding_model` | `RAG_EMBEDDING_MODEL` | top-level `embedding_model` | `all-MiniLM-L6-v2` | Changing requires a full rebuild. |
| `embedding_dim` | `RAG_EMBEDDING_DIM` | top-level `embedding_dim` | `384` | Must match the model's output dimensionality. |

## Chunking

| Pydantic field | Env var | TOML | Default | Notes |
|---|---|---|---|---|
| `chunk_size` | `RAG_CHUNK_SIZE` | `[indexing].chunk_size` or top-level | `400` | Target tokens per chunk. |
| `chunk_overlap` | `RAG_CHUNK_OVERLAP` | `[indexing].chunk_overlap` or top-level | `100` | Overlap tokens between adjacent chunks. |

## Content and projects

| Pydantic field | Env var | TOML | Default | Notes |
|---|---|---|---|---|
| `content_root` | `RAG_CONTENT_ROOT` | top-level `content_root` | `.` | **v1 legacy.** Each immediate subdirectory is a project unless explicit `[[projects]]` is set. |
| `projects` | (TOML only) | `[[projects]]` array | `[]` | **v2 explicit** list of `ProjectConfig` entries (`id`, `name`, `path`, `enabled`, `ignore_patterns`). |
| `config_version` | `RAG_CONFIG_VERSION` | top-level `version` | `1` | Schema version of the config file itself. |

`ProjectConfig` fields (per entry in `[[projects]]`):

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | string | (required) | Unique, used in storage keys and payload filter. |
| `name` | string | `id` | Display name. |
| `path` | string | (required) | Absolute path to the project folder. |
| `enabled` | bool | `true` | Skip if `false`. |
| `ignore_patterns` | list[string] | `[]` | Per-project ignore patterns. |

## Retrieval

| Pydantic field | Env var | TOML | Default | Notes |
|---|---|---|---|---|
| `top_k` | `RAG_TOP_K` | `[retrieval].top_k` | `10` | Default number of search results. |
| `score_threshold` | `RAG_SCORE_THRESHOLD` | `[retrieval].score_threshold` | `0.3` | Below this, results are excluded entirely. See [Confidence Model](Core-Concepts-Confidence-Model). |

## State

| Pydantic field | Env var | TOML | Default | Notes |
|---|---|---|---|---|
| `state_db` | `RAG_STATE_DB` | top-level `state_db` | dev: `data/index_state.db`  · installed: `{data_dir}/data/index_state.db` | SQLite state tracker for incremental indexing. |

## Ignore rules

| Pydantic field | Env var | TOML | Default | Notes |
|---|---|---|---|---|
| `ignore_patterns` | `RAG_IGNORE_PATTERNS` | `[ignore].patterns` | `[]` | Global ignore patterns applied to every project. |
| `use_ragignore_files` | `RAG_USE_RAGIGNORE_FILES` | `[ignore].use_ragignore_files` | `true` | Honor per-directory `.ragignore` files. |

## Service

| Pydantic field | Env var | TOML | Default | Notes |
|---|---|---|---|---|
| `service_host` | `RAG_SERVICE_HOST` | `[service].host` | `127.0.0.1` | Do not bind publicly — no auth. |
| `service_port` | `RAG_SERVICE_PORT` | `[service].port` | installed: `21420`  · dev: `21421` | Dev offsets to 21421 to coexist with an installed service on the same host. |
| `log_level` | `RAG_LOG_LEVEL` | `[service].log_level` or top-level | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |

## Startup (Windows)

| Pydantic field | Env var | TOML | Default | Notes |
|---|---|---|---|---|
| `startup_enabled` | `RAG_STARTUP_ENABLED` | `[startup].enabled` | `false` | Whether the service auto-starts on login. Registration happens via `rag service install`. |
| `startup_delay` | `RAG_STARTUP_DELAY` | `[startup].delay_seconds` | `30` | Seconds to wait after login before starting the service. |
| `startup_open_browser` | `RAG_STARTUP_OPEN_BROWSER` | `[startup].open_browser` | `false` | Open the admin panel automatically after start. |

## Process-level only

These are not fields on `Settings` — they are environment variables consulted at path-resolution time:

| Env var | Purpose |
|---|---|
| `RAG_CONFIG_PATH` | Explicit path to the TOML config file. Overrides the `%LOCALAPPDATA%` / `./ragtools.toml` search. |
| `RAG_DATA_DIR` | Override the data directory. Honored in both dev and installed modes. |

## TOML-to-field mapping rules

`config.py:_load_toml` flattens the TOML file into Pydantic field names with these rules:

- `[section]` + `key = value` → `section_key` (e.g. `[service].port` → `service_port`).
- `[ignore].patterns` → `ignore_patterns` (special case).
- `[ignore].use_ragignore_files` → `use_ragignore_files` (special case).
- Top-level `version = N` → `config_version` (renamed).
- Top-level `[[projects]]` array → `projects` list.
- Top-level scalar keys (e.g. `chunk_size = 500`) → field of the same name.

## Code paths
- `src/ragtools/config.py:Settings` (lines 213-256) — field definitions.
- `src/ragtools/config.py:_load_toml` (lines 143-171) — TOML flattening rules.
- `src/ragtools/config.py:_default_service_port` (line 205) — dev vs installed port policy.

## Deprecations
- `content_root` — v1 legacy. New configs should use `[[projects]]`. At runtime, `migrate_v1_to_v2` auto-discovers projects from `content_root` when `[[projects]]` is empty (see `config.py:migrate_v1_to_v2`, lines 304-339). No TOML is written back — migration is in-memory only.
