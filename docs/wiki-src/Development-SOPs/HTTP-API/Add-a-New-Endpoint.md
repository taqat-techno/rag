# SOP: Add a New HTTP Endpoint

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Add a new route to the Reg service's FastAPI app, wire it through `QdrantOwner`, and integrate it with the CLI if the command line needs to reach it.

## Scope
Covers `src/ragtools/service/routes.py` additions, Pydantic request/response models, CLI dual-mode wiring, and tests. Does not cover HTML admin-panel pages (see the Admin Panel SOP, Phase 7 area) — those are separate view routes.

## Trigger
- New backend operation needed (e.g. a stats endpoint, a project-level operation, an export).
- CLI command needs a service-side counterpart.

## Preconditions
- [ ] Dev environment set up.
- [ ] The backing operation exists on `QdrantOwner` or can be added there. The endpoint is a thin HTTP adapter — business logic belongs in `owner.py` or the relevant indexing/retrieval module.

## Inputs
- Route path (`/api/<name>`), method (GET / POST).
- Request shape (query params or JSON body).
- Response shape.

## Steps

1. **Add the Pydantic models** (if the endpoint takes JSON or returns structured data) at the top of `routes.py`, alongside the existing `IndexRequest`, `ProjectCreateRequest` etc.:

   ```python
   class MyRequest(BaseModel):
       project: Optional[str] = None
       flag: bool = False
   ```

2. **Add the route** to `router`:

   ```python
   @router.post("/api/mycmd")
   def mycmd(req: MyRequest):
       """One-line description."""
       owner = get_owner()
       result = owner.do_mycmd(project_id=req.project, flag=req.flag)
       return {"result": result}
   ```

   - Use `Query(...)` for GET params with validation.
   - Use `owner = get_owner()` — this is the service-global `QdrantOwner` from `service/app.py`. Do not open a new Qdrant client.
   - Raise `HTTPException(status_code=..., detail="...")` for caller errors.

3. **Implement the backing method on `QdrantOwner`.** Route handlers are thin; real work lives in `owner.do_mycmd(...)`. This keeps the lock semantics correct (owner acquires RLock around Qdrant access) and makes the logic unit-testable without FastAPI.

4. **Wire the CLI** (if this endpoint is user-facing). Follow [Add a New CLI Command](Development-SOPs-Commands-Add-a-New-Command). Choose service-required or dual-mode pattern as appropriate — the HTTP endpoint is the service half.

5. **Add tests.**
   - Unit test the owner method.
   - Integration test the route with FastAPI's `TestClient` (examples in `tests/test_service.py`, `tests/test_pages.py`).
   - Use `memory_client` fixture for Qdrant.

6. **Update `Reference/HTTP-API`** (Phase 7 deliverable) with the new endpoint: path, method, request, response, error codes. Until then, the canonical source is `routes.py` itself.

## Conventions

- **Path style:** `/api/<resource>` (plural for collections, singular for single-object ops). Existing: `/api/search`, `/api/index`, `/api/rebuild`, `/api/status`, `/api/projects`.
- **Health probe:** never under `/api/`. `/health` is the fixed contract used by the CLI and MCP dual-mode probes.
- **HTTP method:** GET for pure reads (search, status, projects); POST for mutations or expensive ops (index, rebuild).
- **Validation:** reject malformed input at the route layer with `HTTPException`. See `_validate_project_id` (line 111) as a reference validator.
- **Idempotency:** mutating endpoints should be idempotent when safe. `/api/rebuild` is not idempotent by design (always drops); `/api/index` is (no-ops on unchanged files).
- **Authentication:** none. Service binds `127.0.0.1` only. Do not add endpoints that would be catastrophic if exposed — and do not expose the service publicly.

## Validation / expected result

- `curl http://127.0.0.1:<port>/api/mycmd` (or matching POST) returns the expected shape.
- The endpoint appears in FastAPI's auto-generated `/docs` (when enabled).
- Tests pass via `pytest tests/test_service.py`.
- CLI command (if wired) works end-to-end.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| 503 on every call | Service not reaching "ready" — encoder or Qdrant failed to load | Check `{data_dir}/logs/service.log`. [Service Fails to Start](Runbooks-Service-Fails-to-Start). |
| 500 with stack trace in log | Unhandled exception in route handler | Wrap the call in try/except, raise `HTTPException`, log the detail. |
| Lock contention | Route opened a Qdrant client directly instead of using `QdrantOwner` | Route through `get_owner()` exclusively. See [Single-Process Invariant](Core-Concepts-Single-Process-Invariant). |
| CLI forwards succeed but client sees empty response | Pydantic model mismatch between route return and CLI's expected shape | Align both to a shared schema. |

## Recovery / rollback
- Revert `routes.py`, `owner.py`, and `cli.py` changes.
- Roll back tests.

## Related code paths
- `src/ragtools/service/routes.py` — HTTP API router + request models.
- `src/ragtools/service/app.py` — `get_owner`, `get_settings`, `get_shutdown_event` DI helpers.
- `src/ragtools/service/owner.py` — `QdrantOwner` (business logic goes here).
- `src/ragtools/cli.py` — CLI client-side wiring.

## Related commands
- `pytest tests/test_service.py tests/test_pages.py`.

## Change log
- 2026-04-18 — Initial draft.
