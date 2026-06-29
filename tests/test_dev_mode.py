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
