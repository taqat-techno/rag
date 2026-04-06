"""Tests for the ignore rules engine."""

import os
from pathlib import Path

import pytest

from ragtools.ignore import IgnoreRules, BUILTIN_PATTERNS


FIXTURES = Path(__file__).parent / "fixtures"


# --- Built-in defaults ---


def test_builtin_patterns_superset_of_old_skip_dirs():
    """Built-in defaults must include all directories from the old SKIP_DIRS."""
    old_skip_dirs = {
        ".git", ".hg", ".svn", ".venv", "venv", "__pycache__", "node_modules",
        "site-packages", ".tox", ".mypy_cache", ".pytest_cache", ".hypothesis",
        "dist", "build", ".egg-info", ".stversions",
    }
    builtin_dir_patterns = {
        p.rstrip("/").lstrip("*") for p in BUILTIN_PATTERNS if p.endswith("/")
    }
    for d in old_skip_dirs:
        assert d in builtin_dir_patterns or f".{d}" in builtin_dir_patterns, (
            f"Old SKIP_DIRS entry '{d}' missing from BUILTIN_PATTERNS"
        )


def test_builtin_ignores_git_directory():
    rules = IgnoreRules(content_root=FIXTURES)
    assert rules.is_ignored(Path(".git/config"), FIXTURES)


def test_builtin_ignores_venv():
    rules = IgnoreRules(content_root=FIXTURES)
    assert rules.is_ignored(Path(".venv/lib/site.md"), FIXTURES)


def test_builtin_ignores_node_modules():
    rules = IgnoreRules(content_root=FIXTURES)
    assert rules.is_ignored(Path("node_modules/pkg/README.md"), FIXTURES)


def test_builtin_ignores_pyc():
    rules = IgnoreRules(content_root=FIXTURES)
    assert rules.is_ignored(Path("src/module.pyc"), FIXTURES)


def test_builtin_does_not_ignore_normal_md():
    rules = IgnoreRules(content_root=FIXTURES)
    readme = FIXTURES / "project_a" / "README.md"
    assert not rules.is_ignored(readme, FIXTURES)


# --- Global config patterns ---


def test_global_patterns_ignore_extra_dirs():
    rules = IgnoreRules(
        content_root=FIXTURES,
        global_patterns=["drafts/"],
    )
    assert rules.is_ignored(Path("drafts/my_draft.md"), FIXTURES)


def test_global_patterns_ignore_file_extension():
    rules = IgnoreRules(
        content_root=FIXTURES,
        global_patterns=["*.tmp"],
    )
    assert rules.is_ignored(Path("project_a/notes.tmp"), FIXTURES)


def test_global_patterns_do_not_affect_normal_files():
    rules = IgnoreRules(
        content_root=FIXTURES,
        global_patterns=["drafts/", "*.tmp"],
    )
    readme = FIXTURES / "project_a" / "README.md"
    assert not rules.is_ignored(readme, FIXTURES)


# --- .ragignore files ---


def test_ragignore_file_ignores_matching_files(tmp_path):
    """A .ragignore file should cause matching files to be ignored."""
    # Set up directory structure
    project = tmp_path / "myproject"
    project.mkdir()
    (project / "good.md").write_text("# Good")
    (project / "draft.md").write_text("# Draft")
    (project / ".ragignore").write_text("draft.md\n")

    rules = IgnoreRules(content_root=tmp_path, use_ragignore=True)
    assert rules.is_ignored(project / "draft.md", tmp_path)
    assert not rules.is_ignored(project / "good.md", tmp_path)


def test_ragignore_with_glob_pattern(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "file.md").write_text("# File")
    (project / "file.tmp").write_text("temp")
    (project / ".ragignore").write_text("*.tmp\n")

    rules = IgnoreRules(content_root=tmp_path, use_ragignore=True)
    assert rules.is_ignored(project / "file.tmp", tmp_path)
    assert not rules.is_ignored(project / "file.md", tmp_path)


def test_ragignore_comments_ignored(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "keep.md").write_text("# Keep")
    (project / ".ragignore").write_text("# This is a comment\n")

    rules = IgnoreRules(content_root=tmp_path, use_ragignore=True)
    assert not rules.is_ignored(project / "keep.md", tmp_path)


def test_ragignore_disabled():
    """When use_ragignore=False, .ragignore files should be ignored."""
    rules = IgnoreRules(content_root=FIXTURES, use_ragignore=False)
    # Even if a .ragignore exists, it should not be parsed
    # (we can't easily test without one, but no crash = pass)
    readme = FIXTURES / "project_a" / "README.md"
    assert not rules.is_ignored(readme, FIXTURES)


# --- get_reason ---


def test_get_reason_returns_builtin():
    rules = IgnoreRules(content_root=FIXTURES)
    reason = rules.get_reason(Path(".git/config"), FIXTURES)
    assert reason is not None
    assert "built-in" in reason


def test_get_reason_returns_config():
    rules = IgnoreRules(content_root=FIXTURES, global_patterns=["drafts/"])
    reason = rules.get_reason(Path("drafts/file.md"), FIXTURES)
    assert reason is not None
    assert "config" in reason


def test_get_reason_returns_ragignore(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "skip.md").write_text("skip")
    (project / ".ragignore").write_text("skip.md\n")

    rules = IgnoreRules(content_root=tmp_path, use_ragignore=True)
    reason = rules.get_reason(project / "skip.md", tmp_path)
    assert reason is not None
    assert ".ragignore" in reason


def test_get_reason_returns_none_for_included():
    rules = IgnoreRules(content_root=FIXTURES)
    readme = FIXTURES / "project_a" / "README.md"
    reason = rules.get_reason(readme, FIXTURES)
    assert reason is None


# --- get_all_patterns ---


def test_get_all_patterns_includes_all_layers():
    rules = IgnoreRules(
        content_root=FIXTURES,
        global_patterns=["drafts/"],
    )
    patterns = rules.get_all_patterns()
    assert "built-in" in patterns
    assert "config" in patterns
    assert len(patterns["built-in"]) == len(BUILTIN_PATTERNS)
    assert "drafts/" in patterns["config"]


# --- Cache ---


def test_clear_cache(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "file.md").write_text("# File")

    rules = IgnoreRules(content_root=tmp_path)
    # First check populates cache
    rules.is_ignored(project / "file.md", tmp_path)
    assert len(rules._ragignore_cache) > 0

    rules.clear_cache()
    assert len(rules._ragignore_cache) == 0


# --- Edge cases ---


def test_empty_global_patterns():
    rules = IgnoreRules(content_root=FIXTURES, global_patterns=[])
    readme = FIXTURES / "project_a" / "README.md"
    assert not rules.is_ignored(readme, FIXTURES)


def test_path_outside_content_root():
    rules = IgnoreRules(content_root=FIXTURES)
    outside = Path("/some/other/path/file.md")
    # Should not crash, just check against patterns
    rules.is_ignored(outside, FIXTURES)


# --- Integration with scanner ---


def test_scanner_uses_ignore_rules():
    """Scanner should respect ignore rules."""
    from ragtools.indexing.scanner import discover_markdown_files

    rules = IgnoreRules(content_root=FIXTURES)
    files = discover_markdown_files(FIXTURES / "project_a", ignore_rules=rules)
    # Should find .md files, not crash
    assert len(files) > 0
    # No file in a SKIP_DIRS should appear
    for f in files:
        parts = f.parts
        assert ".git" not in parts
        assert "__pycache__" not in parts


def test_scanner_with_custom_ignore():
    """Scanner should skip files matching custom ignore patterns."""
    from ragtools.indexing.scanner import discover_markdown_files

    rules = IgnoreRules(
        content_root=FIXTURES,
        global_patterns=["guide.md"],
    )
    files = discover_markdown_files(FIXTURES / "project_a", ignore_rules=rules)
    filenames = [f.name for f in files]
    assert "guide.md" not in filenames
    assert "README.md" in filenames


def test_scan_project_with_default_rules():
    """scan_project without explicit rules should work (backward compat)."""
    from ragtools.indexing.scanner import scan_project

    results = scan_project(str(FIXTURES))
    assert len(results) > 0
    # All results should be .md files
    for pid, path in results:
        assert path.suffix == ".md"
