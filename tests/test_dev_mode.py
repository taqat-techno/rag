"""Per-project **Mode** — what a project indexes:

    "docs"    = documentation / text / Markdown only (default)
    "code"    = source / config / code only (no docs-only Markdown)
    "general" = both docs and code/config

Replaces the legacy boolean ``index_source_code`` (migrated transparently on
load). Secret-bearing files are ALWAYS excluded regardless of Mode (orthogonal
layer).
"""

import pytest

from ragtools.config import ProjectConfig, mode_indexes


@pytest.fixture(autouse=True)
def _restore_app_singletons():
    """Save/restore the service app singletons so endpoint tests that inject a
    fake owner/settings can't leak into other test modules."""
    from ragtools.service import app as app_module
    o, s = app_module._owner, app_module._settings
    yield
    app_module._owner, app_module._settings = o, s


# --- Data model: default, explicit values, gate ---

def test_new_project_defaults_to_docs_mode():
    p = ProjectConfig(id="x", path="/tmp/x")
    assert p.mode == "docs"  # default is always Docs


def test_mode_explicit_values():
    assert ProjectConfig(id="a", path="/p", mode="docs").mode == "docs"
    assert ProjectConfig(id="b", path="/p", mode="code").mode == "code"
    assert ProjectConfig(id="c", path="/p", mode="general").mode == "general"


def test_mode_indexes_truth_table():
    # docs: only documentation
    assert mode_indexes("docs", True) is True
    assert mode_indexes("docs", False) is False
    # code: only non-documentation
    assert mode_indexes("code", True) is False
    assert mode_indexes("code", False) is True
    # general: everything
    assert mode_indexes("general", True) is True
    assert mode_indexes("general", False) is True


def test_project_indexes_method():
    assert ProjectConfig(id="a", path="/p", mode="docs").indexes(is_documentation=True) is True
    assert ProjectConfig(id="a", path="/p", mode="docs").indexes(is_documentation=False) is False
    assert ProjectConfig(id="b", path="/p", mode="code").indexes(is_documentation=False) is True
    assert ProjectConfig(id="c", path="/p", mode="general").indexes(is_documentation=True) is True


# --- Migration: legacy index_source_code -> mode ---

def test_legacy_index_source_code_true_migrates_to_general():
    p = ProjectConfig(id="a", path="/p", index_source_code=True)  # type: ignore[call-arg]
    assert p.mode == "general"


def test_legacy_index_source_code_false_migrates_to_docs():
    p = ProjectConfig(id="a", path="/p", index_source_code=False)  # type: ignore[call-arg]
    assert p.mode == "docs"


def test_legacy_index_source_code_none_migrates_to_docs():
    p = ProjectConfig(id="a", path="/p", index_source_code=None)  # type: ignore[call-arg]
    assert p.mode == "docs"


def test_legacy_index_source_code_absent_defaults_docs():
    p = ProjectConfig(id="a", path="/p")
    assert p.mode == "docs"


def test_explicit_mode_wins_over_legacy_key():
    # If both are present, the explicit mode is authoritative (legacy ignored).
    p = ProjectConfig(id="a", path="/p", mode="code", index_source_code=True)  # type: ignore[call-arg]
    assert p.mode == "code"


# --- Persistence round-trip ---

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


def test_save_persists_mode(tmp_path, monkeypatch):
    _, data = _save_and_load(
        tmp_path, monkeypatch,
        [
            ProjectConfig(id="code", path=str(tmp_path), mode="code"),
            ProjectConfig(id="docsonly", path=str(tmp_path), mode="docs"),
            ProjectConfig(id="both", path=str(tmp_path), mode="general"),
        ],
    )
    by_id = {e["id"]: e for e in data["projects"]}
    assert by_id["code"]["mode"] == "code"
    assert by_id["docsonly"]["mode"] == "docs"
    assert by_id["both"]["mode"] == "general"
    # The legacy key must NOT be written anymore.
    assert "index_source_code" not in by_id["code"]


def test_loaded_project_roundtrips_mode(tmp_path, monkeypatch):
    from ragtools.config import Settings
    config_path, _ = _save_and_load(
        tmp_path, monkeypatch,
        [
            ProjectConfig(id="code", path=str(tmp_path), mode="code"),
            ProjectConfig(id="docs", path=str(tmp_path)),  # default docs
        ],
    )
    monkeypatch.setenv("RAG_CONFIG_PATH", str(config_path))
    by_id = {p.id: p for p in Settings().projects}
    assert by_id["code"].mode == "code"
    assert by_id["docs"].mode == "docs"


def test_legacy_config_file_migrates_on_load(tmp_path, monkeypatch):
    """An OLD config.toml (written by a pre-Mode build) must load without error
    and map index_source_code -> mode. Don't break existing configs."""
    from ragtools.config import Settings
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "version = 2\n\n"
        "[[projects]]\n"
        'id = "legacy_code"\n'
        f'path = "{str(tmp_path).replace(chr(92), "/")}"\n'
        "index_source_code = true\n\n"
        "[[projects]]\n"
        'id = "legacy_docs"\n'
        f'path = "{str(tmp_path).replace(chr(92), "/")}"\n'
        "index_source_code = false\n\n"
        "[[projects]]\n"
        'id = "legacy_absent"\n'
        f'path = "{str(tmp_path).replace(chr(92), "/")}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("RAG_CONFIG_PATH", str(config_path))
    by_id = {p.id: p for p in Settings().projects}
    assert by_id["legacy_code"].mode == "general"   # True  -> general
    assert by_id["legacy_docs"].mode == "docs"      # False -> docs
    assert by_id["legacy_absent"].mode == "docs"    # absent -> docs


# --- Pipeline: scanner per-project Mode + secret orthogonality ---

def _mk(path, files):
    path.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (path / name).write_text(content)


def test_scan_docs_mode_excludes_code(tmp_path):
    from ragtools.indexing.scanner import scan_configured_projects
    d = tmp_path / "docs_proj"
    _mk(d, {"app.py": "x = 1\n", "readme.md": "# A\n"})
    projects = [ProjectConfig(id="D", path=str(d), mode="docs")]
    names = {p.name for _, p in scan_configured_projects(projects)}
    assert "readme.md" in names
    assert "app.py" not in names


def test_scan_code_mode_excludes_docs(tmp_path):
    from ragtools.indexing.scanner import scan_configured_projects
    c = tmp_path / "code_proj"
    _mk(c, {"app.py": "x = 1\n", "readme.md": "# A\n"})
    projects = [ProjectConfig(id="C", path=str(c), mode="code")]
    names = {p.name for _, p in scan_configured_projects(projects)}
    assert "app.py" in names
    assert "readme.md" not in names  # docs-only markdown excluded in code mode


def test_scan_general_mode_includes_both(tmp_path):
    from ragtools.indexing.scanner import scan_configured_projects
    g = tmp_path / "general_proj"
    _mk(g, {"app.py": "x = 1\n", "readme.md": "# A\n"})
    projects = [ProjectConfig(id="G", path=str(g), mode="general")]
    names = {p.name for _, p in scan_configured_projects(projects)}
    assert "app.py" in names
    assert "readme.md" in names


def test_scan_secret_excluded_in_every_mode(tmp_path):
    from ragtools.indexing.scanner import scan_configured_projects
    for mode in ("code", "general"):
        a = tmp_path / f"secret_{mode}"
        _mk(a, {"app.py": "x = 1\n", "credentials.json": "{}\n"})
        projects = [ProjectConfig(id=f"A_{mode}", path=str(a), mode=mode)]
        names = {p.name for _, p in scan_configured_projects(projects)}
        assert "app.py" in names
        assert "credentials.json" not in names  # secret layer always-on


def test_watcher_is_indexable_change_honors_mode(tmp_path):
    from ragtools.watcher.observer import is_indexable_change
    from ragtools.ignore import IgnoreRules
    root = tmp_path.resolve()
    rules = IgnoreRules(content_root=str(root))
    py = str(root / "app.py")
    md = str(root / "doc.md")
    # docs mode: md yes, py no
    assert is_indexable_change(md, rules, root, "docs") is True
    assert is_indexable_change(py, rules, root, "docs") is False
    # code mode: py yes, md no
    assert is_indexable_change(py, rules, root, "code") is True
    assert is_indexable_change(md, rules, root, "code") is False
    # general: both
    assert is_indexable_change(py, rules, root, "general") is True
    assert is_indexable_change(md, rules, root, "general") is True


def test_watcher_deepest_matching_root(tmp_path):
    from ragtools.service.watcher_thread import _deepest_matching_root
    parent = (tmp_path / "parent").resolve()
    child = (parent / "child").resolve()
    roots = [parent, child]
    assert _deepest_matching_root((child / "x.py").resolve(), roots) == child
    assert _deepest_matching_root((parent / "y.py").resolve(), roots) == parent
    assert _deepest_matching_root((tmp_path / "other.py").resolve(), roots) is None


# --- API request models + reindex-on-change (G1) ---

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
    monkeypatch.setattr(pages_mod, "_save_projects_to_toml", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_restart_watcher_if_running", lambda *a, **k: None)
    return routes_mod, app_module._owner


def test_project_create_defaults_mode_docs(monkeypatch, tmp_path):
    from ragtools.service.routes import ProjectCreateRequest
    routes_mod, owner = _routes_env(monkeypatch, [])
    monkeypatch.setattr(routes_mod, "_schedule_auto_index", lambda pid: None)
    routes_mod.project_create(ProjectCreateRequest(id="np", path=str(tmp_path)))
    by_id = {p.id: p for p in owner.captured}
    assert by_id["np"].mode == "docs"


def test_project_create_threads_mode(monkeypatch, tmp_path):
    from ragtools.service.routes import ProjectCreateRequest
    routes_mod, owner = _routes_env(monkeypatch, [])
    monkeypatch.setattr(routes_mod, "_schedule_auto_index", lambda pid: None)
    routes_mod.project_create(ProjectCreateRequest(id="np", path=str(tmp_path), mode="code"))
    by_id = {p.id: p for p in owner.captured}
    assert by_id["np"].mode == "code"


def test_project_update_reindexes_only_on_mode_change(monkeypatch, tmp_path):
    from ragtools.service.routes import ProjectUpdateRequest
    proj = ProjectConfig(id="p", path=str(tmp_path))  # docs
    routes_mod, _ = _routes_env(monkeypatch, [proj])
    reindexed = []
    monkeypatch.setattr(routes_mod, "_schedule_reindex", lambda pid: reindexed.append(pid))

    routes_mod.project_update("p", ProjectUpdateRequest(mode="code"))
    assert proj.mode == "code"
    assert reindexed == ["p"]                 # docs -> code: changed -> reindex

    reindexed.clear()
    routes_mod.project_update("p", ProjectUpdateRequest(mode="code"))
    assert reindexed == []                    # same value: no reindex

    routes_mod.project_update("p", ProjectUpdateRequest(name="renamed"))
    assert reindexed == []                    # field not provided: unchanged
    assert proj.mode == "code"


def test_schedule_reindex_is_delete_aware(monkeypatch):
    # G1: a Mode change must use reindex_project (delete+full), NOT run_full_index
    # (upsert-only) — else narrowing the Mode leaves stale chunks on disk.
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


def test_projects_configured_includes_mode(monkeypatch, tmp_path):
    from ragtools.config import Settings
    from ragtools.service import app as app_module, routes as routes_mod
    app_module._settings = Settings(
        state_db=str(tmp_path / "none.db"),
        projects=[
            ProjectConfig(id="c", path=str(tmp_path), mode="code"),
            ProjectConfig(id="d", path=str(tmp_path)),  # docs
        ],
    )
    by_id = {p["id"]: p for p in routes_mod.projects_configured()["projects"]}
    assert by_id["c"]["mode"] == "code"
    assert by_id["d"]["mode"] == "docs"
    # Legacy keys must be gone from the API contract.
    assert "index_source_code" not in by_id["c"]
    assert "index_source_code_effective" not in by_id["c"]


def test_project_status_endpoint_returns_mode(monkeypatch, tmp_path):
    from ragtools.config import Settings
    from ragtools.service import app as app_module, routes as routes_mod
    app_module._settings = Settings(
        state_db=str(tmp_path / "none.db"),  # absent -> summary defaults, empty breakdown
        projects=[ProjectConfig(id="p", path=str(tmp_path), mode="code")],
    )
    res = routes_mod.project_status_endpoint("p")
    assert res["mode"] == "code"
    assert res["enabled"] is True
    assert "file_types" in res
    assert "last_indexed" in res


# --- Admin UI (add form + edit form + list columns) ---

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


def test_ui_add_threads_mode(monkeypatch, tmp_path):
    pages_mod, owner = _ui_env(monkeypatch, [])
    pages_mod.ui_projects_add(id="np", name="NP", path=str(tmp_path), ignore_patterns="", mode="code")
    by_id = {p.id: p for p in owner.captured}
    assert by_id["np"].mode == "code"


def test_add_form_template_mode_default_is_docs():
    """The add-project form must offer Mode with Docs selected by default and a
    General (not "Generic") option, and must no longer say "Dev mode"."""
    from pathlib import Path
    import ragtools.service.pages as pg
    template = Path(pg.__file__).parent / "templates" / "projects.html"
    html = template.read_text(encoding="utf-8")
    assert 'name="mode"' in html
    assert ">Mode</label>" in html
    assert 'value="docs" selected' in html
    assert ">General" in html
    assert "Generic" not in html
    assert "Dev mode" not in html


def test_ui_save_threads_mode(monkeypatch, tmp_path):
    proj = ProjectConfig(id="p", path=str(tmp_path), mode="code")
    pages_mod, _ = _ui_env(monkeypatch, [proj])
    pages_mod.ui_projects_save("p", name="P", path=str(tmp_path), ignore_patterns="", mode="general")
    assert proj.mode == "general"


def test_ui_edit_form_uses_mode_label_and_preselects(monkeypatch, tmp_path):
    pages_mod, _ = _ui_env(monkeypatch, [ProjectConfig(id="p", path=str(tmp_path), mode="code")])
    html = pages_mod.ui_projects_edit("p")
    assert 'name="mode"' in html
    assert 'value="code" selected' in html     # stored mode pre-selected
    assert ">Mode</label>" in html             # label renamed from "Dev mode"
    assert "Dev mode" not in html
    assert 'colspan="7"' in html               # 7 columns now (Status + Mode split)


def test_projects_list_has_separate_status_and_mode_columns(monkeypatch, tmp_path):
    pages_mod, _ = _ui_env(monkeypatch, [ProjectConfig(id="p", path=str(tmp_path), mode="code")])
    html = pages_mod._render_projects_list()
    # Separate column headers.
    assert "<th>Status</th>" in html
    assert "<th>Mode</th>" in html
    # Status badge is Enabled/Disabled only; Mode badge carries Docs/Code/General.
    assert ">Enabled<" in html
    assert ">Code<" in html
    # Icon-button actions (compact, accessible).
    assert "btn-icon" in html
    assert 'aria-label="Edit p"' in html
    assert 'aria-label="Remove p"' in html


def test_projects_list_disabled_shows_disabled_not_mode(monkeypatch, tmp_path):
    pages_mod, _ = _ui_env(
        monkeypatch, [ProjectConfig(id="p", path=str(tmp_path), enabled=False, mode="general")]
    )
    html = pages_mod._render_projects_list()
    assert ">Disabled<" in html
    assert ">General<" in html  # Mode still shown independently of Status


# --- CLI (rag project mode + add --mode), direct mode ---

def _cli_env(tmp_path, monkeypatch):
    import ragtools.config as cfg
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfg, "get_config_write_path", lambda: config_path)
    monkeypatch.setenv("RAG_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("RAG_STATE_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("RAG_QDRANT_PATH", str(tmp_path / "q"))
    monkeypatch.setenv("RAG_SERVICE_PORT", "21599")  # dead -> CLI uses direct mode
    return config_path


def _load_toml(path):
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    with open(path, "rb") as f:
        return tomllib.load(f)


def test_cli_project_mode_direct(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from ragtools.cli import app as cli_app
    from ragtools.service import pages
    config_path = _cli_env(tmp_path, monkeypatch)
    pages._save_projects_to_toml([ProjectConfig(id="p", path=str(tmp_path))])  # seed (docs)

    res = CliRunner().invoke(cli_app, ["project", "mode", "p", "general"])
    assert res.exit_code == 0, res.stdout
    assert _load_toml(config_path)["projects"][0]["mode"] == "general"

    res = CliRunner().invoke(cli_app, ["project", "mode", "p", "code"])
    assert res.exit_code == 0, res.stdout
    assert _load_toml(config_path)["projects"][0]["mode"] == "code"

    res = CliRunner().invoke(cli_app, ["project", "mode", "p", "docs"])
    assert res.exit_code == 0, res.stdout
    assert _load_toml(config_path)["projects"][0]["mode"] == "docs"


def test_cli_project_mode_rejects_invalid(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from ragtools.cli import app as cli_app
    from ragtools.service import pages
    config_path = _cli_env(tmp_path, monkeypatch)
    pages._save_projects_to_toml([ProjectConfig(id="p", path=str(tmp_path))])
    res = CliRunner().invoke(cli_app, ["project", "mode", "p", "bogus"])
    assert res.exit_code == 2


def test_cli_project_add_mode_direct(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from ragtools.cli import app as cli_app
    config_path = _cli_env(tmp_path, monkeypatch)
    res = CliRunner().invoke(
        cli_app, ["project", "add", "--name", "Code Proj", "--path", str(tmp_path), "--mode", "code"]
    )
    assert res.exit_code == 0, res.stdout
    assert _load_toml(config_path)["projects"][0]["mode"] == "code"


def test_cli_project_add_defaults_docs(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from ragtools.cli import app as cli_app
    config_path = _cli_env(tmp_path, monkeypatch)
    res = CliRunner().invoke(
        cli_app, ["project", "add", "--name", "Doc Proj", "--path", str(tmp_path)]
    )
    assert res.exit_code == 0, res.stdout
    assert _load_toml(config_path)["projects"][0]["mode"] == "docs"


# --- Dedicated route + MCP tool (gated, confirm-token on narrowing) ---

def test_route_mode_sets_and_reindexes(monkeypatch, tmp_path):
    from ragtools.service.routes import ModeRequest
    proj = ProjectConfig(id="p", path=str(tmp_path))  # docs
    routes_mod, _ = _routes_env(monkeypatch, [proj])
    reindexed = []
    monkeypatch.setattr(routes_mod, "_schedule_reindex", lambda pid: reindexed.append(pid))
    res = routes_mod.project_set_mode("p", ModeRequest(mode="code"))
    assert proj.mode == "code"
    assert res["mode"] == "code"
    assert res["reindex_scheduled"] is True
    assert reindexed == ["p"]


def test_mcp_set_mode_general_proxies(monkeypatch):
    import types
    from ragtools.integration import mcp_server as mcp
    monkeypatch.setattr(mcp, "_ops_state", types.SimpleNamespace(mode="proxy"))
    monkeypatch.setattr(mcp, "_cooldown_guard", lambda name: None)
    captured = {}
    monkeypatch.setattr(mcp, "proxy_post",
                        lambda state, path, json=None: captured.update(path=path, json=json) or {"ok": True})
    # general is purely additive -> NO confirm token required
    res = mcp.set_project_mode("myproj", "general")
    assert res["ok"] is True
    assert captured["path"] == "/api/projects/myproj/mode"
    assert captured["json"] == {"mode": "general"}


def test_mcp_set_mode_docs_requires_confirm(monkeypatch):
    import types
    from ragtools.integration import mcp_server as mcp
    monkeypatch.setattr(mcp, "_ops_state", types.SimpleNamespace(mode="proxy"))
    monkeypatch.setattr(mcp, "_cooldown_guard", lambda name: None)
    posted = []
    monkeypatch.setattr(mcp, "proxy_post", lambda *a, **k: posted.append(1) or {"ok": True})
    # narrowing to docs WITHOUT confirm_token -> rejected, no proxy call
    res = mcp.set_project_mode("myproj", "docs")
    assert res.get("ok") is not True
    assert posted == []
    # WITH confirm_token -> proceeds
    captured = {}
    monkeypatch.setattr(mcp, "proxy_post",
                        lambda state, path, json=None: captured.update(path=path, json=json) or {"ok": True})
    res = mcp.set_project_mode("myproj", "docs", confirm_token="myproj")
    assert res["ok"] is True
    assert captured["json"] == {"mode": "docs"}


def test_mcp_set_mode_code_requires_confirm(monkeypatch):
    import types
    from ragtools.integration import mcp_server as mcp
    monkeypatch.setattr(mcp, "_ops_state", types.SimpleNamespace(mode="proxy"))
    monkeypatch.setattr(mcp, "_cooldown_guard", lambda name: None)
    posted = []
    monkeypatch.setattr(mcp, "proxy_post", lambda *a, **k: posted.append(1) or {"ok": True})
    res = mcp.set_project_mode("myproj", "code")  # narrowing -> needs confirm
    assert res.get("ok") is not True
    assert posted == []


def test_mcp_set_mode_rejects_invalid(monkeypatch):
    import types
    from ragtools.integration import mcp_server as mcp
    monkeypatch.setattr(mcp, "_ops_state", types.SimpleNamespace(mode="proxy"))
    monkeypatch.setattr(mcp, "_cooldown_guard", lambda name: None)
    posted = []
    monkeypatch.setattr(mcp, "proxy_post", lambda *a, **k: posted.append(1) or {"ok": True})
    res = mcp.set_project_mode("myproj", "bogus", confirm_token="myproj")
    assert res.get("ok") is not True
    assert posted == []


def test_mcp_set_mode_is_gated_default_on():
    from ragtools.config import Settings
    assert Settings().mcp_tools.get("set_project_mode") is True


# --- Docs-mode code search warning ---

class _FakeSearcher:
    """Minimal Searcher stand-in: dev_search only needs ``.settings`` and a
    ``.search`` that returns a list. No Qdrant required."""
    def __init__(self, settings):
        self.settings = settings
    def search(self, **kwargs):
        return []


def test_dev_search_warns_for_docs_mode_project(tmp_path):
    from ragtools.config import Settings
    from ragtools.retrieval.dev_pipeline import dev_search
    settings = Settings(projects=[ProjectConfig(id="d", path=str(tmp_path), mode="docs")])
    outcome = dev_search(_FakeSearcher(settings), "add a new API endpoint", project_id="d")
    assert any("Docs mode; source code is not indexed" in w for w in outcome.warnings)


def test_dev_search_no_warning_for_code_or_general(tmp_path):
    from ragtools.config import Settings
    from ragtools.retrieval.dev_pipeline import dev_search
    for mode in ("code", "general"):
        settings = Settings(projects=[ProjectConfig(id="x", path=str(tmp_path), mode=mode)])
        outcome = dev_search(_FakeSearcher(settings), "add a new API endpoint", project_id="x")
        assert outcome.warnings == []


def test_format_dev_context_prepends_warning():
    from ragtools.retrieval.formatter import format_dev_context
    warn = "Project 'd' is in Docs mode; source code is not indexed."
    out = format_dev_context([], "add endpoint", triggers=["add API"], warnings=[warn])
    assert warn in out
    # Warning appears at the very top, before the no-matches block.
    assert out.index(warn) < out.index("no matches")
