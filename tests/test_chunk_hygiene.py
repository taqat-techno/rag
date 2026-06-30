"""Indexing hygiene: content-free chunk filtering + conflict/temp artifact exclusion.

Generic (stack-agnostic) cleanups from the weakness analysis:
  P1-A — separator/near-empty chunks must never be embedded or scored.
  P2-D — editor/sync conflict & patch-reject artifacts must never be indexed.
"""

from pathlib import Path

import pytest

from ragtools.chunking.hygiene import is_meaningful_chunk


# --- P1-A: min-content chunk filter -----------------------------------------

@pytest.mark.parametrize("sep", ["---", "***", "___", "===", "- - -", "| --- | --- |", "   \n  \t", ""])
def test_separator_and_empty_chunks_are_not_meaningful(sep):
    assert is_meaningful_chunk(sep, "documentation") is False


def test_real_prose_is_meaningful():
    assert is_meaningful_chunk("This section explains how the webhook works.", "documentation") is True


def test_short_code_chunk_is_kept():
    # Code can be legitimately short; the strict word-count rule is docs-only.
    assert is_meaningful_chunk("x = 1", "code") is True
    assert is_meaningful_chunk("return None", "code") is True


def test_doc_badge_or_lone_link_is_dropped():
    # Single-token / badge-only / lone-link doc fragments are noise.
    assert is_meaningful_chunk("[link](http://x)", "documentation") is False
    assert is_meaningful_chunk("Overview", "documentation") is False  # one word, no body


def test_punctuation_only_dropped_for_all_types():
    for ct in ("documentation", "code", "config", "comment"):
        assert is_meaningful_chunk("-----", ct) is False


def test_chunk_file_drops_separator_section(tmp_path):
    """A markdown section whose body is just '---' must not survive chunking."""
    from ragtools.chunking.dispatch import chunk_file

    md = tmp_path / "doc.md"
    md.write_text(
        "# Title\n\n"
        "Real introductory content describing the module.\n\n"
        "## Divider\n\n"
        "---\n\n"
        "## Body\n\n"
        "More real content with several meaningful words here.\n",
        encoding="utf-8",
    )
    chunks = chunk_file(md, project_id="p", relative_path="doc.md")
    raws = [c.raw_text.strip() for c in chunks]
    assert "---" not in raws
    # The genuine sections survive.
    assert any("introductory content" in r for r in raws)
    assert any("meaningful words" in r for r in raws)


# --- P2-D: conflict / temp artifact exclusion -------------------------------

def test_bare_export_default_dropped():
    assert is_meaningful_chunk("export default AdminImageUpload", "code") is False
    assert is_meaningful_chunk("export default AdminImageUpload;", "code") is False
    assert is_meaningful_chunk("export { foo, bar }", "code") is False
    # a default-export WITH a body is real and kept
    assert is_meaningful_chunk("export default function App() {\n  return null;\n}", "code") is True


def test_comment_banner_dropped():
    assert is_meaningful_chunk("// ── Import ──", "code") is False
    assert is_meaningful_chunk("/* ===== Section ===== */", "comment") is False
    # a substantive comment block is kept
    assert is_meaningful_chunk(
        "// Validates the user input and returns a list of errors", "comment") is True


@pytest.mark.parametrize("name", [
    "design [conflicted].md",
    "notes.sync-conflict-20240101-120000-ABCDEF.md",
    "patch.orig",
    "merge.rej",
])
def test_conflict_and_temp_artifacts_are_ignored(tmp_path, name):
    from ragtools.ignore import IgnoreRules
    rules = IgnoreRules(content_root=tmp_path)
    assert rules.is_ignored(tmp_path / name), name


def test_normal_files_not_ignored_by_conflict_rules(tmp_path):
    from ragtools.ignore import IgnoreRules
    rules = IgnoreRules(content_root=tmp_path)
    assert not rules.is_ignored(tmp_path / "design.md")
    assert not rules.is_ignored(tmp_path / "original_notes.md")  # 'orig' substring, not '.orig'
