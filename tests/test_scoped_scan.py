"""A project-scoped scan walks that project only.

Field-observed on 2026-10-24: ``_scan_files(pid)`` walked all 26 configured
projects and then filtered. One project held ~70,000 link-loop entries, so
every watcher tick and every ``run_index`` for a 200-file project took ~5
minutes, holding the index mutex the whole time. Three queued jobs looked stuck
and a one-line edit to another project waited behind them.

Scoping must not change WHICH files come back: nested-path ownership (a parent
excludes its child project's files) still needs every project's path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ragtools.config import ProjectConfig
from ragtools.indexing import scanner
from ragtools.indexing.scanner import scan_configured_projects
from ragtools.service.owner import QdrantOwner


def _write(path: Path, text: str = "# note\n\nbody\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path):
    """parent/ contains child/ (a nested project); sibling/ is separate."""
    _write(tmp_path / "parent" / "a.md")
    _write(tmp_path / "parent" / "child" / "b.md")
    _write(tmp_path / "sibling" / "c.md")
    return [
        ProjectConfig(id="parent", path=str(tmp_path / "parent"), mode="general"),
        ProjectConfig(id="child", path=str(tmp_path / "parent" / "child"), mode="general"),
        ProjectConfig(id="sibling", path=str(tmp_path / "sibling"), mode="general"),
    ]


@pytest.mark.parametrize("pid", ["parent", "child", "sibling"])
def test_scoped_scan_returns_exactly_the_unscoped_rows(tree, pid):
    everything = scan_configured_projects(tree)
    expected = sorted(fp for p, fp in everything if p == pid)

    scoped = scan_configured_projects(tree, only=pid)

    assert {p for p, _ in scoped} <= {pid}
    assert sorted(fp for _, fp in scoped) == expected
    assert expected, "fixture should give every project at least one file"


def test_scoped_scan_does_not_walk_other_projects(tree, monkeypatch):
    walked: list[Path] = []
    real = scanner.discover_indexable_files

    def recording(root, *args, **kwargs):
        walked.append(Path(root))
        return real(root, *args, **kwargs)

    monkeypatch.setattr(scanner, "discover_indexable_files", recording)

    scan_configured_projects(tree, only="sibling")

    assert walked == [Path(tree[2].path).resolve()]


def test_owner_scan_files_scopes_the_walk(tree, monkeypatch):
    seen = {}

    def fake_scan(projects, **kwargs):
        seen["projects"] = [p.id for p in projects]
        seen["only"] = kwargs.get("only")
        return []

    monkeypatch.setattr("ragtools.service.owner.scan_configured_projects", fake_scan)
    settings = SimpleNamespace(
        projects=tree, enabled_projects=tree, ignore_patterns=[],
        use_ragignore_files=True, secret_allowlist=[], dependencies=[],
    )
    fake_owner = SimpleNamespace(_settings=settings)

    QdrantOwner._scan_files(fake_owner, "child")
    # Every project still goes in (nested-path exclusion), one gets walked.
    assert seen == {"projects": ["parent", "child", "sibling"], "only": "child"}

    QdrantOwner._scan_files(fake_owner)
    assert seen["only"] is None
