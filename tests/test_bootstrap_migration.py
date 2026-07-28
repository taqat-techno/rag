"""The migration must actually run, in the real application, in the right order.

`migrate_config` was correct and fully unit-tested from the day it shipped, and
never once executed outside a test: every production reference was a re-export.
So the property these tests pin is not "the function works" — that was already
proven and was not enough. It is that **booting the product through its own
entry points changes the file on disk**, and that it does so before anything
resolves storage.

A test that calls `migrate_config()` directly cannot fail the way the product
failed. These call the seam, and one of them reads the entry points' source to
assert the ordering that a runtime test cannot observe.
"""

from __future__ import annotations

import ast
import sys
import threading
from pathlib import Path

import pytest

from ragtools.config import CONFIG_VERSION


tomllib = pytest.importorskip(
    "tomllib" if sys.version_info >= (3, 11) else "tomli")
tomli_w = pytest.importorskip("tomli_w")

SRC = Path(__file__).resolve().parents[1] / "src" / "ragtools"


@pytest.fixture(autouse=True)
def _clear_memo():
    """The seam memoises per process; tests must not inherit each other's."""
    import ragtools.bootstrap as bootstrap

    bootstrap._MEMO = None
    yield
    bootstrap._MEMO = None


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Point the INDEX state at a sandbox, per test.

    Migration reads the index state to work out what a re-index would have to
    rebuild. With a default `state_db` that read reaches the developer's real
    `data/index_state.db`, finds their actual projects, and opens a relayout
    plan in their repository — which then makes `rag selfcheck` on that machine
    report a pending re-index that does not exist.

    Scoped to this file rather than set globally: exporting `RAG_DATA_DIR` for
    the whole session marks `data_dir` as explicitly set, which defeats the
    `_anchor_data_dir` validator and broke 29 unrelated tests.
    """
    monkeypatch.setenv("RAG_STATE_DB", str(tmp_path / "isolated_state.db"))
    monkeypatch.setenv("RAG_QDRANT_PATH", str(tmp_path / "isolated_qdrant"))


@pytest.fixture
def v2_config(tmp_path, monkeypatch):
    """A realistic v2 file, with the seam pointed at it."""
    import ragtools.config as cfg

    path = tmp_path / "config.toml"
    path.write_bytes(tomli_w.dumps({
        "version": 2,
        "projects": [{"id": "p1", "path": str(tmp_path), "mode": "docs"}],
    }).encode("utf-8"))
    monkeypatch.setattr(cfg, "_find_config_path", lambda: path)
    return path


def read(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


# --- the property that was missing entirely --------------------------------


def test_the_seam_actually_rewrites_the_file(v2_config):
    from ragtools.bootstrap import ensure_config_current

    result = ensure_config_current()

    assert result.migrated, result.describe()
    after = read(v2_config)
    assert after["version"] == CONFIG_VERSION
    assert "storage_backend" in after
    assert "collection_strategy" in after


def test_running_it_twice_changes_nothing_the_second_time(v2_config):
    from ragtools.bootstrap import ensure_config_current

    ensure_config_current()
    first = v2_config.read_bytes()
    second = ensure_config_current()

    assert second.already_current
    assert not second.migrated
    assert v2_config.read_bytes() == first


def test_a_config_with_nothing_to_do_is_not_rewritten(tmp_path, monkeypatch):
    import ragtools.config as cfg
    from ragtools.bootstrap import ensure_config_current

    path = tmp_path / "config.toml"
    path.write_bytes(tomli_w.dumps({
        "version": CONFIG_VERSION, "storage_backend": "embedded",
        "collection_strategy": "shared", "projects": [],
    }).encode("utf-8"))
    monkeypatch.setattr(cfg, "_find_config_path", lambda: path)
    before = path.read_bytes()

    assert ensure_config_current().already_current
    assert path.read_bytes() == before


def test_projects_survive_the_migration(v2_config):
    from ragtools.bootstrap import ensure_config_current

    ensure_config_current()

    assert [p["id"] for p in read(v2_config)["projects"]] == ["p1"]


# --- the product decision, pinned ------------------------------------------


def test_a_legacy_config_with_no_storage_keys_adopts_the_v3_architecture(v2_config):
    """A missing key is not a decision.

    A v2 config has no `collection_strategy` because the key did not exist when
    it was written — not because anyone chose the value it now implies. Treating
    that absence as "the user wants shared" preserves a legacy architecture
    nobody asked for and leaves the scale problem that prompted the upgrade
    exactly where it was, discoverable only by finding and running
    `rag storage strategy` by hand.
    """
    from ragtools.bootstrap import ensure_config_current

    ensure_config_current()

    after = read(v2_config)
    assert after["collection_strategy"] == "per_project"
    assert after["storage_backend"], "the engine was left unstated"


def test_the_migration_records_which_values_it_chose(v2_config):
    """Absence is readable exactly ONCE.

    After migration the keys exist, and a later run cannot tell a value it wrote
    from one the user chose. Provenance has to be recorded at the moment the
    distinction is still visible, or it is lost with the write that erases it.
    """
    from ragtools.bootstrap import ensure_config_current

    ensure_config_current()

    record = read(v2_config).get("migration") or {}
    assert set(record.get("adopted") or []) == {"storage_backend", "collection_strategy"}
    assert record.get("from_version") == 2


def test_an_explicit_choice_is_preserved_even_when_it_is_the_legacy_one(tmp_path, monkeypatch):
    """`embedded` and `shared` chosen deliberately are decisions, not defaults.

    The rule cuts both ways: if absence means "migrate me", then presence must
    mean "leave me alone" — otherwise the product overrides users who read the
    documentation and configured what they wanted.
    """
    import ragtools.config as cfg
    from ragtools.bootstrap import ensure_config_current

    path = tmp_path / "config.toml"
    path.write_bytes(tomli_w.dumps({
        "version": 2,
        "storage_backend": "embedded",
        "collection_strategy": "shared",
        "projects": [{"id": "p1", "path": str(tmp_path), "mode": "docs"}],
    }).encode("utf-8"))
    monkeypatch.setattr(cfg, "_find_config_path", lambda: path)

    ensure_config_current()

    after = read(path)
    assert after["storage_backend"] == "embedded"
    assert after["collection_strategy"] == "shared"
    assert "migration" not in after, "an explicit config was recorded as adopted"


def test_an_external_backend_keeps_its_address_and_credentials(tmp_path, monkeypatch):
    """Never overwrite what points at a server the user runs.

    A migration that rewrote `storage_url` would disconnect an installation from
    its own database, and one that dropped `storage_api_key` would do it while
    looking like a permissions problem.
    """
    import ragtools.config as cfg
    from ragtools.bootstrap import ensure_config_current

    path = tmp_path / "config.toml"
    path.write_bytes(tomli_w.dumps({
        "version": 2,
        "storage_backend": "external",
        "storage_url": "http://qdrant.internal:6333",
        "storage_api_key": "a-real-secret",
        "collection_strategy": "shared",
        "projects": [],
    }).encode("utf-8"))
    monkeypatch.setattr(cfg, "_find_config_path", lambda: path)

    ensure_config_current()

    after = read(path)
    assert after["storage_backend"] == "external"
    assert after["storage_url"] == "http://qdrant.internal:6333"
    assert after["storage_api_key"] == "a-real-secret"
    assert after["collection_strategy"] == "shared"


def test_a_partially_explicit_config_only_adopts_what_is_missing(tmp_path, monkeypatch):
    """Each key is judged on its own. Setting one does not consent for the other."""
    import ragtools.config as cfg
    from ragtools.bootstrap import ensure_config_current

    path = tmp_path / "config.toml"
    path.write_bytes(tomli_w.dumps({
        "version": 2,
        "collection_strategy": "shared",     # explicit
        "projects": [{"id": "p1", "path": str(tmp_path), "mode": "docs"}],
    }).encode("utf-8"))                       # storage_backend absent
    monkeypatch.setattr(cfg, "_find_config_path", lambda: path)

    ensure_config_current()

    after = read(path)
    assert after["collection_strategy"] == "shared", "an explicit layout was overridden"
    assert after["storage_backend"], "the absent engine was not adopted"
    assert (after.get("migration") or {}).get("adopted") == ["storage_backend"]


def test_a_config_with_no_projects_adopts_the_v3_layout(tmp_path, monkeypatch):
    """Nothing is indexed, so nothing is re-indexed and the v3 model is free."""
    import ragtools.config as cfg
    from ragtools.bootstrap import ensure_config_current

    path = tmp_path / "config.toml"
    path.write_bytes(tomli_w.dumps({"version": 2}).encode("utf-8"))
    monkeypatch.setattr(cfg, "_find_config_path", lambda: path)

    ensure_config_current()

    assert read(path)["collection_strategy"] == "per_project"


def test_migration_now_changes_the_declared_architecture(v2_config):
    """Migration is deliberately NO LONGER behaviour-preserving.

    That was the justification for "only writers migrate": a reader looking at
    an unmigrated file resolved exactly what it would resolve afterwards, so
    skipping migration on read paths cost nothing. Adopting the recommended
    architecture for legacy defaults removes that guarantee by design — the
    whole point is that the machine ends up somewhere different.

    Two consequences follow, and they are tracked below rather than left
    implicit.
    """
    from ragtools.bootstrap import ensure_config_current
    from ragtools.config import Settings

    before = Settings()
    assert before.collection_strategy == "shared", "fixture is not a legacy config"

    ensure_config_current()

    after = Settings()
    assert after.collection_strategy == "per_project", (
        "the legacy default was not migrated to the recommended architecture"
    )


def test_the_product_refuses_to_look_ready_while_the_index_is_rebuilding(v2_config):
    """How the target/effective split is resolved, now that it is settled.

    Migration switches the architecture immediately and the index for it does
    not exist yet. The answer is NOT to keep serving the old layout — dual-read
    is explicitly not a requirement — but to stop claiming readiness until the
    rebuild finishes. An empty result from a half-built index is
    indistinguishable from "your query matched nothing", which tells the user
    their content is gone in the one form they have no reason to doubt.
    """
    from ragtools.bootstrap import ensure_config_current
    from ragtools.config import Settings
    from ragtools.upgrade import relayout
    from ragtools.upgrade.relayout import Inventory, MigrationInProgress, Unit

    ensure_config_current()
    # An explicit state_db: a bare Settings() resolves it relative to the
    # working directory, so this test wrote a real `data/relayout.db` into the
    # repository — after which `rag selfcheck` on the developer's machine
    # reported a pending re-index that did not exist.
    settings = Settings(state_db=str(v2_config.parent / "state.db"),
                        qdrant_path=str(v2_config.parent / "qdrant"))

    relayout.begin(settings,
                   Inventory(units=[Unit(relayout.KIND_PROJECT, "p1", 500)]),
                   from_backend="embedded", to_backend="managed",
                   from_strategy="shared", to_strategy="per_project")

    with pytest.raises(MigrationInProgress):
        relayout.guard_ready(settings)


# --- failure is loud, not fatal --------------------------------------------


def test_an_unwritable_config_degrades_instead_of_raising(v2_config, monkeypatch):
    import ragtools.bootstrap as bootstrap

    def _refuse(*a, **k):
        raise PermissionError("read-only volume")

    monkeypatch.setattr("ragtools.atomicio.atomic_write_bytes", _refuse)
    result = bootstrap.ensure_config_current()

    assert result.degraded and not result.migrated
    assert "could not write" in (result.error or "")
    assert read(v2_config)["version"] == 2, "a failed write must not corrupt the file"


def test_an_unreadable_config_degrades_instead_of_raising(tmp_path, monkeypatch):
    import ragtools.config as cfg
    from ragtools.bootstrap import ensure_config_current

    path = tmp_path / "config.toml"
    path.write_text("this is not valid toml [[[", encoding="utf-8")
    monkeypatch.setattr(cfg, "_find_config_path", lambda: path)

    result = ensure_config_current()

    assert result.degraded
    assert not result.migrated


def test_a_clean_install_gets_the_canonical_v3_config(tmp_path, monkeypatch):
    """A missing config is a clean installation, not a no-op.

    This used to return "already current" and write nothing, leaving the file to
    whichever writer ran first — and those writers stamp a version and nothing
    else. A fresh install therefore ended up with `version = 3` and no storage
    keys, running on code defaults its own configuration did not state, so two
    installs of the same release could behave differently for reasons nothing on
    disk explained.
    """
    import ragtools.config as cfg
    from ragtools.bootstrap import ensure_config_current

    target = tmp_path / "absent.toml"
    monkeypatch.setattr(cfg, "_find_config_path", lambda: None)
    monkeypatch.setattr(cfg, "get_config_write_path", lambda: target)

    result = ensure_config_current()

    assert not result.degraded, result.describe()
    assert target.is_file(), "no configuration was created for a clean install"
    document = read(target)
    assert document["version"] == CONFIG_VERSION
    assert document["storage_backend"], "the canonical config omits the engine"
    assert document["collection_strategy"], "the canonical config omits the layout"


def test_a_clean_install_adopts_the_v3_layout(tmp_path, monkeypatch):
    """Nothing is indexed yet, so the v3 layout costs no re-index at all."""
    import ragtools.config as cfg
    from ragtools.bootstrap import ensure_config_current

    target = tmp_path / "fresh.toml"
    monkeypatch.setattr(cfg, "_find_config_path", lambda: None)
    monkeypatch.setattr(cfg, "get_config_write_path", lambda: target)

    ensure_config_current()

    assert read(target)["collection_strategy"] == "per_project"


def test_a_read_only_caller_creates_nothing(tmp_path, monkeypatch):
    """A read path that materialises the user's configuration as a side effect
    is the same class of defect as one that rewrites it."""
    import ragtools.config as cfg
    from ragtools.bootstrap import ensure_config_current

    target = tmp_path / "absent.toml"
    monkeypatch.setattr(cfg, "_find_config_path", lambda: None)
    monkeypatch.setattr(cfg, "get_config_write_path", lambda: target)

    result = ensure_config_current(allow_write=False)

    assert not target.exists()
    assert not result.migrated and not result.degraded


def test_a_read_only_caller_leaves_the_file_alone(v2_config):
    from ragtools.bootstrap import ensure_config_current

    result = ensure_config_current(allow_write=False)

    assert not result.migrated
    assert read(v2_config)["version"] == 2


# --- concurrency ------------------------------------------------------------


def test_concurrent_bootstraps_cannot_corrupt_the_file(v2_config):
    """The service, the tray, the MCP server and a CLI command can all start
    within the same second of a login. Atomic writes protect one writer from
    interruption, not two writers from each other."""
    from ragtools.bootstrap import ensure_config_current

    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def go():
        try:
            barrier.wait(timeout=10)
            ensure_config_current()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    after = read(v2_config)          # must still parse
    assert after["version"] == CONFIG_VERSION
    assert [p["id"] for p in after["projects"]] == ["p1"]


def test_a_held_lock_does_not_wedge_the_caller(v2_config, monkeypatch):
    """Waiting forever on another process's lock would turn a config nicety
    into a startup hang."""
    import ragtools.bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "LOCK_WAIT_SECONDS", 0.2)
    lock = bootstrap._ConfigLock(v2_config)
    assert lock.acquire()
    try:
        result = bootstrap.ensure_config_current()
    finally:
        lock.release()

    assert not result.degraded, "a busy lock is not an error"
    assert not result.migrated


def test_an_abandoned_lock_is_reclaimed(v2_config, monkeypatch):
    """A process that died mid-migration must not block every future boot."""
    import os
    import time

    import ragtools.bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "LOCK_STALE_SECONDS", 0.01)
    stale = bootstrap._ConfigLock(v2_config).path
    stale.write_text("", encoding="utf-8")
    os.utime(stale, (time.time() - 3600, time.time() - 3600))

    result = bootstrap.ensure_config_current()

    assert result.migrated, result.describe()


# --- ordering, which no runtime test can observe ---------------------------


def _first_call_line(node: ast.AST, name: str) -> int | None:
    """Line of the first call to `name()` inside `node`, or None.

    Matched on the syntax tree rather than on text. The text version of this
    helper reported the wrong order the moment a COMMENT above the seam
    mentioned `Settings()` — the search found the explanation instead of the
    call, and failed a file that was already correct. Only code has an order.
    """
    lines = [n.lineno for n in ast.walk(node)
             if isinstance(n, ast.Call)
             and ((isinstance(n.func, ast.Name) and n.func.id == name)
                  or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]
    return min(lines) if lines else None


def _function(source: str, name: str) -> ast.AST | None:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _calls_before(source: str, function: str, needle: str, marker: str) -> bool:
    node = _function(source, function)
    if node is None:
        return False
    first, second = _first_call_line(node, needle), _first_call_line(node, marker)
    return first is not None and second is not None and first < second


@pytest.mark.parametrize("module, function", [
    ("service/run.py", "main"),
    ("cli.py", "_get_settings"),
])
def test_the_seam_runs_before_settings_are_read(module, function):
    source = (SRC / module).read_text(encoding="utf-8")

    assert _calls_before(source, function, "ensure_config_current_once", "Settings"), (
        f"{module}:{function} reads Settings() before migrating the config"
    )


def test_the_service_migrates_before_the_owner_is_constructed():
    """`QdrantOwner.__init__` opens the store and creates collections, so a
    migration after it would already be looking at the previous layout."""
    source = (SRC / "service" / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    seam = _first_call_line(tree, "ensure_config_current_once")
    owner = _first_call_line(tree, "QdrantOwner")
    assert seam is not None, "the service never migrates"
    assert owner is not None, "QdrantOwner is no longer constructed here"
    assert seam < owner, (
        f"the owner is constructed (line {owner}) before the configuration is "
        f"migrated (line {seam})"
    )


def test_every_entry_point_that_writes_config_runs_the_seam():
    """A new entry point that loads Settings and skips this is the original
    defect returning by a different door."""
    for module, function in (("service/run.py", "main"), ("cli.py", "_get_settings")):
        source = (SRC / module).read_text(encoding="utf-8")
        assert "ensure_config_current_once" in source, f"{module} does not migrate"


# --- `rag upgrade` must be the same code, not a second implementation -----


def test_rag_upgrade_calls_the_startup_seam(monkeypatch):
    """One implementation, or they drift until one of them is wrong.

    A separate manual upgrade path is exactly how the automatic and manual
    routes end up disagreeing — and this project has already shipped two
    releases in which the migration existed and nothing invoked it. The command
    must therefore call `ensure_config_current`, not re-derive the work.
    """
    import ragtools.bootstrap as bootstrap
    from typer.testing import CliRunner

    from ragtools.cli import app

    seen = {}
    real = bootstrap.ensure_config_current

    def watched(*a, **k):
        seen["called"] = True
        seen["allow_write"] = k.get("allow_write", True)
        return real(*a, **k)

    monkeypatch.setattr(bootstrap, "ensure_config_current", watched)
    CliRunner().invoke(app, ["upgrade"])

    assert seen.get("called"), "rag upgrade does not use the startup seam"
    assert seen["allow_write"] is True


def test_rag_upgrade_dry_run_writes_nothing(tmp_path, monkeypatch):
    import ragtools.bootstrap as bootstrap
    import ragtools.config as cfg
    from typer.testing import CliRunner

    from ragtools.cli import app

    config = tmp_path / "config.toml"
    config.write_bytes(tomli_w.dumps({
        "version": 2, "projects": [{"id": "p", "path": str(tmp_path), "mode": "docs"}],
    }).encode("utf-8"))
    monkeypatch.setattr(cfg, "_find_config_path", lambda: config)
    monkeypatch.setattr(cfg, "get_config_write_path", lambda: config)
    bootstrap._MEMO = None

    before = config.read_bytes()
    result = CliRunner().invoke(app, ["upgrade", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert config.read_bytes() == before, "a dry run modified the configuration"
    assert "Dry run" in result.output


def test_rag_upgrade_reports_the_architecture_it_left_behind(tmp_path, monkeypatch):
    """"Migrated" without saying to WHAT is the kind of success message that
    hides a surprise."""
    import ragtools.bootstrap as bootstrap
    import ragtools.config as cfg
    from typer.testing import CliRunner

    from ragtools.cli import app

    config = tmp_path / "config.toml"
    config.write_bytes(tomli_w.dumps({
        "version": 2, "projects": [{"id": "p", "path": str(tmp_path), "mode": "docs"}],
    }).encode("utf-8"))
    monkeypatch.setattr(cfg, "_find_config_path", lambda: config)
    monkeypatch.setattr(cfg, "get_config_write_path", lambda: config)
    bootstrap._MEMO = None

    result = CliRunner().invoke(app, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert "engine:" in result.output
    assert "layout:" in result.output


def test_a_failed_upgrade_exits_non_zero(tmp_path, monkeypatch):
    """A caller scripting this must be able to tell success from failure."""
    import ragtools.bootstrap as bootstrap
    import ragtools.config as cfg
    from typer.testing import CliRunner

    from ragtools.cli import app

    config = tmp_path / "config.toml"
    config.write_bytes(tomli_w.dumps({"version": 2}).encode("utf-8"))
    monkeypatch.setattr(cfg, "_find_config_path", lambda: config)
    monkeypatch.setattr(cfg, "get_config_write_path", lambda: config)
    bootstrap._MEMO = None

    def refuse(*a, **k):
        raise PermissionError("read-only volume")

    monkeypatch.setattr("ragtools.atomicio.atomic_write_bytes", refuse)
    result = CliRunner().invoke(app, ["upgrade"])

    assert result.exit_code == 1, result.output
