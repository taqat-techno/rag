"""S13 — MCP capability descriptor: per-tool annotations, destructive modifier,
deterministic ordering (pure core).

Two contract facts from the plan drive this:

* **B30** — every tool must carry ``ToolAnnotations`` (``readOnlyHint`` /
  ``destructiveHint`` / ``idempotentHint``); today all 26 are ``None``.
* **§24.1** — the "Destructive operations" capability is a *modifier* on groups
  2–7, not a standalone grant; and profile-administration (group 8) is never
  grantable to an agent by an agent.

This pins the annotation table, the destructive-modifier filter, deterministic
ordering (the incoming spec requires a stable tool list for client caching),
and the cross-module invariant that every authz-known tool
(:data:`ragtools.profiles.CAPABILITY_GROUPS`) is annotation-known here — so the
two tables cannot drift back into a ``None`` gap.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S13 -> G13)
"""

import pytest

from ragtools.mcp_capabilities import (
    MODIFIER_GROUPS,
    NON_AGENT_GRANTABLE_GROUPS,
    apply_destructive_modifier,
    destructive_tools,
    ordered_tools,
    tool_annotations,
    tool_spec,
)
from ragtools.profiles import CAPABILITY_GROUPS


PRESERVED = [
    "search_knowledge_base", "search_project_context", "find_definition",
    "secret_audit", "list_projects", "index_status", "project_status",
    "project_summary", "list_project_files", "get_project_ignore_rules",
    "preview_ignore_effect", "run_index", "reindex_project", "add_project",
    "set_project_mode", "add_project_ignore_rule", "remove_project_ignore_rule",
    "service_status", "recent_activity", "tail_logs", "crash_history",
    "get_config", "get_ignore_rules", "get_paths", "system_health",
    "list_indexed_paths",
]


# --- B30: no tool has a None annotation ---------------------------------


@pytest.mark.parametrize("name", PRESERVED)
def test_every_preserved_tool_has_full_annotations(name):
    ann = tool_annotations(name)
    assert set(ann) == {"readOnlyHint", "destructiveHint", "idempotentHint"}
    assert all(isinstance(v, bool) for v in ann.values())


def test_read_only_retrieval_tool_hints():
    ann = tool_annotations("search_knowledge_base")
    assert ann == {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}


def test_reindex_has_annotation_and_is_not_read_only():
    # B30's named example: reindex_project's annotation used to be None.
    spec = tool_spec("reindex_project")
    assert spec is not None
    assert spec.read_only is False
    assert spec.idempotent is True
    assert spec.destructive is False  # rebuild is recoverable, not irreversible


# --- destructive classification + modifier (§24.1) ----------------------


def test_destructive_tools_are_the_irreversible_ones():
    assert destructive_tools() == frozenset({"delete_collection", "restore_collection"})
    assert tool_spec("delete_collection").destructive is True
    assert tool_spec("delete_collection").read_only is False


def test_destructive_modifier_removes_unless_allowed():
    granted = {"search_knowledge_base", "delete_collection", "restore_collection"}
    without = apply_destructive_modifier(granted, allow_destructive=False)
    assert without == {"search_knowledge_base"}
    with_ = apply_destructive_modifier(granted, allow_destructive=True)
    assert with_ == granted


def test_destructive_is_a_modifier_group_not_a_standalone_grant():
    assert "destructive" in MODIFIER_GROUPS
    assert "destructive" not in CAPABILITY_GROUPS  # never a plain group


def test_profile_administration_is_not_agent_grantable():
    assert "profile_administration" in NON_AGENT_GRANTABLE_GROUPS


# --- deterministic ordering (spec: stable list for client caching) ------


def test_ordered_tools_is_sorted_and_deterministic():
    shuffled = ["run_index", "add_project", "search_knowledge_base", "delete_collection"]
    out1 = ordered_tools(shuffled)
    out2 = ordered_tools(list(reversed(shuffled)))
    assert out1 == out2 == sorted(shuffled)


def test_unknown_tool_has_no_spec():
    assert tool_spec("no_such_tool") is None
    with pytest.raises(KeyError):
        tool_annotations("no_such_tool")


# --- cross-module invariant: authz table and annotation table agree -----


def test_every_authz_known_tool_is_annotation_known():
    # Any tool a profile can be granted must carry annotations — this is the
    # guard against a tool re-acquiring a None annotation (B30) by being added
    # to a capability group but not the spec table.
    authz_tools = set().union(*CAPABILITY_GROUPS.values())
    missing = sorted(t for t in authz_tools if tool_spec(t) is None)
    assert missing == [], f"authz-known but annotation-unknown: {missing}"
