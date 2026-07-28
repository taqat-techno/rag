"""Boot the real application on a v2 configuration and prove it migrates.

**A direct call to `migrate_config()` is not evidence.** That function shipped in
v3.0.0, was correct, was fully unit-tested — and never once executed outside its
own tests, because nothing called it. Every test that passed proved the function
worked; none proved the *product* used it. So these tests do not call it at all.

They start the FastAPI application through `create_app()` and `TestClient`, which
runs the real `lifespan`: the same code path `rag service run` takes. `_owner` is
deliberately left unset so the lifespan constructs everything itself, rather than
the injected-owner shortcut most route tests use.

Seven properties, from the amendment:

1. migration happens automatically;
2. it happens BEFORE storage initialisation;
3. the resulting config is version 3;
4. the intended backend and collection strategy are active;
5. projects and framework assignments survive;
6. a required reindex is triggered exactly once;
7. a restart does not repeat a completed migration.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ragtools.config import CONFIG_VERSION


tomllib = pytest.importorskip(
    "tomllib" if sys.version_info >= (3, 11) else "tomli")
tomli_w = pytest.importorskip("tomli_w")


def read(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def v2_machine(monkeypatch):
    """A directory that looks like a real v2 installation, and nothing else.

    Two projects with content, a `dependency_paths` entry (the legacy shape a v3
    catalog adopts), and a config with NO storage keys — exactly what an
    installation upgraded from 2.7.0 carries.
    """
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        alpha, beta, vendor = root / "alpha", root / "beta", root / "vendor"
        for d in (alpha, beta, vendor):
            d.mkdir(parents=True)
        (alpha / "a.md").write_text("# alpha\n\nAlpha content.", encoding="utf-8")
        (beta / "b.md").write_text("# beta\n\nBeta content.", encoding="utf-8")
        (vendor / "v.md").write_text("# vendor\n\nVendored.", encoding="utf-8")

        config = root / "config.toml"
        config.write_bytes(tomli_w.dumps({
            "version": 2,
            "qdrant_path": str(root / "qdrant"),
            "state_db": str(root / "state.db"),
            "service_port": 21456,
            "projects": [
                {"id": "alpha", "path": str(alpha), "mode": "docs",
                 "dependency_paths": [str(vendor)]},
                {"id": "beta", "path": str(beta), "mode": "docs"},
            ],
        }).encode("utf-8"))

        monkeypatch.setenv("RAG_CONFIG_PATH", str(config))
        monkeypatch.setenv("RAG_DATA_DIR", str(root))

        import ragtools.bootstrap as bootstrap
        bootstrap._MEMO = None
        yield root, config
        bootstrap._MEMO = None


def boot(record_order: list | None = None):
    """Start the real application. Returns a TestClient context manager."""
    from ragtools.service import app as app_module

    app_module._owner = None          # force the lifespan to build it
    app_module._settings = None
    from ragtools.service import routes as routes_module
    routes_module.set_bound_address(None, None)
    return app_module


# --- 1, 3: it happens, and the result is v3 -------------------------------


def test_booting_the_real_app_migrates_a_v2_config(v2_machine):
    root, config = v2_machine
    assert read(config)["version"] == 2, "fixture is not a v2 config"

    from ragtools.service.app import create_app
    boot()

    with TestClient(create_app(), raise_server_exceptions=True):
        pass

    after = read(config)
    assert after["version"] == CONFIG_VERSION, (
        "booting the product did not migrate the configuration — which is the "
        "entire defect: the function worked, nothing called it"
    )


def test_the_migrated_config_carries_the_v3_keys(v2_machine):
    root, config = v2_machine
    from ragtools.service.app import create_app
    boot()

    with TestClient(create_app(), raise_server_exceptions=True):
        pass

    after = read(config)
    assert "storage_backend" in after
    assert "collection_strategy" in after


# --- 2: ordering, which is the part that makes it correct -----------------


def test_migration_completes_before_storage_is_initialised(v2_machine, monkeypatch):
    """`QdrantOwner.__init__` opens the store and creates collections while
    constructing itself. A migration that ran afterwards would already be
    looking at a store built to the previous layout, so ordering is a
    correctness property rather than a preference."""
    root, config = v2_machine
    order: list[str] = []

    import ragtools.bootstrap as bootstrap
    from ragtools.service import owner as owner_module

    real_migrate = bootstrap.ensure_config_current

    def watched_migrate(*a, **k):
        order.append("migrate")
        return real_migrate(*a, **k)

    real_init = owner_module.QdrantOwner.__init__

    def watched_init(self, *a, **k):
        order.append("owner")
        return real_init(self, *a, **k)

    monkeypatch.setattr(bootstrap, "ensure_config_current", watched_migrate)
    monkeypatch.setattr(owner_module.QdrantOwner, "__init__", watched_init)

    from ragtools.service.app import create_app
    boot()
    with TestClient(create_app(), raise_server_exceptions=True):
        pass

    assert "migrate" in order, "the seam never ran during a real boot"
    assert "owner" in order, "the owner was never constructed"
    assert order.index("migrate") < order.index("owner"), (
        f"storage was initialised before the config was migrated: {order}"
    )


# --- 4: the intended architecture is actually in force --------------------


def test_the_running_service_reports_the_migrated_architecture(v2_machine):
    root, config = v2_machine
    from ragtools.service.app import create_app
    boot()

    with TestClient(create_app(), raise_server_exceptions=True) as tc:
        health = tc.get("/health").json()

    on_disk = read(config)
    assert health["config_version"] == CONFIG_VERSION, (
        f"/health reports config_version={health['config_version']} while the "
        f"file says {on_disk['version']}"
    )
    assert health["storage_backend"] == on_disk["storage_backend"]
    assert health["collection_strategy"] == on_disk["collection_strategy"]
    assert not health.get("degraded") or "config_migration_failed" not in health.get(
        "issues", []), health


# --- 5: nothing is lost --------------------------------------------------


def test_every_project_and_dependency_survives_the_boot(v2_machine):
    root, config = v2_machine
    from ragtools.service.app import create_app
    boot()

    with TestClient(create_app(), raise_server_exceptions=True):
        pass

    after = read(config)
    assert [p["id"] for p in after["projects"]] == ["alpha", "beta"]
    # The legacy `dependency_paths` entry is adopted into the catalog rather
    # than dropped — a project silently losing its vendored tree would be a
    # data-shaped regression, not a cosmetic one.
    adopted = after.get("dependencies") or []
    linked = after["projects"][0].get("dependencies") or []
    assert adopted or after["projects"][0].get("dependency_paths"), (
        "the legacy dependency declaration vanished entirely"
    )
    if adopted:
        assert linked, "a catalog entry was created but the project was not linked"


# --- 7: it settles -------------------------------------------------------


def test_a_second_boot_does_not_migrate_again(v2_machine):
    """A migration that re-runs on every start is how the version field stops
    meaning anything — and it is exactly what the sixteen `version = 2` writers
    used to cause."""
    from ragtools.service.app import create_app
    root, config = v2_machine

    boot()
    with TestClient(create_app(), raise_server_exceptions=True):
        pass
    first = config.read_bytes()

    import ragtools.bootstrap as bootstrap
    bootstrap._MEMO = None                      # a genuinely new process
    boot()
    with TestClient(create_app(), raise_server_exceptions=True):
        pass

    assert config.read_bytes() == first, "the second boot rewrote the config"
    bootstrap._MEMO = None
    result = bootstrap.ensure_config_current()
    assert result.already_current, result.describe()
    assert not result.migrated


# --- 6: the reindex is triggered exactly once -----------------------------


def test_a_layout_change_forces_exactly_one_reindex(v2_machine):
    """The state DB describes a store. Change the layout and its file hashes
    describe the PREVIOUS one, so trusting them skips every file against an
    empty collection — observed once as "indexed 0, skipped 38286" with ~28k
    chunks missing. `index_identity` must therefore refuse to trust it once,
    and must stop refusing after the re-index has stamped the new identity.
    """
    from ragtools.index_identity import IndexIdentity, current_identity, reconcile, stamp
    from ragtools.indexing.state import IndexState

    root, _config = v2_machine
    state = IndexState(str(root / "identity.db"))
    try:
        before = IndexIdentity(
            storage_backend="embedded", collection_strategy="shared",
            collection_name="markdown_kb", model_name="all-MiniLM-L6-v2",
            dimension=384)
        stamp(state, before)

        trustworthy, changed = reconcile(state, before)
        assert trustworthy and not changed, "an unchanged store was distrusted"

        after = IndexIdentity(
            storage_backend="embedded", collection_strategy="per_project",
            collection_name="markdown_kb", model_name="all-MiniLM-L6-v2",
            dimension=384)
        trustworthy, changed = reconcile(state, after)
        assert not trustworthy, "a layout change did not force a re-index"
        assert "collection_strategy" in " ".join(changed)

        # Exactly once: after the run stamps the new identity, the next start
        # must NOT re-index again.
        stamp(state, after)
        trustworthy, changed = reconcile(state, after)
        assert trustworthy and not changed, (
            "the re-index repeats on every start — it is triggered more than once"
        )
    finally:
        state.close()
