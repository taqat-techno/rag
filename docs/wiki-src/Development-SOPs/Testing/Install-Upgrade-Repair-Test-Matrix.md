# SOP: Install / Upgrade / Repair Test Matrix

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Define the coverage matrix every change must satisfy — which test runs must pass on which platform in which install mode before merge and before release.

## Scope
Applies to every PR touching `src/ragtools/` or install/packaging tooling. Feature-specific tests are authored alongside code; this SOP ensures the cross-cutting axes are also covered.

## Trigger
- Opening a PR.
- Cutting a release.
- Investigating a regression suspected to be platform- or mode-specific.

## Preconditions
- [ ] All dependencies installed (`pip install -e ".[dev]"`).
- [ ] For install/upgrade/repair checks: access to a Windows 11 host.

## Inputs
- Branch / tag under test.

## Test module inventory (`tests/`)

21 modules as of v2.4.2. Grouped by category:

| Category | Modules |
|---|---|
| **Unit — core pipeline** | `test_chunking`, `test_scanner`, `test_ignore`, `test_retrieval`, `test_indexing`, `test_map_data` |
| **Unit — config & state** | `test_config_migration` |
| **Unit — service internals** | `test_owner`, `test_process`, `test_scale_warning` |
| **Integration — end-to-end pipeline** | `test_integration`, `test_incremental` |
| **Integration — service** | `test_service`, `test_pages`, `test_startup`, `test_supervisor`, `test_activity` |
| **Integration — MCP** | `test_mcp_proxy` |
| **Resilience** | `test_fatal_crash_logging` |
| **Smoke** | `test_smoke` |
| **Eval** | `test_eval` (runs retrieval quality eval via `scripts/eval_retrieval.py`) |

Common fixtures from `tests/conftest.py`:
- `isolate_config` (autouse) — monkeypatches `RAG_CONFIG_PATH` to a non-existent file in tmp so tests never pick up a CWD `ragtools.toml`.
- `settings` — defaults-only Settings.
- `memory_client` — `Settings.get_memory_client()`, in-memory Qdrant.

## Matrix — per-PR gate

| Axis | Windows dev | Windows installed | macOS dev | macOS installed |
|---|---|---|---|---|
| `pytest` (unit + integration) | CI required | — | CI required | — |
| `pytest -k smoke` quick gate | CI + local | — | CI | — |
| Dev-mode `rag doctor` | CI | — | CI | — |
| Installed startup | — | Manual (pre-release only) | — | Manual (pre-release only) |

Baseline: every PR must pass `pytest` on the Windows + macOS CI runners. The installed-mode checks are release-gate, not PR-gate.

## Matrix — release gate

Additional checks beyond the per-PR gate, enforced via the lifecycle gate in [Release Checklist](Development-SOPs-Release-Release-Checklist) and `docs/RELEASE_LIFECYCLE.md`:

| Check | Tool / how | Windows | macOS |
|---|---|---|---|
| PyInstaller local build succeeds | `python scripts/build.py --no-model` | Required | Required |
| Installer runs on a clean VM | Manual | Required | Required |
| Upgrade over prior version preserves user data | Manual; inspect `%LOCALAPPDATA%\RAGTools\` / `~/Library/Application Support/RAGTools/` before and after | Required | Required |
| Uninstall with "keep data" preserves user data | Manual | Required | — |
| Uninstall with "wipe" removes user data | Manual | Required | — |
| Soft reset rebuilds cleanly | Manual, on a test install | Required | Required |
| Data reset requires `DELETE` verbatim | Manual | Required | Required |
| Nuclear reset removes config | Manual | Required | Required |
| MCP proxy mode starts in <1 s when service is up | Manual with Claude CLI | Required | Required |
| MCP direct mode loads encoder and serves | Manual with Claude CLI (service stopped) | Required | Required |
| `rag doctor` on fresh install reports OK | Manual | Required | Required |

## Steps — running the suite

### Unit + integration
```
pytest
```
Runs all modules. No on-disk Qdrant (enforced via `isolate_config` + `memory_client`).

### Filter by category
```
pytest tests/test_chunking.py tests/test_indexing.py tests/test_retrieval.py
pytest -k integration
pytest -k "mcp or service"
```

### Coverage
```
pytest --cov=ragtools
```

### Retrieval eval
```
python scripts/eval_retrieval.py --questions tests/fixtures/eval_questions.json
```
Reports f1, mrr, ndcg. Run before release; regressions in any metric are a release blocker.

## Validation / expected result

- PR gate: CI green on Windows + macOS runners.
- Release gate: every row in the release-gate table above has a green or manually-signed-off result.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `tests/test_service.py` flaky | Port collision with real running service | `RAG_SERVICE_PORT=0` or stop local service before testing. |
| Qdrant-related tests slow or flaky | Using on-disk Qdrant | Use `memory_client` fixture exclusively. |
| `test_eval` quality regression | Embedding model or chunking change | Either justify with evidence or revert. Not a flake. |
| Installed-mode check fails on VM | PyInstaller missed a hidden import | Add to `rag.spec`; rebuild; re-test. |

## Recovery / rollback
- For broken CI: fix forward, not revert of the test change, unless the test itself is wrong.
- For installed-mode regressions discovered pre-release: do not tag; iterate on packaging.

## Related code paths
- `tests/` — all modules.
- `tests/conftest.py` — shared fixtures.
- `tests/fixtures/` — sample Markdown + eval questions.
- `scripts/eval_retrieval.py` — eval harness.
- `scripts/build.py` — packaging validation.
- `docs/RELEASE_LIFECYCLE.md` — the gate document for release-level checks.

## Related commands
- `pytest`, `pytest --cov=ragtools`, `python scripts/eval_retrieval.py`, `python scripts/build.py --no-model`.

## Change log
- 2026-04-18 — Initial draft (21 test modules).
