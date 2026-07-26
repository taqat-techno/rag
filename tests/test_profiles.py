"""S12 — Local Client Profiles + server-side authorization (pure core).

The authorization decision is a property of an authenticated client identity,
computed server-side — never trusting a caller-supplied project, collection, or
claimed name. This pins the pure resolvers: which tools a profile may use
(capability groups + per-tool overrides) and which projects a request resolves
to (fail-closed intersection with the profile's allowed set).

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S12 -> G12)
"""

import pytest

from ragtools.profiles import (
    ClientProfile,
    ScopeDenied,
    authorize_projects,
    effective_tools,
    is_tool_allowed,
)


def _make(**kw) -> ClientProfile:
    defaults = dict(
        profile_id="p",
        allowed_projects=None,  # None = ALL (owner)
        capability_groups=frozenset({"retrieval"}),
        tool_overrides={},
        cross_project_policy="none",
        destructive_policy="forbidden",
    )
    defaults.update(kw)
    return ClientProfile(**defaults)


# --- tool authorization -------------------------------------------------


def test_retrieval_group_grants_search_but_not_admin():
    p = _make(capability_groups=frozenset({"retrieval"}))
    assert is_tool_allowed(p, "search_current_project")
    assert is_tool_allowed(p, "search_knowledge_base")
    assert not is_tool_allowed(p, "add_project")  # needs project_management


def test_multiple_groups_union():
    p = _make(capability_groups=frozenset({"retrieval", "indexing"}))
    assert is_tool_allowed(p, "run_index")
    assert is_tool_allowed(p, "search_current_project")


def test_tool_override_deny_wins_over_group():
    p = _make(
        capability_groups=frozenset({"retrieval"}),
        tool_overrides={"search_knowledge_base": False},
    )
    assert not is_tool_allowed(p, "search_knowledge_base")


def test_tool_override_allow_grants_outside_group():
    p = _make(
        capability_groups=frozenset({"retrieval"}),
        tool_overrides={"add_project": True},
    )
    assert is_tool_allowed(p, "add_project")


def test_restricted_bot_sees_retrieval_only():
    roy = _make(capability_groups=frozenset({"retrieval"}))
    tools = effective_tools(roy)
    assert "search_current_project" in tools
    assert not any(
        t in tools for t in ("add_project", "reindex_project", "delete_collection")
    )


# --- project-scope authorization (fail-closed) --------------------------


def test_owner_all_projects_pass_through():
    p = _make(allowed_projects=None)
    assert set(authorize_projects(p, ["a", "c"])) == {"a", "c"}


def test_requested_intersected_with_allowed():
    p = _make(allowed_projects=frozenset({"a", "b"}))
    assert set(authorize_projects(p, ["a", "c"])) == {"a"}  # c dropped


def test_entirely_out_of_scope_refuses():
    p = _make(allowed_projects=frozenset({"a"}))
    with pytest.raises(ScopeDenied):
        authorize_projects(p, ["c"])


def test_unscoped_single_project_profile_auto_scopes():
    roy = _make(allowed_projects=frozenset({"royal-preps"}))
    assert authorize_projects(roy, None) == ["royal-preps"]


def test_unscoped_multi_project_profile_refuses():
    p = _make(allowed_projects=frozenset({"a", "b"}))
    with pytest.raises(ScopeDenied):
        authorize_projects(p, None)


def test_unscoped_owner_refuses_without_explicit_all():
    # Even the owner must be explicit — no accidental global from an empty req.
    p = _make(allowed_projects=None)
    with pytest.raises(ScopeDenied):
        authorize_projects(p, None)


def test_caller_supplied_foreign_project_cannot_widen():
    # The core guarantee: a caller naming a foreign project gets it dropped,
    # never honoured.
    p = _make(allowed_projects=frozenset({"mine"}))
    assert set(authorize_projects(p, ["mine", "someone-elses"])) == {"mine"}
