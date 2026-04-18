# SOP: Add a New CLI Command

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Add a new subcommand to the `rag` CLI, wired to either the service (via HTTP) or direct Qdrant access, with tests and docs.

## Scope
Covers Typer subcommand authoring, the dual-mode pattern, and test/doc coverage requirements. Does not cover new HTTP endpoints (see [Add a New HTTP Endpoint](Development-SOPs-HTTP-API-Add-a-New-Endpoint)) — if the command needs a new backend operation, do that first.

## Trigger
- New user-facing CLI action needed.
- Existing behavior needs a scripted entry point.

## Preconditions
- [ ] Dev environment set up ([Fresh Install (Dev)](Operational-SOPs-Installation-Fresh-Install-Dev)).
- [ ] Clear picture of which execution mode the command needs: service-required, dual-mode (service + direct fallback), or local-only.

## Inputs
- Command name, args, options, help text.
- Parent (top-level `app`, or a sub-typer like `service_app`, `project_app`, `ignore_app`).

## Pattern reference

`src/ragtools/cli.py` already exposes three templates. Pick the one that matches your mode.

### Pattern A — Service required (no direct fallback)

Example: `rag index` (requires the indexer running inside the service).

```python
@app.command()
def mycmd(option: str = typer.Option(..., "--option")):
    """One-line help."""
    settings = _get_settings()
    if _probe_service(settings):
        import httpx
        try:
            r = httpx.post(
                f"{_service_url(settings)}/api/mycmd",
                json={"option": option},
                timeout=30.0,
            )
            r.raise_for_status()
            # render response
        except Exception as e:
            console.print(f"[red]mycmd via service failed:[/red] {e}")
            raise typer.Exit(1)
    else:
        console.print(
            "[yellow]Service is not running.[/yellow] "
            "Start with: [bold]rag service start[/bold]"
        )
        raise typer.Exit(1)
```

### Pattern B — Dual-mode (service preferred, direct fallback)

Example: `rag search`, `rag status`, `rag projects`, `rag rebuild`.

```python
@app.command()
def mycmd(query: str = typer.Argument(...)):
    """One-line help."""
    settings = _get_settings()
    if _probe_service(settings):
        # HTTP path via httpx -> _service_url/api/mycmd
        ...
    else:
        # Direct path using settings.get_qdrant_client() + Encoder/Indexer/etc.
        ...
```

### Pattern C — Local-only (no Qdrant touch)

Example: `rag doctor`, `rag version`, `rag ignore test`.

```python
@app.command()
def mycmd():
    """One-line help."""
    # Read-only diagnostics; no service probe, no Qdrant client.
    ...
```

## Steps

1. **Decide the mode** using the patterns above. Be honest about whether you need writes (service-required or dual-mode) or only reads (local-only).

2. **Add the command** to `src/ragtools/cli.py`:
   - Top-level commands: `@app.command()`.
   - Namespaced commands: `@service_app.command()`, `@project_app.command()`, `@ignore_app.command()`. If you need a new namespace, add a new sub-typer: `my_app = typer.Typer(...); app.add_typer(my_app, name="my")`.

3. **Reuse helpers** — do not re-implement:
   - `_get_settings()` — load Settings.
   - `_probe_service(settings)` — health check.
   - `_service_url(settings)` — build base URL.
   - `_get_ignore_rules(settings)` — build ignore rules.
   - `console` — rich output.

4. **Handle errors consistently.** On HTTP failure or backend error, print a red message and `raise typer.Exit(1)`. Never swallow exceptions silently.

5. **Add tests.**
   - Smoke test in `tests/test_smoke.py` (invocation does not crash).
   - If the command touches indexing, routing, or state, add a dedicated test module or extend `tests/test_service.py` / `tests/test_integration.py`.
   - Use the `isolate_config` autouse fixture (prevents CWD `ragtools.toml` leaking into tests).
   - Use `memory_client` / `Settings.get_memory_client()` for Qdrant — **never an on-disk path in tests**.

6. **Update the CLI Reference** page (Phase 7 deliverable) with the new subcommand. Interim: add a row to `Architecture/CLI-Dual-Mode.md` if behavior is dual-mode-relevant.

7. **Run the suite:**
   ```
   pytest
   ```

## Validation / expected result

- `rag --help` lists the new command.
- `rag mycmd --help` prints the Typer-generated help.
- Smoke path works end-to-end with and without the service running (per the chosen mode).
- `pytest` passes.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Command not found after `pip install -e` | Venv not reactivated | Reactivate venv. |
| Typer help missing the command | Decorator on wrong app | Confirm `@app.command()` vs sub-typer. |
| HTTP call hangs | Timeout missing or too long | Provide `timeout=` on `httpx` calls; index-style operations use 300 s, queries 5-10 s. |
| Tests flaky on Qdrant | Using on-disk Qdrant | Switch to `Settings.get_memory_client()`. |
| Service probe always fails in tests | Real service running on the test host | Monkey-patch `_probe_service` or bind to a different port via `RAG_SERVICE_PORT`. |

## Recovery / rollback
- Revert `cli.py` changes.
- Roll back tests.

## Related code paths
- `src/ragtools/cli.py:app` (line 12) — top-level Typer app.
- `src/ragtools/cli.py:_probe_service` (line 42).
- `src/ragtools/cli.py:_service_url` (line 60).
- `src/ragtools/cli.py:search` (line 99) — dual-mode reference implementation.
- `src/ragtools/cli.py:index` (line 70) — service-required reference implementation.
- `src/ragtools/cli.py:doctor` (line 209) — local-only reference implementation.

## Related commands
- `pytest`, `pytest tests/test_smoke.py`.
- `rag --help`, `rag mycmd --help`.

## Change log
- 2026-04-18 — Initial draft.
