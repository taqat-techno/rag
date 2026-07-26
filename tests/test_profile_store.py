"""S12 — persistent client-profile store (the home authz enforcement reads from).

Tool-list filtering is theatre if the endpoint is open (§24.2): every call must
be re-checked server-side against the caller's stored profile. That requires the
profile to live somewhere durable and load back byte-for-byte — including the
distinction between ``allowed_projects = None`` (owner: ALL) and an empty set
(nothing), which a naive serialization silently destroys. This pins that store;
it composes the S12 :class:`~ragtools.profiles.ClientProfile` core.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S12 -> G12)
"""

import pytest

from ragtools.profiles import ClientProfile
from ragtools.profile_store import ProfileStore


@pytest.fixture
def store(tmp_path):
    return ProfileStore(str(tmp_path / "profiles.db"))


def _profile(**kw) -> ClientProfile:
    base = dict(
        profile_id="roy",
        allowed_projects=frozenset({"royal-preps"}),
        capability_groups=frozenset({"retrieval", "indexing"}),
        tool_overrides={"add_project": True, "delete_collection": False},
        cross_project_policy="none",
        destructive_policy="forbidden",
        display_name="Roy",
        client_type="bot",
    )
    base.update(kw)
    return ClientProfile(**base)


def test_add_and_get_roundtrips_every_field(store):
    p = _profile()
    store.add(p)
    got = store.get("roy")
    assert got == p


def test_owner_all_projects_none_is_preserved(store):
    # None (owner: ALL) must NOT collapse to an empty set (nothing).
    p = _profile(profile_id="owner", allowed_projects=None)
    store.add(p)
    got = store.get("owner")
    assert got.allowed_projects is None


def test_empty_allowed_set_stays_empty(store):
    p = _profile(profile_id="locked", allowed_projects=frozenset())
    store.add(p)
    assert store.get("locked").allowed_projects == frozenset()


def test_get_unknown_returns_none(store):
    assert store.get("nobody") is None


def test_list_returns_all_profiles(store):
    store.add(_profile(profile_id="a"))
    store.add(_profile(profile_id="b"))
    assert {p.profile_id for p in store.list()} == {"a", "b"}


def test_add_existing_id_updates(store):
    store.add(_profile(profile_id="x", display_name="old"))
    store.add(_profile(profile_id="x", display_name="new"))
    assert store.get("x").display_name == "new"
    assert len(store.list()) == 1


def test_remove_deletes(store):
    store.add(_profile(profile_id="x"))
    store.remove("x")
    assert store.get("x") is None


def test_persists_across_reopen(tmp_path):
    path = str(tmp_path / "p.db")
    ProfileStore(path).add(_profile(profile_id="z"))
    assert ProfileStore(path).get("z") is not None
