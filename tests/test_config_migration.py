"""Tests for ProjectConfig, v2 config loading, and v1→v2 migration."""

import tempfile
from pathlib import Path

import pytest

from ragtools.config import ProjectConfig, Settings, migrate_v1_to_v2


# --- ProjectConfig ---


def test_project_config_basic():
    p = ProjectConfig(id="wiki", name="Wiki", path="/tmp/wiki")
    assert p.id == "wiki"
    assert p.name == "Wiki"
    assert p.path == "/tmp/wiki"
    assert p.enabled is True
    assert p.ignore_patterns == []


def test_project_config_name_defaults_to_id():
    p = ProjectConfig(id="my-project", path="/tmp/p")
    assert p.name == "my-project"


def test_project_config_with_ignore_patterns():
    p = ProjectConfig(id="p", path="/tmp/p", ignore_patterns=["drafts/", "*.tmp"])
    assert p.ignore_patterns == ["drafts/", "*.tmp"]


def test_project_config_disabled():
    p = ProjectConfig(id="p", path="/tmp/p", enabled=False)
    assert p.enabled is False


# --- Settings with explicit projects ---


def test_settings_has_explicit_projects_empty():
    s = Settings()
    assert s.has_explicit_projects is False


def test_settings_has_explicit_projects_with_projects():
    s = Settings(projects=[ProjectConfig(id="a", path="/tmp/a")])
    assert s.has_explicit_projects is True


def test_settings_enabled_projects():
    s = Settings(projects=[
        ProjectConfig(id="a", path="/tmp/a", enabled=True),
        ProjectConfig(id="b", path="/tmp/b", enabled=False),
        ProjectConfig(id="c", path="/tmp/c", enabled=True),
    ])
    enabled = s.enabled_projects
    assert len(enabled) == 2
    assert [p.id for p in enabled] == ["a", "c"]


# --- Migration v1→v2 ---


def test_migrate_v1_to_v2_discovers_projects(tmp_path):
    # Create subdirectories that look like projects
    (tmp_path / "project_a").mkdir()
    (tmp_path / "project_a" / "readme.md").write_text("# A")
    (tmp_path / "project_b").mkdir()
    (tmp_path / "project_b" / "notes.md").write_text("# B")
    (tmp_path / ".hidden").mkdir()  # Should be skipped

    s = Settings(content_root=str(tmp_path))
    projects = migrate_v1_to_v2(s)

    assert len(projects) == 2
    ids = [p.id for p in projects]
    assert "project_a" in ids
    assert "project_b" in ids
    assert ".hidden" not in ids

    for p in projects:
        assert p.enabled is True
        assert p.name == p.id
        assert Path(p.path).exists()


def test_migrate_v1_to_v2_default_content_root():
    """Default content_root '.' should return empty list."""
    s = Settings(content_root=".")
    projects = migrate_v1_to_v2(s)
    assert projects == []


def test_migrate_v1_to_v2_nonexistent_path():
    """Non-existent content_root should return empty list."""
    s = Settings(content_root="/nonexistent/path/12345")
    projects = migrate_v1_to_v2(s)
    assert projects == []


# --- Scanner v2 functions ---


def test_scan_configured_projects(tmp_path):
    from ragtools.indexing.scanner import scan_configured_projects

    # Create two project directories
    proj_a = tmp_path / "proj_a"
    proj_a.mkdir()
    (proj_a / "readme.md").write_text("# Project A")
    (proj_a / "guide.md").write_text("# Guide")

    proj_b = tmp_path / "proj_b"
    proj_b.mkdir()
    (proj_b / "notes.md").write_text("# Notes")

    projects = [
        ProjectConfig(id="alpha", name="Alpha", path=str(proj_a)),
        ProjectConfig(id="beta", name="Beta", path=str(proj_b)),
    ]

    results = scan_configured_projects(projects)
    assert len(results) == 3  # 2 from alpha + 1 from beta

    pids = [pid for pid, _ in results]
    assert pids.count("alpha") == 2
    assert pids.count("beta") == 1


def test_scan_configured_projects_skips_disabled(tmp_path):
    from ragtools.indexing.scanner import scan_configured_projects

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "file.md").write_text("# File")

    projects = [
        ProjectConfig(id="active", path=str(proj), enabled=True),
        ProjectConfig(id="inactive", path=str(proj), enabled=False),
    ]

    results = scan_configured_projects(projects)
    pids = [pid for pid, _ in results]
    assert "active" in pids
    assert "inactive" not in pids


def test_scan_configured_projects_skips_missing_path(tmp_path):
    from ragtools.indexing.scanner import scan_configured_projects

    projects = [
        ProjectConfig(id="missing", path=str(tmp_path / "nonexistent")),
    ]

    results = scan_configured_projects(projects)
    assert results == []


def test_scan_configured_projects_per_project_ignore(tmp_path):
    from ragtools.indexing.scanner import scan_configured_projects

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "keep.md").write_text("# Keep")
    (proj / "skip.md").write_text("# Skip")

    projects = [
        ProjectConfig(id="filtered", path=str(proj), ignore_patterns=["skip.md"]),
    ]

    results = scan_configured_projects(projects)
    names = [fp.name for _, fp in results]
    assert "keep.md" in names
    assert "skip.md" not in names


def test_get_project_relative_path(tmp_path):
    from ragtools.indexing.scanner import get_project_relative_path

    proj_path = str(tmp_path / "my_project")
    file_path = tmp_path / "my_project" / "docs" / "readme.md"

    # Need the directory to exist for resolve()
    (tmp_path / "my_project" / "docs").mkdir(parents=True)
    file_path.write_text("# Test")

    result = get_project_relative_path(file_path, proj_path, "my-proj")
    assert result == "my-proj/docs/readme.md"


def test_two_projects_no_path_collision(tmp_path):
    """Two projects with same internal file names should not collide."""
    from ragtools.indexing.scanner import scan_configured_projects, get_project_relative_path

    proj_a = tmp_path / "proj_a"
    proj_a.mkdir()
    (proj_a / "readme.md").write_text("# A")

    proj_b = tmp_path / "proj_b"
    proj_b.mkdir()
    (proj_b / "readme.md").write_text("# B")

    projects = [
        ProjectConfig(id="alpha", path=str(proj_a)),
        ProjectConfig(id="beta", path=str(proj_b)),
    ]

    results = scan_configured_projects(projects)
    rel_paths = set()
    for pid, fp in results:
        proj = next(p for p in projects if p.id == pid)
        rel = get_project_relative_path(fp, proj.path, proj.id)
        rel_paths.add(rel)

    # Both should exist with distinct paths
    assert "alpha/readme.md" in rel_paths
    assert "beta/readme.md" in rel_paths
    assert len(rel_paths) == 2  # No collision
