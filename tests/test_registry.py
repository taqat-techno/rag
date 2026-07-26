"""S6 §11 — persistent project registry: UUID identity + collection lifecycle.

§11.1: a project's identity is an immutable UUID; its collection name derives
from that UUID, not the editable id or the movable path. So a rename or a path
move must leave both UUID and collection untouched (§11.2). §11.3's three verbs —
archive (keep collection, stop watching), remove (drop from registry, orphan the
collection), delete-collection (separate destructive act) — must be three
distinct outcomes. This pins that store; it composes the shared identity core
and the S5 SQLite hardening.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S6 §11 -> G6)
"""

import pytest

from ragtools.identity import InvalidProjectId, project_collection_name
from ragtools.registry import ProjectRegistry


@pytest.fixture
def reg(tmp_path):
    return ProjectRegistry(str(tmp_path / "registry.db"))


# --- add / identity -----------------------------------------------------


def test_add_derives_collection_from_uuid(reg):
    rec = reg.add("royal-preps", path="/w/royal", display_name="Royal Preps")
    assert rec.project_id == "royal-preps"
    assert rec.uuid
    assert rec.collection_name == project_collection_name(rec.uuid)
    assert rec.mode == "docs"  # default


def test_add_rejects_duplicate_id(reg):
    reg.add("a", path="/w/a")
    with pytest.raises(ValueError):
        reg.add("a", path="/w/a2")


def test_add_validates_id(reg):
    with pytest.raises(InvalidProjectId):
        reg.add("Bad Id!", path="/w/x")


def test_same_basename_different_paths_get_distinct_identity(reg):
    a = reg.add("docs-1", path="/w/one/docs")
    b = reg.add("docs-2", path="/w/two/docs")
    assert a.uuid != b.uuid
    assert a.collection_name != b.collection_name


# --- rename / move preserve identity (§11.2) ----------------------------


def test_rename_preserves_uuid_and_collection(reg):
    rec = reg.add("old-id", path="/w/p")
    uuid, coll = rec.uuid, rec.collection_name
    reg.rename("old-id", "new-id")
    assert reg.get("old-id") is None
    moved = reg.get("new-id")
    assert moved.uuid == uuid
    assert moved.collection_name == coll  # collection follows UUID, not the id


def test_move_preserves_uuid_and_collection(reg):
    rec = reg.add("p", path="/w/old")
    reg.move("p", "/w/new")
    moved = reg.get("p")
    assert moved.path == "/w/new"
    assert moved.uuid == rec.uuid
    assert moved.collection_name == rec.collection_name


def test_rename_validates_new_id(reg):
    reg.add("p", path="/w/p")
    with pytest.raises(InvalidProjectId):
        reg.rename("p", "Bad Id")


# --- three lifecycle verbs (§11.3) --------------------------------------


def test_archive_keeps_the_record_and_collection(reg):
    rec = reg.add("p", path="/w/p")
    reg.archive("p")
    assert reg.get("p") is not None                 # still registered
    assert reg.get("p").archived is True
    assert "p" not in [r.project_id for r in reg.list()]        # not in active list
    assert "p" in [r.project_id for r in reg.list(include_archived=True)]


def test_remove_drops_from_registry_and_returns_orphaned_collection(reg):
    rec = reg.add("p", path="/w/p")
    coll = reg.remove("p")
    assert coll == rec.collection_name              # caller learns the orphaned collection
    assert reg.get("p") is None                     # gone from the registry


# --- durability (S5 hardening) ------------------------------------------


def test_registry_persists_across_reopen(tmp_path):
    path = str(tmp_path / "r.db")
    r1 = ProjectRegistry(path)
    rec = r1.add("p", path="/w/p")
    r2 = ProjectRegistry(path)
    got = r2.get("p")
    assert got is not None
    assert got.uuid == rec.uuid
