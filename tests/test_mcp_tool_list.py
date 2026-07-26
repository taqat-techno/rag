"""S13 — the tools/list payload builder (deterministic, annotated).

The incoming MCP spec requires a stable, annotated tool list for client-side
caching (§24.2/§24.4). This pins the pure builder that turns a set of registered
tool names into that payload: deterministically ordered, each entry carrying the
B30 annotations. Composes `tool_annotations` + `ordered_tools`.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S13 -> G13)
"""

import pytest

from ragtools.mcp_capabilities import build_tool_list


def test_entries_are_ordered_and_annotated():
    out = build_tool_list(["run_index", "search_knowledge_base", "delete_collection"])
    assert [e["name"] for e in out] == sorted(
        ["run_index", "search_knowledge_base", "delete_collection"]
    )
    for e in out:
        assert set(e["annotations"]) == {"readOnlyHint", "destructiveHint", "idempotentHint"}


def test_annotation_values_are_correct():
    out = build_tool_list(["search_knowledge_base", "delete_collection"])
    by_name = {e["name"]: e["annotations"] for e in out}
    assert by_name["search_knowledge_base"]["readOnlyHint"] is True
    assert by_name["delete_collection"]["destructiveHint"] is True


def test_is_deterministic_regardless_of_input_order():
    a = build_tool_list(["a" for a in ()] or ["run_index", "add_project"])
    b = build_tool_list(["add_project", "run_index"])
    assert a == b


def test_unknown_tool_is_rejected_not_silently_dropped():
    with pytest.raises(KeyError):
        build_tool_list(["search_knowledge_base", "no_such_tool"])
