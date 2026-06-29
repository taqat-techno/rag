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
