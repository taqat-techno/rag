"""Tests for the file scanner module."""

from pathlib import Path

from ragtools.indexing.scanner import discover_projects, discover_markdown_files


FIXTURES = Path(__file__).parent / "fixtures"


def test_discover_projects_returns_dict():
    projects = discover_projects(str(FIXTURES))
    assert isinstance(projects, dict)
    for name, path in projects.items():
        assert isinstance(name, str)
        assert isinstance(path, Path)
        assert path.is_dir()


def test_discover_markdown_files_returns_paths():
    files = discover_markdown_files(FIXTURES / "project_a")
    assert isinstance(files, list)
    for f in files:
        assert isinstance(f, Path)
        assert f.suffix == ".md"
        assert f.exists()
