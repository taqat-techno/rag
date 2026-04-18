# Reference: HTTP API

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Source of truth** | `src/ragtools/service/routes.py` |

The Reg service exposes a FastAPI app at `127.0.0.1:<service_port>` (21420 installed, 21421 dev). No authentication — **bind localhost only; do not expose publicly**.

## Index
- [Health](#health)
- [Search](#search)
- [Indexing](#indexing)
- [Status](#status)
- [Project management](#project-management)
- [Configuration](#configuration)
- [Watcher](#watcher)
- [Semantic map](#semantic-map)
- [MCP connection](#mcp-connection)
- [Activity](#activity)
- [Crash history](#crash-history)
- [Shutdown](#shutdown)

## Health

### `GET /health`
Readiness probe.

**Response 200**
```json
{"status": "ready", "collection": "markdown_kb"}
```

**Response 503** (service not ready — encoder still loading, or not yet wired)
```json
{"detail": "Service not ready"}
```

## Search

### `GET /api/search`
Search the knowledge base.

| Query param | Type | Default | Notes |
|---|---|---|---|
| `query` | string | required | Natural-language query |
| `project` | string | null | Filter to a project id |
| `top_k` | int | 10 | Max results |
| `compact` | bool | false | Token-efficient output (used by MCP) |

Returns the result object produced by `QdrantOwner.search_formatted`.

## Indexing

### `POST /api/index`
Trigger indexing. Incremental by default.

**Body** (`IndexRequest`)
```json
{"project": null, "full": false}
```

**Response**
```json
{"stats": { /* pipeline stats */ }}
```

### `POST /api/rebuild`
Drop the collection and re-index everything from scratch. No body.

**Response**
```json
{"stats": { /* pipeline stats */ }}
```

## Status

### `GET /api/status`
Collection and index statistics.

### `GET /api/projects`
List **indexed** projects derived from the collection (file + chunk counts by project id).

### `GET /api/projects/configured`
List **configured** projects from the TOML config, with indexed stats joined in. Distinct from `/api/projects` — configured can include disabled or empty projects.

## Project management

### `POST /api/projects`
Create a project.

**Body** (`ProjectCreateRequest`)
```json
{
  "id": "my-notes",
  "name": "My notes",
  "path": "/abs/path",
  "enabled": true,
  "ignore_patterns": ["drafts/"]
}
```

Validation:
- `id` must match `^[a-z0-9][a-z0-9_-]*$`, max 64 chars — else 422.
- `id` must not collide with an existing configured project — else 422.
- `path` must resolve to an existing directory — else 422.
- Exact-duplicate `path` among existing projects — else 422.

Side effects: persists to TOML, calls `QdrantOwner.update_projects`, restarts watcher, schedules background auto-index (3 s delayed).

**Response 200**
```json
{"status": "created", "project": {"id": "...", "name": "...", "path": "..."}}
```

### `PUT /api/projects/{project_id}`
Update a project. Body (`ProjectUpdateRequest`) — all fields optional:
`name`, `path`, `enabled`, `ignore_patterns`.

### `DELETE /api/projects/{project_id}`
Remove the project and delete indexed data (Qdrant points + state DB rows).

**Response 200**
```json
{"status": "removed", "project_id": "...", "files_deleted": 47}
```

### `POST /api/projects/{project_id}/toggle`
Toggle `enabled`. Flips current value.

## Configuration

### `GET /api/config`
Return the effective subset of `Settings` that the admin panel surfaces. Not all fields — only: `embedding_model`, `chunk_size`, `chunk_overlap`, `top_k`, `score_threshold`, `collection_name`, `ignore_patterns`, `use_ragignore_files`, `service_port`, `service_host`, `log_level`, `qdrant_path`, `state_db`, `startup_enabled`, `startup_delay`, `startup_open_browser`.

### `PUT /api/config`
Partial update.

**Body** (`ConfigUpdateRequest`) — all optional:
`chunk_size` (100-2000), `chunk_overlap` (0 to min(500, chunk_size-1)), `top_k` (1-100), `score_threshold` (0.0-1.0), `service_port` (1024-65535), `log_level` (`DEBUG`/`INFO`/`WARNING`/`ERROR`).

**Hot-reload fields** — applied without restart:
`chunk_size`, `chunk_overlap`, `top_k`, `score_threshold`.

**Restart-required fields** — persisted to TOML but require `rag service restart` to take effect:
`service_port`, `log_level`.

**Response 200**
```json
{"updated": ["chunk_size"], "restart_required": false}
```

Validation failures return 422 with semicolon-joined error detail.

## Watcher

### `POST /api/watcher/start`
Start the watcher daemon thread. Idempotent.

**Response**
```json
{"status": "started", "project_count": 3}
```
or `{"status": "already_running"}`.

### `POST /api/watcher/stop`
Stop the watcher. Blocks up to 5 s waiting for the thread to join.

**Response**
```json
{"status": "stopped"}
```
or `{"status": "not_running"}`.

### `GET /api/watcher/status`
```json
{"running": true, "paths": ["/abs/a", "/abs/b"], "project_count": 2}
```

## Semantic map

### `GET /api/map/points`
2D coordinates for each indexed file (UMAP/projection result).

| Query param | Type | Default | Notes |
|---|---|---|---|
| `project` | string | null | Filter by project id |

**Response**
```json
{"points": [/* ... */], "count": 42}
```

### `POST /api/map/recompute`
Force recomputation of map coordinates.

## MCP connection

### `GET /api/mcp-config`
Return the right `mcpServers` entry for the current install:

- **Installed / frozen exe** — uses `sys.executable` + `serve`.
- **Dev with `rag-mcp` on PATH** — uses `rag-mcp` entry point under the name `ragtools-dev` (so dev + installed can coexist in the same `.mcp.json`).
- **Fallback** — `python -m ragtools.integration.mcp_server`.

**Response**
```json
{"config": {"mcpServers": {"ragtools": {"command": "...", "args": ["serve"]}}}}
```

## Activity

### `GET /api/activity`
Recent activity events for the admin-panel log.

| Query param | Type | Default | Notes |
|---|---|---|---|
| `limit` | int | 50 | Max events |
| `after` | int | 0 | Return events with `id > after` (for polling) |

**Response**
```json
{"events": [/* ... */], "count": 50}
```

## Crash history

### `GET /api/crash-history`
Unreviewed crash markers. Events older than 30 days are filtered out. The admin panel calls this on every page load and shows a dismissable banner if non-empty.

### `POST /api/crash-history/{dismiss_key}/dismiss`
Mark a crash marker as reviewed. The underlying file is renamed with a `.reviewed` suffix (preserved for post-mortem).

**Response 200**
```json
{"dismissed": "<dismiss_key>"}
```

## Shutdown

### `POST /api/shutdown`
Graceful shutdown. Stops the watcher, sets the shutdown event, then sends `SIGINT` to the current process after a 500 ms delay so the HTTP response returns first.

**Response**
```json
{"status": "shutting_down"}
```

## Error conventions

- Validation failures → 422 with `detail` as a single string, semicolon-joined if multiple.
- Not found → 404 with `detail`.
- Not ready (health) → 503 with `detail: "Service not ready"`.
- Route handlers should not raise bare exceptions — catch and return `HTTPException`.

## Code paths

- `src/ragtools/service/routes.py` — all routes.
- `src/ragtools/service/app.py` — `get_owner`, `get_settings`, `get_shutdown_event` DI.
- `src/ragtools/service/owner.py` — business logic (routes stay thin).
- `src/ragtools/service/pages.py` — admin-panel HTML view routes (not listed here — this page covers only `/api/*` + `/health`).

## Deprecations

None as of v2.4.2.
