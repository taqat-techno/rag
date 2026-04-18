# Standards & Governance: Coding Standards

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |

Conventions for writing Python in this repo. Enforced by review, not by an automated linter (as of v2.4.2, `pyproject.toml` carries no `[tool.ruff]` / `[tool.black]` / `[tool.mypy]` config — adding these is a candidate future ADR).

## Language and runtime

- **Python:** `>=3.10` (per `pyproject.toml`). Target 3.12 for development; avoid language features newer than 3.10 unless guarded.
- **Stdlib TOML:** use `tomllib` (3.11+) with a `tomli` fallback for 3.10 — the pattern in `src/ragtools/config.py:_load_toml` (lines 143-171).

## Type hints

- **Required** on public module-level functions, class `__init__`, and any function signature touched by multiple modules.
- **Optional** on private helpers and one-liners.
- Use modern syntax: `list[str]`, `dict[str, int]`, `str | None` (PEP 604) — consistent with existing code.
- Pydantic models are preferred for structured data (see `Settings`, `ProjectConfig`, route request/response models).

## Imports

Ordering (matches existing files):

1. Stdlib.
2. Third-party.
3. Local (`ragtools.*`).

Blank line between groups. Prefer explicit imports over `from x import *`. **Avoid eager imports of heavy dependencies** at module top level if only used in one code path — `import httpx` inside the function that calls it is the pattern used in `src/ragtools/cli.py` to keep CLI startup fast.

## Error handling

- **At system boundaries** (user input, HTTP routes, subprocess results): validate and return clear errors. `HTTPException(status_code=422, detail="...")` for API; `raise typer.Exit(1)` with `console.print` for CLI.
- **Internally**: trust framework guarantees. Don't wrap every call in try/except "just in case."
- **Never swallow exceptions silently**. If you catch, log and surface.
- Route handlers must not leak tracebacks to the client — catch and convert to `HTTPException`.

## Logging

Use `logging.getLogger("ragtools.<component>")`. Per ADR-9:

- `ragtools.service` for the FastAPI app and routes.
- `ragtools.indexing` for the pipeline.
- `ragtools.watcher` for watchfiles-related code.
- `ragtools.mcp` for the MCP server.
- `ragtools.config` for config resolution.

Format (set in the service): `%(asctime)s %(levelname)-8s %(name)s %(message)s`. MCP logs go to stderr exclusively; stdout is reserved for MCP stdio transport.

## Comments

- **Default to no comments.** Well-named identifiers and types do the work.
- Write a comment only when the WHY is non-obvious — a hidden constraint, a workaround for a specific bug, behavior that would surprise a reader.
- **Do not** describe what the code does; do not reference the task or PR; do not leave "TODO" without an issue link.

## Public API surfaces to respect

- `Settings` fields are versioned implicitly by `config_version`. Adding a field is free; renaming or removing requires migration planning (`config.py:migrate_v1_to_v2` is the reference).
- HTTP route paths (`/api/*`) are a user contract once shipped. Renaming is a breaking change.
- MCP tool names (`search_knowledge_base`, `list_projects`, `index_status`) are a Claude CLI contract.
- `QdrantOwner` method signatures: changing them ripples into every route handler.

## What not to do

- Do **not** re-instantiate `QdrantClient` inside route handlers — use `get_owner()` (per ADR-1, ADR-12).
- Do **not** add dependencies without updating `pyproject.toml` and considering the packaging impact — each new dependency grows the PyInstaller bundle.
- Do **not** introduce async anywhere without a concrete reason. Reg is a single-process synchronous service — async adds complexity without throughput gain at current scale.
- Do **not** write JS or add `package.json` — the admin panel is htmx + Jinja2 (ADR-6).
- Do **not** add `--no-verify`, `--no-gpg-sign`, or other commit bypasses.

## Reference code patterns

- **CLI dual-mode:** `src/ragtools/cli.py:search` (line 99) — full pattern with fallback.
- **CLI service-required:** `src/ragtools/cli.py:index` (line 70) — pattern with explicit error exit.
- **HTTP route:** `src/ragtools/service/routes.py:search` (line 45) — thin adapter over `QdrantOwner`.
- **Pydantic request model:** `IndexRequest` (line 26 of `routes.py`).
- **Validation helper:** `_validate_project_id` (line 111 of `routes.py`) — returns `None` or error string.

## Related

- [Testing Standards](Standards-and-Governance-Testing-Standards).
- [Review and Merge Standards](Standards-and-Governance-Review-and-Merge-Standards).
- [Architecture Decisions](Standards-and-Governance-Architecture-Decisions).
