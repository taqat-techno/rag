"""S12 — client-profile administration service (shared by CLI + UI).

The one place that turns owner input (checkboxes / flags) into a validated
:class:`~ragtools.profiles.ClientProfile`, enforcing the security rules so both
surfaces behave identically: an ungrantable owner-only group is refused, the
implicit ``owner`` id is reserved, destructive access is an explicit opt-in, and
an unknown capability or malformed id is rejected — never silently widened.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S12 -> G12)
"""

import pytest

from ragtools.client_admin import (
    AGENT_GRANTABLE_GROUPS,
    CAPABILITY_CATALOG,
    ClientAdminError,
    build_profile,
    client_config_snippet,
    profile_summary,
)
from ragtools.profiles import CAPABILITY_GROUPS, ClientProfile


# --- the checkbox catalog stays in sync with the authz groups -----------


def test_catalog_covers_every_capability_group():
    catalog_groups = {c.group for c in CAPABILITY_CATALOG}
    assert catalog_groups == set(CAPABILITY_GROUPS)  # no drift
    for c in CAPABILITY_CATALOG:
        assert c.label and c.description and c.tier in ("read", "write", "owner")


def test_agent_grantable_excludes_owner_only_admin():
    assert "profile_administration" not in AGENT_GRANTABLE_GROUPS
    assert "retrieval" in AGENT_GRANTABLE_GROUPS


# --- building a profile from owner input --------------------------------


def test_build_all_projects_read_only():
    p = build_profile(profile_id="reader", display_name="Reader",
                      all_projects=True, capabilities=["retrieval"])
    assert isinstance(p, ClientProfile)
    assert p.allowed_projects is None          # all projects
    assert p.capability_groups == frozenset({"retrieval"})
    assert p.destructive_policy == "forbidden"  # destructive off by default


def test_build_scoped_to_specific_projects():
    p = build_profile(profile_id="bot", projects=["rag-docs", "royal"],
                      capabilities=["retrieval", "indexing"])
    assert p.allowed_projects == frozenset({"rag-docs", "royal"})
    assert p.capability_groups == frozenset({"retrieval", "indexing"})


def test_allow_destructive_opts_into_confirm_token():
    p = build_profile(profile_id="ops", all_projects=True,
                      capabilities=["collection_management"], allow_destructive=True)
    assert p.destructive_policy != "forbidden"


# --- validation: cover every bad situation ------------------------------


@pytest.mark.parametrize("bad_id", ["", "  ", "Bad Id", "-x", "owner", "OWNER"])
def test_bad_profile_id_is_refused(bad_id):
    with pytest.raises(ClientAdminError):
        build_profile(profile_id=bad_id, all_projects=True, capabilities=["retrieval"])


def test_ungrantable_admin_group_is_refused():
    with pytest.raises(ClientAdminError):
        build_profile(profile_id="x", all_projects=True,
                      capabilities=["retrieval", "profile_administration"])


def test_unknown_capability_is_refused():
    with pytest.raises(ClientAdminError):
        build_profile(profile_id="x", all_projects=True, capabilities=["make_coffee"])


def test_scope_must_be_chosen():
    # Neither all_projects nor any project → refused (no accidental empty/global).
    with pytest.raises(ClientAdminError):
        build_profile(profile_id="x", capabilities=["retrieval"])


def test_all_projects_and_specific_projects_conflict_is_refused():
    with pytest.raises(ClientAdminError):
        build_profile(profile_id="x", all_projects=True, projects=["a"],
                      capabilities=["retrieval"])


def test_no_capabilities_is_allowed_but_locked():
    # A profile with zero capabilities is valid (a disabled client), not an error.
    p = build_profile(profile_id="locked", all_projects=True, capabilities=[])
    assert p.capability_groups == frozenset()


def test_blank_project_entries_are_refused():
    with pytest.raises(ClientAdminError):
        build_profile(profile_id="x", projects=["ok", "  "], capabilities=["retrieval"])


# --- config snippet + summary (for CLI/UI display) ----------------------


def test_config_snippet_shape():
    p = build_profile(profile_id="reader", all_projects=True, capabilities=["retrieval"])
    snip = client_config_snippet(p)
    assert snip["mcpServers"]["rag-mcp"]["env"]["RAG_CLIENT_PROFILE"] == "reader"
    assert "command" in snip["mcpServers"]["rag-mcp"]


def test_profile_summary_fields():
    p = build_profile(profile_id="bot", display_name="Bot", projects=["rag-docs"],
                      capabilities=["retrieval"], allow_destructive=False)
    s = profile_summary(p)
    assert s["profile_id"] == "bot"
    assert s["display_name"] == "Bot"
    assert s["scope"] == ["rag-docs"]
    assert s["capabilities"] == ["retrieval"]
    assert s["destructive"] is False
    assert s["tool_count"] > 0            # retrieval grants some tools
