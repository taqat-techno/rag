"""A client restricted to one set of projects could read every other one.

`require_capability` checked the tool NAME. Nothing checked the tool's SCOPE.
The only production-shaped `authorize_projects` call lived in
`retrieval/router.py`, which has no production importer — four test modules
import it and nothing else does. Meanwhile `search_knowledge_base`'s own
docstring promises "pass neither ``project`` nor ``projects`` → search ALL
indexed content", and `_direct_search` passed the caller's argument straight
through to the searcher.

So a profile scoped to one client's projects could omit the argument and read
the whole machine. Capability isolation worked; project isolation did not — and
that is what has to be true before a per-client sidecar service can be replaced
by a client profile.

The owner default is load-bearing in the other direction: a single-owner install
must keep searching everything with no project argument, exactly as before.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ragtools.integration.mcp_authz import DEFAULT_OWNER_PROFILE, scope_for_search
from ragtools.profiles import CAPABILITY_GROUPS, ClientProfile, ScopeDenied


def client(*projects, groups=("retrieval",)):
    return ClientProfile(
        profile_id="roy",
        allowed_projects=frozenset(projects),
        capability_groups=frozenset(groups),
        cross_project_policy="selected",
        display_name="Roy",
        client_type="agent",
    )


# --- the owner default must not move ----------------------------------------


def test_the_owner_still_searches_everything_unscoped():
    assert scope_for_search(DEFAULT_OWNER_PROFILE, None, None) is None


def test_the_owner_still_gets_exactly_what_it_asks_for():
    assert scope_for_search(DEFAULT_OWNER_PROFILE, "anything", None) == ["anything"]
    assert scope_for_search(DEFAULT_OWNER_PROFILE, None, ["a", "b"]) == ["a", "b"]


# --- the boundary -----------------------------------------------------------


def test_an_unscoped_call_narrows_to_the_clients_own_projects():
    """The hole, closed. This used to widen to everything indexed."""
    roy = client("royal_preps_backend", "royal_preps_frontend")

    assert scope_for_search(roy, None, None) == ["royal_preps_backend",
                                                 "royal_preps_frontend"]


def test_a_foreign_project_is_refused():
    roy = client("royal_preps_backend")

    with pytest.raises(ScopeDenied):
        scope_for_search(roy, "another_clients_secrets", None)


def test_a_mixed_request_keeps_only_what_is_authorized():
    roy = client("royal_preps_backend")

    assert scope_for_search(roy, None,
                            ["royal_preps_backend", "another_client"]) == [
        "royal_preps_backend"]


def test_a_request_of_only_foreign_projects_raises_rather_than_emptying():
    """An empty scope downstream would read as "search everything"."""
    roy = client("royal_preps_backend")

    with pytest.raises(ScopeDenied):
        scope_for_search(roy, None, ["a", "b"])


def test_blank_entries_do_not_widen_the_scope():
    roy = client("royal_preps_backend")

    assert scope_for_search(roy, None, ["", "  ", "royal_preps_backend"]) == [
        "royal_preps_backend"]


def test_a_single_project_client_resolves_to_that_project():
    assert scope_for_search(client("only_one"), None, None) == ["only_one"]


def test_two_clients_cannot_see_each_other():
    roy = client("royal_preps")
    other = client("acme_corp")

    assert scope_for_search(roy, None, None) == ["royal_preps"]
    assert scope_for_search(other, None, None) == ["acme_corp"]
    with pytest.raises(ScopeDenied):
        scope_for_search(roy, "acme_corp", None)
    with pytest.raises(ScopeDenied):
        scope_for_search(other, "royal_preps", None)


# --- the wiring, which is the part that was missing --------------------------


def _calls_in(function_name: str, source: str) -> set[str]:
    """Every function called inside a named top-level function. AST, not grep:
    a textual scan matches the comment that explains the fix."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return {
                inner.func.id
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
            }
    raise AssertionError(f"{function_name} not found")


@pytest.fixture(scope="module")
def mcp_source() -> str:
    path = (Path(__file__).resolve().parents[1] / "src" / "ragtools" /
            "integration" / "mcp_server.py")
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("entry_point", [
    "_direct_search", "_direct_dev_search", "_direct_find_definition",
])
def test_every_retrieval_entry_point_resolves_the_authorized_scope(
        entry_point, mcp_source):
    """Defining the check was never the gap. Calling it was."""
    assert "_authorized_scope" in _calls_in(entry_point, mcp_source), (
        f"{entry_point} accepts a caller-supplied project without checking "
        f"whether this client may read it"
    )


@pytest.mark.parametrize("entry_point", [
    "_direct_search", "_direct_dev_search", "_direct_find_definition",
])
def test_every_retrieval_entry_point_refuses_a_half_built_index(
        entry_point, mcp_source):
    """`guard_ready` lives in `QdrantOwner.search`, which the proxy path reaches
    and the direct path does not — it builds its own `Searcher`. So during a
    layout rebuild, direct mode answered "no matches" from an index that had
    not been built: wrong, and completely convincing."""
    assert "_migration_error" in _calls_in(entry_point, mcp_source), (
        f"{entry_point} can answer from an index that is still being rebuilt"
    )


def test_the_capability_check_is_still_there(mcp_source):
    """The scope check is additional, not a replacement."""
    assert "_capability_error" in _calls_in("_direct_search", mcp_source)


def test_the_orphaned_router_is_not_the_enforcement_path(mcp_source):
    """`retrieval/router.py` holds an `authorize_projects` call and no
    production importer. If it ever becomes the seam, this test should be
    replaced — but silence about which module enforces is how the gap survived."""
    root = Path(__file__).resolve().parents[1] / "src" / "ragtools"
    importers = [
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if p.name != "router.py" and "retrieval.router" in p.read_text(encoding="utf-8")
    ]
    assert importers == [], (
        f"retrieval/router.py is now imported by {importers}; enforcement has "
        f"two homes and the tests only pin one"
    )


# --- the profile a client is actually given ---------------------------------


def test_a_retrieval_only_client_cannot_reach_management_tools():
    from ragtools.profiles import is_tool_allowed

    roy = client("royal_preps")

    assert is_tool_allowed(roy, "search_knowledge_base")
    assert not is_tool_allowed(roy, "add_project")
    assert not is_tool_allowed(roy, "delete_collection")


def test_the_snippet_binds_the_client_to_its_profile():
    """How a scoped client is actually wired up, end to end."""
    from ragtools.client_admin import client_config_snippet

    snippet = client_config_snippet(client("royal_preps"))

    assert snippet["mcpServers"]["rag-mcp"]["env"]["RAG_CLIENT_PROFILE"] == "roy"


def test_every_capability_group_is_a_real_group():
    assert set(CAPABILITY_GROUPS) >= {"retrieval", "project_management"}
