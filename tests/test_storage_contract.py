"""The v3 storage contract: which configurations are supported, enforced.

Both values were already refused deeper in — `resolve_backend` raises on an
unknown engine, `CollectionRouter` on an unknown layout — but only at the moment
something needed them. A typo in `config.toml` therefore surfaced during owner
construction, as a traceback naming a module the user has never heard of, at the
one moment they least want one: the service failing to start after an upgrade.

The contract itself is small and worth stating plainly:

* **engine** ∈ {embedded, managed, external} — what serves the collections;
* **layout** ∈ {shared, per_project} — which collections exist;
* the two **compose**: all six combinations are supported;
* `external` additionally needs `storage_url`, because it is the one engine
  ragtools neither starts nor stops and therefore cannot discover;
* `managed` does NOT need it at config time — the service starts the server and
  fills in the address afterwards, so demanding it here would refuse a valid
  configuration.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from ragtools.config import (
    _SUPPORTED_BACKENDS,
    _SUPPORTED_STRATEGIES,
    Settings,
)


def build(tmp_path, **kwargs) -> Settings:
    return Settings(qdrant_path=str(tmp_path / "qdrant"),
                    state_db=str(tmp_path / "state.db"), **kwargs)


# --- every supported combination must load --------------------------------


@pytest.mark.parametrize("backend,strategy", sorted(
    itertools.product(sorted(_SUPPORTED_BACKENDS), sorted(_SUPPORTED_STRATEGIES))))
def test_every_declared_combination_is_accepted(tmp_path, backend, strategy):
    """The layout decides which collections exist and the engine decides what
    serves them; nothing about one constrains the other."""
    extra = {"storage_url": "http://127.0.0.1:6333"} if backend == "external" else {}
    settings = build(tmp_path, storage_backend=backend,
                     collection_strategy=strategy, **extra)

    assert settings.storage_backend == backend
    assert settings.collection_strategy == strategy


def test_managed_does_not_need_a_url_at_config_time(tmp_path):
    """The service starts the server and sets `storage_url` afterwards.
    Requiring it here would refuse a configuration that is entirely valid."""
    settings = build(tmp_path, storage_backend="managed")

    assert settings.storage_backend == "managed"
    assert not settings.storage_url


# --- and every unsupported one must be refused, at load --------------------


@pytest.mark.parametrize("bad", ["qdrant", "server", "sqlite", "", "EMBEDED"])
def test_an_unknown_engine_is_refused_when_the_config_loads(tmp_path, bad):
    with pytest.raises(ValueError) as excinfo:
        build(tmp_path, storage_backend=bad)

    message = str(excinfo.value)
    assert "storage_backend" in message
    assert "embedded" in message, "the message does not name the alternatives"


@pytest.mark.parametrize("bad", ["per-project", "perproject", "one_per_project", ""])
def test_an_unknown_layout_is_refused_when_the_config_loads(tmp_path, bad):
    with pytest.raises(ValueError) as excinfo:
        build(tmp_path, collection_strategy=bad)

    message = str(excinfo.value)
    assert "collection_strategy" in message
    assert "per_project" in message


def test_external_without_a_url_is_refused_with_a_reason(tmp_path):
    """The one engine ragtools neither starts nor stops, so the one it cannot
    discover."""
    with pytest.raises(ValueError) as excinfo:
        build(tmp_path, storage_backend="external")

    message = str(excinfo.value)
    assert "storage_url" in message
    assert "run yourself" in message, "the message does not explain WHY"


def test_the_refusal_happens_before_any_store_is_opened(tmp_path):
    """The point of validating here rather than at resolution: nothing has
    touched the filesystem when the configuration is rejected."""
    store = tmp_path / "qdrant"

    with pytest.raises(ValueError):
        Settings(qdrant_path=str(store), state_db=str(tmp_path / "s.db"),
                 storage_backend="nonsense")

    assert not store.exists(), "a store was created for a configuration we refused"


# --- the contract and the resolver must not drift apart -------------------


def test_the_resolver_accepts_exactly_what_the_contract_declares(tmp_path):
    """Two lists of valid engines is one list too many."""
    from ragtools.storage import resolve_backend

    for backend in sorted(_SUPPORTED_BACKENDS):
        extra = {"storage_url": "http://127.0.0.1:6333"} if backend != "embedded" else {}
        settings = build(tmp_path, storage_backend=backend, **extra)
        resolved = resolve_backend(settings)      # must not raise
        assert resolved.mode == backend


def test_the_router_accepts_exactly_what_the_contract_declares(tmp_path):
    from ragtools.collection_router import CollectionRouter

    class _Registry:
        def collection_for(self, project_id):      # pragma: no cover
            return f"proj_{project_id}"

        def all_collections(self):
            return []

    for strategy in sorted(_SUPPORTED_STRATEGIES):
        settings = build(tmp_path, collection_strategy=strategy)
        router = CollectionRouter(settings, registry=_Registry())
        assert router.strategy == strategy


# --- the engine must be findable where it actually ships ------------------


def test_the_engine_is_found_beside_the_running_executable(tmp_path, monkeypatch):
    r"""Where the installer puts it, not where the finder used to look.

    `_candidate_dirs` searched `_get_app_dir()` under a comment reading "the
    installer ships it here". It does not: `app_dir()` is the DATA directory
    (`%LOCALAPPDATA%\RAGTools`) and the bundle installs to the PROGRAM
    directory (`%LOCALAPPDATA%\Programs\RAGTools`). So the engine shipped
    correctly and was invisible — a packaged upgrade adopted `embedded` while a
    perfectly good `qdrant.exe` sat next to the binary doing the looking.

    Caught by a packaged CI run reporting `storage_backend: embedded` when the
    bundle-contract job had already proven the engine was in the bundle. Two
    green checks, one wrong conclusion between them.
    """
    from ragtools.service.managed_qdrant import _binary_name, find_qdrant_binary

    program = tmp_path / "Programs" / "RAGTools"
    (program / "bin").mkdir(parents=True)
    engine = program / "bin" / _binary_name()
    engine.write_bytes(b"MZ" + b"\0" * 64)

    data = tmp_path / "RAGTools"
    data.mkdir(parents=True)

    monkeypatch.setattr("sys.executable", str(program / "rag.exe"))
    found = find_qdrant_binary(build(tmp_path, data_dir=str(data)))

    assert found, "the packaged engine was not found beside the executable"
    assert Path(found).resolve() == engine.resolve()


def test_the_data_directory_is_still_searched(tmp_path, monkeypatch):
    """A future first-run download lands there; both locations must work."""
    from ragtools.service.managed_qdrant import _binary_name, find_qdrant_binary

    data = tmp_path / "RAGTools"
    (data / "bin").mkdir(parents=True)
    engine = data / "bin" / _binary_name()
    engine.write_bytes(b"MZ" + b"\0" * 64)

    elsewhere = tmp_path / "Programs" / "RAGTools"
    elsewhere.mkdir(parents=True)
    monkeypatch.setattr("sys.executable", str(elsewhere / "rag.exe"))

    found = find_qdrant_binary(build(tmp_path, data_dir=str(data)))

    assert found and Path(found).resolve() == engine.resolve()
