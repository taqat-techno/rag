"""An ignored directory is never walked, not walked and then filtered.

Field-observed: project ``royal-preps`` ignores ``.claude/`` (built-in) and
``**/worktrees/**`` / ``**/node_modules/**`` (config), yet every ~10 minutes the
service logged "skipped 70359 already-visited (link loop) e.g.
...\\.claude\\worktrees\\visual-parity\\frontend\\node_modules\\.package-lock.json".
The scanner used ``rglob("*")`` and applied the ignore rules per file, AFTER
statting each one — so every incremental scan walked a full worktree checkout
and its ``node_modules``, and a second project hit ``WinError 1920`` inside an
ignored ``.claude/mcp/.venv``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from ragtools.ignore import IgnoreRules
from ragtools.indexing.scanner import SkipLedger, discover_indexable_files

from tests.test_scanner_resilience import make_junction


def _listed_dirs(monkeypatch) -> list[Path]:
    """Record every directory the walk asks the OS to list."""
    listed: list[Path] = []
    real = os.scandir

    def spy(path="."):
        listed.append(Path(os.fsdecode(path)))
        return real(path)

    monkeypatch.setattr(os, "scandir", spy)
    return listed


def _scan(root: Path, patterns: list[str] | None = None):
    ledger = SkipLedger()
    rules = IgnoreRules(content_root=root, global_patterns=patterns or [])
    found = discover_indexable_files(root, ignore_rules=rules, mode="general", ledger=ledger)
    return {p.relative_to(root).as_posix() for p in found}, ledger


def test_an_ignored_directory_is_never_listed(tmp_path, monkeypatch):
    (tmp_path / "keep.md").write_text("# keep", encoding="utf-8")
    deep = tmp_path / "frontend" / "node_modules" / "pkg" / "lib"
    deep.mkdir(parents=True)
    (deep / "index.md").write_text("# vendored", encoding="utf-8")
    listed = _listed_dirs(monkeypatch)

    found, ledger = _scan(tmp_path)

    assert found == {"keep.md"}
    assert not any("node_modules" in p.parts for p in listed), listed
    assert ledger.total == 0


def test_config_worktrees_glob_prunes_the_royal_preps_shape(tmp_path, monkeypatch):
    """The exact field layout: a git worktree under an ignored tree, with its
    own node_modules. Neither may be opened."""
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    wt = tmp_path / "tools" / "worktrees" / "visual-parity" / "frontend" / "node_modules"
    wt.mkdir(parents=True)
    (wt / "a.md").write_text("# a", encoding="utf-8")
    listed = _listed_dirs(monkeypatch)

    found, ledger = _scan(tmp_path, ["**/worktrees/**", "**/node_modules/**"])

    assert found == {"app.py"}
    assert not any("worktrees" in p.parts for p in listed), listed
    assert ledger.total == 0


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_a_link_loop_inside_an_ignored_directory_is_never_entered(tmp_path, monkeypatch):
    """Before the fix this walked the loop to the path-length limit and logged
    every revisited file as an "already-visited (link loop)" skip."""
    (tmp_path / "only.md").write_text("# only", encoding="utf-8")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    if not make_junction(nm / "loop", tmp_path):
        pytest.skip("could not create a junction on this runner")
    listed = _listed_dirs(monkeypatch)

    found, ledger = _scan(tmp_path)

    assert found == {"only.md"}
    assert ledger.loops == 0 and ledger.total == 0, ledger.describe()
    assert all(p != nm and nm not in p.parents for p in listed), listed


def test_a_hardlink_in_an_ignored_directory_does_not_shadow_the_real_file(tmp_path):
    """Identity de-duplication used to run before the ignore check, so an
    ignored copy reached first could drop the real file as a "link loop"."""
    real = tmp_path / "src" / "notes.md"
    real.parent.mkdir()
    real.write_text("# notes", encoding="utf-8")
    shadow_dir = tmp_path / ".claude" / "worktrees" / "wt" / "src"
    shadow_dir.mkdir(parents=True)
    try:
        os.link(real, shadow_dir / "notes.md")
    except OSError:
        pytest.skip("hard links unsupported here")

    found, ledger = _scan(tmp_path)

    assert found == {"src/notes.md"}
    assert ledger.loops == 0


def test_an_unreadable_ignored_directory_is_not_touched(tmp_path, monkeypatch):
    """Field: ``OSError [WinError 1920]`` inside an ignored ``.claude/mcp/.venv``."""
    (tmp_path / "keep.md").write_text("# keep", encoding="utf-8")
    bad = tmp_path / ".claude" / "mcp" / ".venv" / "lib64"
    bad.mkdir(parents=True)
    real = os.scandir

    def refuse(path="."):
        if Path(os.fsdecode(path)) == bad:
            raise OSError(22, "The file cannot be accessed by the system", os.fsdecode(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", refuse)

    found, ledger = _scan(tmp_path)

    assert found == {"keep.md"}
    assert ledger.total == 0, ledger.describe()


def test_an_unstattable_link_in_an_ignored_directory_is_not_touched(tmp_path, monkeypatch):
    """The field shape exactly: ``.venv/lib64`` is a WSL-created symlink that
    Windows lists but refuses to stat (``WinError 1920``). The old walk listed
    the ignored ``.venv``, statted the entry and logged it as unreadable."""
    (tmp_path / "keep.md").write_text("# keep", encoding="utf-8")
    venv = tmp_path / ".claude" / "mcp" / ".venv"
    venv.mkdir(parents=True)
    bad = venv / "lib64"
    bad.write_text("", encoding="utf-8")
    real_stat = Path.stat

    def refuse(self, *args, **kwargs):
        if self == bad:
            exc = OSError(22, "The file cannot be accessed by the system", str(self))
            exc.winerror = 1920  # type: ignore[attr-defined]
            raise exc
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", refuse)

    found, ledger = _scan(tmp_path)

    assert found == {"keep.md"}
    assert ledger.total == 0, ledger.describe()


def test_an_unreadable_directory_that_is_not_ignored_is_recorded(tmp_path, monkeypatch):
    """Control for the test above: the simulated refusal does reach the walk,
    and a real (non-ignored) unreadable directory is counted, not hidden."""
    (tmp_path / "keep.md").write_text("# keep", encoding="utf-8")
    bad = tmp_path / "docs" / "locked"
    bad.mkdir(parents=True)
    real = os.scandir

    def refuse(path="."):
        if Path(os.fsdecode(path)) == bad:
            raise PermissionError(13, "Permission denied", os.fsdecode(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", refuse)

    found, ledger = _scan(tmp_path)

    assert found == {"keep.md"}
    assert ledger.unreadable == 1
    assert "locked" in ledger.describe()


def test_a_negated_ragignore_still_reincludes_its_file(tmp_path):
    """A spec with ``!`` negation is never used to prune: ``drafts/`` plus
    ``!drafts/keep.md`` must still index keep.md, as per-file filtering did."""
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "keep.md").write_text("# keep", encoding="utf-8")
    (drafts / "drop.md").write_text("# drop", encoding="utf-8")
    (tmp_path / ".ragignore").write_text("drafts/\n!drafts/keep.md\n", encoding="utf-8")

    found, _ = _scan(tmp_path)

    assert found == {"drafts/keep.md"}


def test_a_plain_ragignore_directory_is_pruned(tmp_path, monkeypatch):
    drafts = tmp_path / "drafts" / "old"
    drafts.mkdir(parents=True)
    (drafts / "x.md").write_text("# x", encoding="utf-8")
    (tmp_path / "keep.md").write_text("# keep", encoding="utf-8")
    (tmp_path / ".ragignore").write_text("drafts/\n", encoding="utf-8")
    listed = _listed_dirs(monkeypatch)

    found, _ = _scan(tmp_path)

    assert found == {"keep.md"}
    assert not any("drafts" in p.parts for p in listed), listed


@pytest.mark.parametrize("rel,pruned", [
    ("node_modules", True),
    ("a/b/node_modules", True),
    (".claude", True),
    ("src", False),
    ("build_scripts", False),
    ("docs/build", True),
])
def test_prunes_dir_matches_the_builtin_directory_patterns(tmp_path, rel, pruned):
    rules = IgnoreRules(content_root=tmp_path)
    assert rules.prunes_dir(tmp_path / rel, tmp_path) is pruned


def test_the_project_root_is_never_pruned(tmp_path):
    rules = IgnoreRules(content_root=tmp_path, global_patterns=["*"])
    assert rules.prunes_dir(tmp_path, tmp_path) is False
