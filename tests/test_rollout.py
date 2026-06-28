"""Safe-rollout regression tests (P0): source-code indexing is opt-in.

Upgrading must NOT silently start indexing a user's entire codebase. The
default keeps the prior documentation-only behavior; code/config indexing is
enabled explicitly via ``index_source_code=True`` (RAG_INDEX_SOURCE_CODE).
"""

from ragtools.config import Settings
from ragtools.indexing.scanner import scan_project


def test_source_code_indexing_is_opt_in_by_default():
    assert Settings().index_source_code is False


def test_opt_in_can_be_enabled():
    assert Settings(index_source_code=True).index_source_code is True


def test_default_settings_discover_docs_only(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (proj / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (proj / "config.yaml").write_text("key: value\n", encoding="utf-8")

    s = Settings()
    results = scan_project(str(tmp_path), include_code=s.index_source_code)
    names = {p.name for _, p in results}
    assert names == {"guide.md"}, names


def test_opt_in_discovers_code_and_config(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (proj / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    s = Settings(index_source_code=True)
    results = scan_project(str(tmp_path), include_code=s.index_source_code)
    names = {p.name for _, p in results}
    assert names == {"guide.md", "app.py"}, names
