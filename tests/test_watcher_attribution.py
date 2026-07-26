"""S1 / A6 — watcher attributes changes to the DEEPEST project (B25).

v2.7.0's watcher run loop attributed a changed path to the FIRST matching
project in config order, then re-indexed that project. For a nested child
project configured after its parent, a change inside the child was attributed
to the PARENT — whose (possibly docs-only) mode then filtered the child's code
edits out, and the scanner excludes child-owned files from the parent's scan,
so the change was silently never indexed. This pins deepest-root attribution,
matching ``_accept`` and the scanner's child-path ownership.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S1/A6 -> G1)
"""

from ragtools.service.watcher_thread import _affected_projects


def test_change_in_nested_child_attributes_to_child(tmp_path):
    parent = tmp_path / "parent"
    child = parent / "sub" / "child"
    child.mkdir(parents=True)
    # Parent is inserted FIRST — first-match (the old bug) would pick "parent".
    project_map = {parent.resolve(): "parent", child.resolve(): "child"}

    affected = _affected_projects([str(child / "app.py")], project_map)
    assert affected == {"child"}


def test_change_in_parent_outside_child_attributes_to_parent(tmp_path):
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    project_map = {parent.resolve(): "parent", child.resolve(): "child"}

    affected = _affected_projects([str(parent / "top.py")], project_map)
    assert affected == {"parent"}


def test_unmatched_path_attributes_to_nothing(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    project_map = {parent.resolve(): "parent"}

    affected = _affected_projects([str(tmp_path / "elsewhere" / "x.py")], project_map)
    assert affected == set()


def test_multiple_changes_union_of_deepest(tmp_path):
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    project_map = {parent.resolve(): "parent", child.resolve(): "child"}

    affected = _affected_projects(
        [str(child / "a.py"), str(parent / "b.py")], project_map
    )
    assert affected == {"child", "parent"}
