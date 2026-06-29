"""Per-project "dev mode" — a per-project override of the global index_source_code.

None  = inherit the global Settings.index_source_code
True  = force code+config indexing for this project
False = force docs-only for this project

Secret-bearing files are ALWAYS excluded regardless (orthogonal layer).
"""

import pytest

from ragtools.config import ProjectConfig


@pytest.fixture(autouse=True)
def _restore_app_singletons():
    """Save/restore the service app singletons so endpoint tests that inject a
    fake owner/settings can't leak into other test modules."""
    from ragtools.service import app as app_module
    o, s = app_module._owner, app_module._settings
    yield
    app_module._owner, app_module._settings = o, s


# --- Phase 1: data model + resolver ---

def test_index_source_code_defaults_to_none_inherit():
    p = ProjectConfig(id="x", path="/tmp/x")
    assert p.index_source_code is None  # inherit the global


def test_index_source_code_explicit_values():
    assert ProjectConfig(id="a", path="/p", index_source_code=True).index_source_code is True
    assert ProjectConfig(id="b", path="/p", index_source_code=False).index_source_code is False


def test_resolve_index_code_truth_table():
    # None -> inherit whatever the global is
    assert ProjectConfig(id="a", path="/p").resolve_index_code(True) is True
    assert ProjectConfig(id="a", path="/p").resolve_index_code(False) is False
    # explicit value overrides the global in both directions
    assert ProjectConfig(id="b", path="/p", index_source_code=True).resolve_index_code(False) is True
    assert ProjectConfig(id="c", path="/p", index_source_code=False).resolve_index_code(True) is False


# --- Phase 2: persistence round-trip ---

def _save_and_load(tmp_path, monkeypatch, projects):
    import ragtools.config as cfg
    from ragtools.service import pages
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfg, "get_config_write_path", lambda: config_path)
    pages._save_projects_to_toml(projects)
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    with open(config_path, "rb") as f:
        return config_path, tomllib.load(f)


def test_save_omits_none_index_source_code_without_crashing(tmp_path, monkeypatch):
    # tomli_w cannot serialize None; a None override must be omitted from the TOML.
    _, data = _save_and_load(
        tmp_path, monkeypatch,
        [ProjectConfig(id="docs", path=str(tmp_path), index_source_code=None)],
    )
    assert "index_source_code" not in data["projects"][0]


def test_save_persists_explicit_index_source_code(tmp_path, monkeypatch):
    _, data = _save_and_load(
        tmp_path, monkeypatch,
        [
            ProjectConfig(id="code", path=str(tmp_path), index_source_code=True),
            ProjectConfig(id="docsonly", path=str(tmp_path), index_source_code=False),
        ],
    )
    by_id = {e["id"]: e for e in data["projects"]}
    assert by_id["code"]["index_source_code"] is True
    assert by_id["docsonly"]["index_source_code"] is False


def test_loaded_project_roundtrips_through_settings(tmp_path, monkeypatch):
    from ragtools.config import Settings
    config_path, _ = _save_and_load(
        tmp_path, monkeypatch,
        [
            ProjectConfig(id="code", path=str(tmp_path), index_source_code=True),
            ProjectConfig(id="inherit", path=str(tmp_path)),  # None
        ],
    )
    monkeypatch.setenv("RAG_CONFIG_PATH", str(config_path))
    by_id = {p.id: p for p in Settings().projects}
    assert by_id["code"].index_source_code is True
    assert by_id["inherit"].index_source_code is None


# --- Phase 3: pipeline threading (scanner per-project + watcher deepest-match) ---

def _mk(path, files):
    path.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (path / name).write_text(content)


def test_scan_resolves_index_code_per_project(tmp_path):
    from ragtools.indexing.scanner import scan_configured_projects
    a = tmp_path / "code_proj"
    _mk(a, {"app.py": "x = 1\n", "readme.md": "# A\n"})
    b = tmp_path / "docs_proj"
    _mk(b, {"util.py": "y = 2\n", "notes.md": "# B\n"})
    projects = [
        ProjectConfig(id="A", path=str(a), index_source_code=True),  # force code
        ProjectConfig(id="B", path=str(b)),                          # inherit (None)
    ]
    # global OFF -> A forces code, B inherits docs-only
    names = {(pid, p.name) for pid, p in scan_configured_projects(projects, include_code=False)}
    assert ("A", "app.py") in names
    assert ("A", "readme.md") in names
    assert ("B", "util.py") not in names
    assert ("B", "notes.md") in names


def test_scan_global_on_project_forced_docs(tmp_path):
    from ragtools.indexing.scanner import scan_configured_projects
    b = tmp_path / "forced_docs"
    _mk(b, {"util.py": "y = 2\n", "notes.md": "# B\n"})
    projects = [ProjectConfig(id="B", path=str(b), index_source_code=False)]
    # global ON, but this project is forced docs-only
    names = {(pid, p.name) for pid, p in scan_configured_projects(projects, include_code=True)}
    assert ("B", "util.py") not in names
    assert ("B", "notes.md") in names


def test_scan_secret_excluded_even_in_code_mode(tmp_path):
    from ragtools.indexing.scanner import scan_configured_projects
    a = tmp_path / "code_secret"
    _mk(a, {"app.py": "x = 1\n", "credentials.json": "{}\n"})
    projects = [ProjectConfig(id="A", path=str(a), index_source_code=True)]
    names = {p.name for _, p in scan_configured_projects(projects, include_code=False)}
    assert "app.py" in names
    assert "credentials.json" not in names  # secret layer is orthogonal & always-on


def test_watcher_deepest_matching_root(tmp_path):
    from pathlib import Path
    from ragtools.service.watcher_thread import _deepest_matching_root
    parent = (tmp_path / "parent").resolve()
    child = (parent / "child").resolve()
    roots = [parent, child]
    # a file inside the nested child must attribute to the DEEPEST root (child)
    assert _deepest_matching_root((child / "x.py").resolve(), roots) == child
    # a file directly under parent attributes to parent
    assert _deepest_matching_root((parent / "y.py").resolve(), roots) == parent
    # outside both roots -> None
    assert _deepest_matching_root((tmp_path / "other.py").resolve(), roots) is None


# --- Phase 4: API request models + reindex-on-change (G1) ---

def test_dev_mode_enum_mapping():
    from ragtools.service.routes import _stored_index_code, _index_code_enum
    assert _stored_index_code("code") is True
    assert _stored_index_code("docs") is False
    assert _stored_index_code("inherit") is None
    assert _stored_index_code(None) is None
    assert _index_code_enum(True) == "code"
    assert _index_code_enum(False) == "docs"
    assert _index_code_enum(None) == "inherit"


def _routes_env(monkeypatch, projects):
    from ragtools.config import Settings
    from ragtools.service import app as app_module, routes as routes_mod

    class _FakeOwner:
        captured = None
        def update_projects(self, projs):
            _FakeOwner.captured = list(projs)

    from ragtools.service import pages as pages_mod
    app_module._owner = _FakeOwner()
    app_module._settings = Settings(projects=projects)
    # _save_projects_to_toml is imported inside the route fns -> patch it at source.
    monkeypatch.setattr(pages_mod, "_save_projects_to_toml", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_restart_watcher_if_running", lambda *a, **k: None)
    return routes_mod, app_module._owner


def test_project_update_reindexes_only_on_effective_change(monkeypatch, tmp_path):
    from ragtools.service.routes import ProjectUpdateRequest
    proj = ProjectConfig(id="p", path=str(tmp_path))  # None -> inherit (global False -> docs)
    routes_mod, _ = _routes_env(monkeypatch, [proj])
    reindexed = []
    monkeypatch.setattr(routes_mod, "_schedule_reindex", lambda pid: reindexed.append(pid))

    routes_mod.project_update("p", ProjectUpdateRequest(index_source_code="code"))
    assert proj.index_source_code is True
    assert reindexed == ["p"]                 # docs -> code: effective changed -> reindex

    reindexed.clear()
    routes_mod.project_update("p", ProjectUpdateRequest(index_source_code="code"))
    assert reindexed == []                    # same value: no reindex

    routes_mod.project_update("p", ProjectUpdateRequest(name="renamed"))
    assert reindexed == []                    # field not provided: unchanged
    assert proj.index_source_code is True


def test_project_create_threads_dev_mode(monkeypatch, tmp_path):
    from ragtools.service.routes import ProjectCreateRequest
    routes_mod, owner = _routes_env(monkeypatch, [])
    monkeypatch.setattr(routes_mod, "_schedule_auto_index", lambda pid: None)
    routes_mod.project_create(
        ProjectCreateRequest(id="np", path=str(tmp_path), index_source_code="code")
    )
    by_id = {p.id: p for p in owner.captured}
    assert by_id["np"].index_source_code is True


def test_schedule_reindex_is_delete_aware(monkeypatch):
    # G1: a dev-mode flip must use reindex_project (delete+full), NOT run_full_index
    # (upsert-only) — else disabling dev mode leaves stale code chunks on disk.
    import threading
    from ragtools.service import app as app_module, routes as routes_mod
    import ragtools.service.activity as activity_mod

    calls = {"reindex_project": 0, "run_full_index": 0}

    class _FakeOwner:
        def reindex_project(self, project_id):
            calls["reindex_project"] += 1
            return {}
        def run_full_index(self, project_id=None):
            calls["run_full_index"] += 1
            return {}

    app_module._owner = _FakeOwner()
    monkeypatch.setattr(activity_mod, "log_activity", lambda *a, **k: None)

    class _ImmediateTimer:
        def __init__(self, delay, fn):
            self._fn = fn
            self.daemon = True
        def start(self):
            self._fn()

    monkeypatch.setattr(threading, "Timer", _ImmediateTimer)
    routes_mod._schedule_reindex("p")
    assert calls["reindex_project"] == 1
    assert calls["run_full_index"] == 0


def test_projects_configured_includes_dev_mode(monkeypatch, tmp_path):
    from ragtools.config import Settings
    from ragtools.service import app as app_module, routes as routes_mod
    app_module._settings = Settings(
        state_db=str(tmp_path / "none.db"),
        projects=[
            ProjectConfig(id="c", path=str(tmp_path), index_source_code=True),
            ProjectConfig(id="i", path=str(tmp_path)),  # inherit
        ],
    )
    by_id = {p["id"]: p for p in routes_mod.projects_configured()["projects"]}
    assert by_id["c"]["index_source_code"] == "code"
    assert by_id["c"]["index_source_code_effective"] is True
    assert by_id["i"]["index_source_code"] == "inherit"
    assert by_id["i"]["index_source_code_effective"] is False


# --- Phase 5: admin UI (add form + edit form + list badge) ---

def _ui_env(monkeypatch, projects):
    from ragtools.config import Settings
    from ragtools.service import app as app_module, pages as pages_mod, routes as routes_mod

    class _FakeOwner:
        captured = None
        def update_projects(self, projs):
            _FakeOwner.captured = list(projs)

    app_module._owner = _FakeOwner()
    app_module._settings = Settings(projects=projects)
    monkeypatch.setattr(pages_mod, "_save_projects_to_toml", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_restart_watcher_if_running", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_schedule_auto_index", lambda pid: None)
    monkeypatch.setattr(routes_mod, "_schedule_reindex", lambda pid: None)
    return pages_mod, _FakeOwner


def test_ui_add_threads_dev_mode(monkeypatch, tmp_path):
    pages_mod, owner = _ui_env(monkeypatch, [])
    pages_mod.ui_projects_add(
        id="np", name="NP", path=str(tmp_path), ignore_patterns="", index_source_code="code"
    )
    by_id = {p.id: p for p in owner.captured}
    assert by_id["np"].index_source_code is True


def test_ui_save_threads_dev_mode(monkeypatch, tmp_path):
    proj = ProjectConfig(id="p", path=str(tmp_path))
    pages_mod, _ = _ui_env(monkeypatch, [proj])
    pages_mod.ui_projects_save(
        "p", name="P", path=str(tmp_path), ignore_patterns="", index_source_code="docs"
    )
    assert proj.index_source_code is False


def test_ui_edit_form_preselects_stored_mode(monkeypatch, tmp_path):
    pages_mod, _ = _ui_env(monkeypatch, [ProjectConfig(id="p", path=str(tmp_path), index_source_code=True)])
    html = pages_mod.ui_projects_edit("p")
    assert 'name="index_source_code"' in html
    assert 'value="code" selected' in html  # stored True -> "code" pre-selected


def test_projects_list_shows_mode_badge(monkeypatch, tmp_path):
    pages_mod, _ = _ui_env(monkeypatch, [ProjectConfig(id="p", path=str(tmp_path), index_source_code=True)])
    html = pages_mod._render_projects_list()
    assert ">Code<" in html   # effective dev-mode badge on the row (code project)
