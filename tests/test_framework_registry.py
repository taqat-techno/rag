"""S7 §12-13 — framework registry: dedup by build identity, links, removal safety.

A framework corpus is indexed ONCE per build identity and linked to many
projects (§12) — the whole point being that registering the same build twice
reuses the one collection. Links are first-class (§13). Removing a framework
edition still linked by ≥1 project is refused, naming the blockers (§12.5). This
pins that store; it feeds the router's ``links_by_project`` and composes the
shared framework-collection derivation.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S7 §12-13 -> G7)
"""

import pytest

from ragtools.identity import framework_collection_name
from ragtools.registry import FrameworkRegistry, FrameworkLinkError


@pytest.fixture
def fw(tmp_path):
    return FrameworkRegistry(str(tmp_path / "fw.db"))


EE = dict(name="odoo", version="19.0", edition="enterprise", build_id="sha-AAA")
EE_OTHER_BUILD = dict(name="odoo", version="19.0", edition="enterprise", build_id="sha-BBB")


# --- dedup by build identity (§12) --------------------------------------


def test_registering_same_build_reuses_one_collection(fw):
    rec1, created1 = fw.register(**EE, canonical_root="/a")
    rec2, created2 = fw.register(**EE, canonical_root="/b")  # same build, diff root
    assert created1 is True and created2 is False       # second reused
    assert rec1.collection_name == rec2.collection_name
    assert rec1.collection_name == framework_collection_name(
        "odoo", version="19.0", edition="enterprise", build_id="sha-AAA"
    )


def test_different_build_gets_its_own_collection(fw):
    a, _ = fw.register(**EE, canonical_root="/a")
    b, _ = fw.register(**EE_OTHER_BUILD, canonical_root="/b")
    assert a.collection_name != b.collection_name


# --- links feed the router (§13) ----------------------------------------


def test_link_and_lookup_by_project(fw):
    rec, _ = fw.register(**EE, canonical_root="/a")
    fw.link("uuid-proj-a", rec.collection_name, link_kind="detected", detector="odoo-release")
    assert fw.framework_collections_for("uuid-proj-a") == [rec.collection_name]


def test_two_projects_share_one_framework_corpus(fw):
    rec, _ = fw.register(**EE, canonical_root="/a")
    fw.link("uuid-a", rec.collection_name)
    fw.link("uuid-b", rec.collection_name)
    # Freshness fans out to EVERY linked project (§13.3).
    assert set(fw.projects_for(rec.collection_name)) == {"uuid-a", "uuid-b"}


def test_relink_is_idempotent(fw):
    rec, _ = fw.register(**EE, canonical_root="/a")
    fw.link("uuid-a", rec.collection_name)
    fw.link("uuid-a", rec.collection_name)  # no duplicate
    assert fw.framework_collections_for("uuid-a") == [rec.collection_name]


# --- removal safety (§12.5) ---------------------------------------------


def test_remove_refused_while_linked_and_names_blockers(fw):
    rec, _ = fw.register(**EE, canonical_root="/a")
    fw.link("uuid-a", rec.collection_name)
    with pytest.raises(FrameworkLinkError) as ei:
        fw.remove(rec.collection_name)
    assert "uuid-a" in str(ei.value)  # blocking project named


def test_remove_allowed_after_unlink(fw):
    rec, _ = fw.register(**EE, canonical_root="/a")
    fw.link("uuid-a", rec.collection_name)
    fw.unlink("uuid-a", rec.collection_name)
    fw.remove(rec.collection_name)  # no raise
    assert fw.get(rec.collection_name) is None


# --- durability ---------------------------------------------------------


def test_persists_across_reopen(tmp_path):
    path = str(tmp_path / "fw.db")
    r1 = FrameworkRegistry(path)
    rec, _ = r1.register(**EE, canonical_root="/a")
    r1.link("uuid-a", rec.collection_name)
    r2 = FrameworkRegistry(path)
    assert r2.framework_collections_for("uuid-a") == [rec.collection_name]
