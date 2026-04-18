# Standards & Governance: Testing Standards

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |

The non-negotiable rules for writing and running tests in this repo. For the coverage matrix (per-PR vs release-gate) see [Install / Upgrade / Repair Test Matrix](Development-SOPs-Testing-Install-Upgrade-Repair-Test-Matrix).

## The three invariants

1. **Tests never touch on-disk Qdrant.** Use `Settings.get_memory_client()` (returns `QdrantClient(":memory:")`) via the `memory_client` fixture. A test that opens `./data/qdrant/` is a bug.
2. **Tests never pick up the CWD `ragtools.toml`.** The `isolate_config` autouse fixture in `tests/conftest.py` monkeypatches `RAG_CONFIG_PATH` to a non-existent file in `tmp_path`. If a test needs config, construct `Settings` with explicit init kwargs or point `RAG_CONFIG_PATH` at a controlled fixture file.
3. **Tests isolate state.** Use `tmp_path` / `tmp_path_factory` for any filesystem work. Never write under `./data/`, `./tests/data/`, or the user's home directory.

## Stack

- `pytest >=8.0.0` (required; pinned in `[project.optional-dependencies].dev`).
- `pytest-cov >=5.0.0` for coverage (optional).
- `pytest-asyncio >=0.23.0` for async helpers.
- No other test runner. No unittest. No nose.

## Fixtures (`tests/conftest.py`)

| Fixture | Scope | Purpose |
|---|---|---|
| `isolate_config` | function (autouse) | Monkeypatch `RAG_CONFIG_PATH` → non-existent path in tmp. Prevents leakage from dev `ragtools.toml`. |
| `settings` | function | `Settings()` with defaults only. |
| `memory_client` | function | `Settings.get_memory_client()`. In-memory Qdrant. |

Additional fixtures live in test modules; don't move them to `conftest.py` unless they are genuinely shared.

## Module layout

- Unit tests mirror source modules: `test_chunking.py` for `src/ragtools/chunking/`, etc.
- Integration tests live alongside unit tests, named for what they integrate: `test_integration.py`, `test_service.py`, `test_mcp_proxy.py`.
- Eval: `test_eval.py` imports `scripts/eval_retrieval.py` for retrieval-quality gating.

The full list of 21 modules as of v2.4.2 is enumerated in [Install / Upgrade / Repair Test Matrix](Development-SOPs-Testing-Install-Upgrade-Repair-Test-Matrix#test-module-inventory-tests).

## What every PR must test

| Change kind | Required test |
|---|---|
| Bug fix | A regression test that fails against the broken code and passes against the fix. |
| New CLI command | A smoke test in `tests/test_smoke.py` (at minimum) plus a dedicated test module if the command has logic beyond argument parsing. |
| New HTTP endpoint | Integration test against FastAPI `TestClient` in `tests/test_service.py` or `tests/test_pages.py`. |
| New `QdrantOwner` method | Direct unit test with `memory_client`. |
| Chunking / encoding / retrieval change | Include an update to `scripts/eval_retrieval.py` inputs if the change could shift the quality metric. |
| Config schema change | Migration test in `test_config_migration.py`. Must cover both "old config, new code" and "new config, old defaults". |
| Packaging / install change | Manual release-gate test (see the test matrix). CI cannot fully cover this. |

## What not to test

- Third-party library behavior (Qdrant, SentenceTransformers, FastAPI). Trust it.
- Exact log strings. Test behavior, not log formatting.
- Flakey external services. Either mock, or move to an opt-in integration suite.

## Running

```
pytest                              # full suite
pytest tests/test_chunking.py       # single module
pytest -k "mcp or service"          # by expression
pytest --cov=ragtools               # with coverage
pytest -x                           # stop at first failure
```

Eval is separate:
```
python scripts/eval_retrieval.py --questions tests/fixtures/eval_questions.json
```

## Non-determinism

- Embedding outputs are deterministic for a fixed model version. If a retrieval test flaps, do **not** raise the tolerance — investigate.
- Time-sensitive assertions (watcher debounce, service startup) use `time.monotonic()` or explicit sleep-with-retry. Avoid wall-clock checks.

## Coverage expectations

No hard floor enforced today. Target: no regression in module-level coverage on changed files. CI does not block on coverage.

## Related

- [Install / Upgrade / Repair Test Matrix](Development-SOPs-Testing-Install-Upgrade-Repair-Test-Matrix).
- [Coding Standards](Standards-and-Governance-Coding-Standards).
- [Add a New Command](Development-SOPs-Commands-Add-a-New-Command).
- [Add a New HTTP Endpoint](Development-SOPs-HTTP-API-Add-a-New-Endpoint).
