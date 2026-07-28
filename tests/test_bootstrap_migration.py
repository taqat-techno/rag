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


def test_an_existing_install_keeps_the_layout_it_already_had(v2_config):
    """Switching layout forces a full re-index of every file, at first boot,
    on a machine the user just upgraded. It is offered, never imposed."""
    from ragtools.bootstrap import ensure_config_current

    ensure_config_current()

    assert read(v2_config)["collection_strategy"] == "shared"


def test_a_config_with_no_projects_adopts_the_v3_layout(tmp_path, monkeypatch):
    """Nothing is indexed, so nothing is re-indexed and the v3 model is free."""
    import ragtools.config as cfg
    from ragtools.bootstrap import ensure_config_current

    path = tmp_path / "config.toml"
    path.write_bytes(tomli_w.dumps({"version": 2}).encode("utf-8"))
    monkeypatch.setattr(cfg, "_find_config_path", lambda: path)

    ensure_config_current()

    assert read(path)["collection_strategy"] == "per_project"


def test_migration_never_changes_the_resolved_behaviour_of_an_existing_install(v2_config):
    """The reason read paths may safely skip migration.

    A reader looking at the unmigrated file must resolve the same backend and
    layout it would resolve afterwards; otherwise "only writers migrate" would
    be a correctness bug rather than a simplification.
    """
    from ragtools.config import Settings

    before = Settings()
    resolved_before = (before.storage_backend, before.collection_strategy)

    from ragtools.bootstrap import ensure_config_current
    ensure_config_current()

    after = Settings()
    assert (after.storage_backend, after.collection_strategy) == resolved_before


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


def test_a_missing_config_is_not_an_error(tmp_path, monkeypatch):
    """A fresh install has no file until something writes one."""
    import ragtools.config as cfg
    from ragtools.bootstrap import ensure_config_current

    monkeypatch.setattr(cfg, "_find_config_path", lambda: tmp_path / "absent.toml")
    result = ensure_config_current()

    assert result.already_current and not result.degraded


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
