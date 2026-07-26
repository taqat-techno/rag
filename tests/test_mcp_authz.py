"""S12/S13 §24.2 — the MCP server's live profile-resolution + capability guard.

"The ragtools service RE-CHECKS authorization on EVERY call. Tool-list filtering
alone is theatre if the endpoint is open." The MCP process learns its client
identity from the spawn (an env var here) and loads that profile from the store;
absent any configuration it is the OWNER (all tools, all projects), so existing
single-owner behavior is preserved byte-for-byte. A NAMED-but-unknown profile is
a misconfiguration and fails closed — never silently escalates to owner.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S12/S13 §24.2)
"""

import pytest

from ragtools.authz import CapabilityDenied
from ragtools.integration.mcp_authz import (
    DEFAULT_OWNER_PROFILE,
    require_capability,
    resolve_active_profile,
)
from ragtools.profiles import CAPABILITY_GROUPS, ClientProfile


PRESERVED_TOOLS = [
    "search_knowledge_base", "search_project_context", "find_definition",
    "secret_audit", "list_projects", "index_status", "add_project",
    "set_project_mode", "run_index", "reindex_project", "get_config",
    "service_status", "delete_collection",
]


# --- default owner preserves single-owner behavior ----------------------


def test_no_env_resolves_to_owner_with_all_tools():
    p = resolve_active_profile(env={}, store=None)
    assert p is DEFAULT_OWNER_PROFILE
    assert p.allowed_projects is None  # owner sees ALL projects
    for tool in PRESERVED_TOOLS:
        require_capability(p, tool)  # owner may use every preserved tool — no raise


def test_owner_has_every_capability_group():
    assert DEFAULT_OWNER_PROFILE.capability_groups == frozenset(CAPABILITY_GROUPS)


# --- a configured profile is loaded and enforced ------------------------


class _Store:
    def __init__(self, **profiles):
        self._p = profiles

    def get(self, pid):
        return self._p.get(pid)


def test_named_profile_is_loaded_from_store():
    roy = ClientProfile(profile_id="roy", allowed_projects=frozenset({"royal"}),
                        capability_groups=frozenset({"retrieval"}))
    p = resolve_active_profile(env={"RAG_CLIENT_PROFILE": "roy"}, store=_Store(roy=roy))
    assert p is roy


def test_restricted_profile_is_denied_admin_tools():
    roy = ClientProfile(profile_id="roy", allowed_projects=frozenset({"royal"}),
                        capability_groups=frozenset({"retrieval"}))
    require_capability(roy, "search_knowledge_base")  # allowed
    with pytest.raises(CapabilityDenied):
        require_capability(roy, "delete_collection")  # not in retrieval


def test_named_but_unknown_profile_fails_closed():
    # A profile id that isn't in the store must NOT silently become the owner.
    with pytest.raises(ValueError):
        resolve_active_profile(env={"RAG_CLIENT_PROFILE": "ghost"}, store=_Store())


def test_denied_capability_is_audited(tmp_path):
    from ragtools.audit import AuditLog

    audit = AuditLog(str(tmp_path / "a.db"))
    roy = ClientProfile(profile_id="roy", allowed_projects=None,
                        capability_groups=frozenset({"retrieval"}))
    with pytest.raises(CapabilityDenied):
        require_capability(roy, "delete_collection", audit=audit)
    assert len(audit.recent(event_type="denied_capability")) == 1


# --- LIVE wiring: _direct_search's capability gate -----------------------


def test_search_handler_owner_default_passes():
    # The single-owner default must not gate anything (backward-compatible).
    from ragtools.integration import mcp_server

    assert mcp_server._capability_error("search_knowledge_base", True, "q") is None


def test_search_handler_denies_a_restricted_profile(monkeypatch):
    # A profile without the retrieval group is refused at the handler, with the
    # standard envelope — proving the re-check is live, not just the tool list.
    from ragtools.integration import mcp_server, mcp_authz

    restricted = ClientProfile(profile_id="roy", allowed_projects=None,
                               capability_groups=frozenset({"configuration"}))  # no retrieval
    monkeypatch.setattr(mcp_authz, "resolve_active_profile", lambda **kw: restricted)
    env = mcp_server._capability_error("search_knowledge_base", True, "q")
    assert env is not None
    assert env["meta"]["error_code"] == "CAPABILITY_DENIED"
    assert env["results"] == []


def test_retrieval_only_bot_cannot_mutate(monkeypatch):
    # The end-to-end point: a retrieval-only profile calling add_project /
    # run_index is refused server-side with CAPABILITY_DENIED — its tool list
    # never containing them is not the only defence.
    from ragtools.integration import mcp_server, mcp_authz

    bot = ClientProfile(profile_id="bot", allowed_projects=frozenset({"a"}),
                        capability_groups=frozenset({"retrieval"}))  # read-only
    monkeypatch.setattr(mcp_authz, "resolve_active_profile", lambda **kw: bot)
    for tool in ("add_project", "run_index", "reindex_project", "set_project_mode"):
        env = mcp_server._ops_capability_error(tool)
        assert env is not None, tool
        assert env["error_code"] == "CAPABILITY_DENIED", tool


def test_ops_capability_owner_default_is_noop():
    from ragtools.integration import mcp_server

    assert mcp_server._ops_capability_error("add_project") is None


def test_active_profile_resolves_a_configured_profile_at_runtime(monkeypatch):
    # With RAG_CLIENT_PROFILE set and a populated store, the MCP process serves
    # THAT profile — the runtime piece that makes non-owner enforcement real.
    from ragtools.integration import mcp_server

    class _Store:
        def get(self, pid):
            if pid == "roy":
                return ClientProfile(profile_id="roy", allowed_projects=frozenset({"a"}),
                                     capability_groups=frozenset({"retrieval"}))
            return None

    monkeypatch.setattr(mcp_server, "_get_profile_store", lambda: _Store())
    monkeypatch.setenv("RAG_CLIENT_PROFILE", "roy")
    assert mcp_server._active_profile().profile_id == "roy"


def test_active_profile_is_owner_without_env(monkeypatch):
    from ragtools.integration import mcp_server

    monkeypatch.delenv("RAG_CLIENT_PROFILE", raising=False)
    assert mcp_server._active_profile().profile_id == "owner"
