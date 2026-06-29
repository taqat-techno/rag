"""Per-project "dev mode" — a per-project override of the global index_source_code.

None  = inherit the global Settings.index_source_code
True  = force code+config indexing for this project
False = force docs-only for this project

Secret-bearing files are ALWAYS excluded regardless (orthogonal layer).
"""

from ragtools.config import ProjectConfig


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
