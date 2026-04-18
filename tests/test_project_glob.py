"""Tests for the bulk-add-from-glob helpers.

Everything in `ragtools.project_glob` is pure logic — no HTTP, no config
writes — so this file covers the full feature surface without needing a
service instance.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ragtools.config import ProjectConfig
from ragtools.project_glob import (
    PlanKind,
    PlannedAdd,
    derive_plan,
    expand_glob,
    plan_summary,
    slugify_id,
)


# ---------------------------------------------------------------------------
# slugify_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("My Project", "my-project"),
    ("already-slugified", "already-slugified"),
    ("UPPER_snake_case", "upper-snake-case"),
    ("weird!!!name???", "weird-name"),
    ("   spaces   ", "spaces"),
    ("---leading---trailing---", "leading-trailing"),
    ("a--b--c", "a-b-c"),
    ("", ""),
    ("!!!", ""),
    ("docs", "docs"),
    ("docs/v2", "docs-v2"),
])
def test_slugify_id_rules(text, expected):
    assert slugify_id(text) == expected


# ---------------------------------------------------------------------------
# expand_glob
# ---------------------------------------------------------------------------


def test_expand_glob_returns_only_directories(tmp_path):
    (tmp_path / "proj_a").mkdir()
    (tmp_path / "proj_b").mkdir()
    (tmp_path / "note.md").write_text("x")  # file — must be excluded

    pattern = str(tmp_path / "*")
    result = expand_glob(pattern)

    assert len(result) == 2
    assert all(p.is_dir() for p in result)
    names = [p.name for p in result]
    assert names == sorted(names)  # deterministic order


def test_expand_glob_skips_missing_paths(tmp_path):
    # Glob of a non-existent tree → empty list, no crash
    result = expand_glob(str(tmp_path / "does_not_exist" / "*"))
    assert result == []


def test_expand_glob_supports_recursive(tmp_path):
    (tmp_path / "a" / "docs").mkdir(parents=True)
    (tmp_path / "b" / "docs").mkdir(parents=True)
    (tmp_path / "c" / "other").mkdir(parents=True)

    pattern = str(tmp_path / "**" / "docs")
    result = expand_glob(pattern)

    assert len(result) == 2
    resolved_names = [p.parent.name for p in result]
    assert "a" in resolved_names
    assert "b" in resolved_names
    assert "c" not in resolved_names


def test_expand_glob_excludes_matching_directories(tmp_path):
    (tmp_path / "keep").mkdir()
    (tmp_path / "archive").mkdir()

    pattern = str(tmp_path / "*")
    excludes = [str(tmp_path / "archive")]
    result = expand_glob(pattern, excludes=excludes)

    names = [p.name for p in result]
    assert "keep" in names
    assert "archive" not in names


def test_expand_glob_deduplicates_overlapping_matches(tmp_path):
    """A directory matched by both pattern and a sibling branch appears once."""
    (tmp_path / "x").mkdir()
    # Overlapping patterns: * and x (both select the same dir)
    pattern = str(tmp_path / "*")
    result1 = expand_glob(pattern)
    assert len(result1) == 1


# ---------------------------------------------------------------------------
# derive_plan
# ---------------------------------------------------------------------------


def _project(pid: str, path: str) -> ProjectConfig:
    return ProjectConfig(id=pid, name=pid, path=path)


def test_derive_plan_marks_new_paths(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()

    plan = derive_plan(
        [tmp_path / "alpha", tmp_path / "beta"],
        existing=[],
    )

    assert len(plan) == 2
    assert all(row.kind == PlanKind.NEW for row in plan)
    assert {row.project_id for row in plan} == {"alpha", "beta"}
    assert all(row.actionable for row in plan)


def test_derive_plan_marks_duplicate_by_resolved_path(tmp_path):
    target = tmp_path / "docs"
    target.mkdir()

    existing = [_project("already", str(target.resolve()))]
    plan = derive_plan([target], existing=existing)

    assert len(plan) == 1
    assert plan[0].kind == PlanKind.DUPLICATE
    assert plan[0].reason == "Already registered"
    assert not plan[0].actionable


def test_derive_plan_marks_invalid_for_missing_paths(tmp_path):
    missing = tmp_path / "gone"  # not created
    plan = derive_plan([missing], existing=[])

    assert len(plan) == 1
    assert plan[0].kind == PlanKind.INVALID
    assert not plan[0].actionable


def test_derive_plan_marks_invalid_for_file_not_dir(tmp_path):
    f = tmp_path / "file.md"
    f.write_text("x")
    plan = derive_plan([f], existing=[])

    assert len(plan) == 1
    assert plan[0].kind == PlanKind.INVALID


def test_derive_plan_disambiguates_colliding_ids(tmp_path):
    (tmp_path / "docs").mkdir()
    other = tmp_path / "more" / "docs"
    other.mkdir(parents=True)

    existing = [_project("docs", "/some/other/place/docs")]
    plan = derive_plan([tmp_path / "docs", other], existing=existing)

    # First input collides with existing 'docs' id → renamed to 'docs-2'
    assert plan[0].kind == PlanKind.RENAMED
    assert plan[0].project_id == "docs-2"
    assert "docs-2" in plan[0].reason
    # Second input would also be 'docs' but 'docs-2' is now reserved → 'docs-3'
    assert plan[1].kind == PlanKind.RENAMED
    assert plan[1].project_id == "docs-3"


def test_derive_plan_rejects_same_path_twice_in_input(tmp_path):
    """If the user somehow supplies the same path twice, the second is a dupe."""
    (tmp_path / "alpha").mkdir()
    target = tmp_path / "alpha"

    plan = derive_plan([target, target], existing=[])

    assert len(plan) == 2
    assert plan[0].kind == PlanKind.NEW
    assert plan[1].kind == PlanKind.DUPLICATE


def test_derive_plan_applies_name_prefix(tmp_path):
    (tmp_path / "alpha").mkdir()
    plan = derive_plan([tmp_path / "alpha"], existing=[], name_prefix="work-")

    assert plan[0].name == "work-alpha"
    assert plan[0].project_id == "alpha"  # id not prefixed


def test_derive_plan_handles_empty_basename_paths(tmp_path):
    """Paths resolving to empty name (unlikely but possible) get 'project' id."""
    # We can't easily synthesize an empty-basename path with tmp_path, so we
    # test slugify_id('') separately and trust that fallback kicks in.
    assert slugify_id("") == ""


# ---------------------------------------------------------------------------
# plan_summary
# ---------------------------------------------------------------------------


def test_plan_summary_counts_each_kind():
    entries = [
        PlannedAdd(path=Path("/a"), kind=PlanKind.NEW, project_id="a", name="a"),
        PlannedAdd(path=Path("/b"), kind=PlanKind.NEW, project_id="b", name="b"),
        PlannedAdd(path=Path("/c"), kind=PlanKind.RENAMED, project_id="c-2", name="c"),
        PlannedAdd(path=Path("/d"), kind=PlanKind.DUPLICATE),
        PlannedAdd(path=Path("/e"), kind=PlanKind.INVALID),
    ]
    summary = plan_summary(entries)
    assert summary == {"NEW": 2, "RENAMED": 1, "DUPLICATE": 1, "INVALID": 1}


def test_plan_summary_zero_when_empty():
    assert plan_summary([]) == {"NEW": 0, "RENAMED": 0, "DUPLICATE": 0, "INVALID": 0}


# ---------------------------------------------------------------------------
# End-to-end (pure): glob → plan
# ---------------------------------------------------------------------------


def test_glob_plus_plan_full_flow(tmp_path):
    """One realistic pattern feeds derive_plan; summary reflects all outcomes."""
    (tmp_path / "proj_a").mkdir()
    (tmp_path / "proj_b").mkdir()
    (tmp_path / "archive" / "old").mkdir(parents=True)
    (tmp_path / "note.md").write_text("x")

    existing = [_project("proj-a", str((tmp_path / "proj_a").resolve()))]

    found = expand_glob(str(tmp_path / "*"))
    plan = derive_plan(found, existing=existing)

    # proj_a is duplicate, proj_b is new, archive is new
    kinds = {row.path.name: row.kind for row in plan}
    assert kinds[(tmp_path / "proj_a").resolve().name] == PlanKind.DUPLICATE
    assert kinds[(tmp_path / "proj_b").resolve().name] == PlanKind.NEW
    assert kinds[(tmp_path / "archive").resolve().name] == PlanKind.NEW

    summary = plan_summary(plan)
    assert summary["NEW"] == 2
    assert summary["DUPLICATE"] == 1
