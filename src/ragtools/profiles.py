"""Local Client Profiles + server-side authorization (RAG v3, Stage S12).

Single-owner, multi-client. Each local client (Claude owner, a dev agent, a
project-scoped bot, CI) has a profile that defines what it may reach. The
authorization decision is computed **server-side** from the profile and never
trusts a caller-supplied project, collection, prompt text, or claimed name.

This module is the pure, dependency-free core: the profile record and the
resolvers for effective tools (capability groups + per-tool overrides) and
fail-closed project scope. Enforcement wiring into the routes/MCP is layered on
top (so every operation is re-checked server-side, not just filtered in the
tool list).

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S12 -> G12)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


class ScopeDenied(PermissionError):
    """A request resolved to no authorized project scope. Fail closed."""


#: Capability group -> the tools it grants. Tool NAMES are the permission
#: boundary (a client that never sees a tool cannot call it); groups keep the
#: policy human-manageable. Additive: new tools join a group here.
CAPABILITY_GROUPS: dict[str, frozenset[str]] = {
    "retrieval": frozenset({
        "search_knowledge_base", "search_project_context", "search_current_project",
        "search_project_and_frameworks", "search_selected_projects",
        "find_definition", "secret_audit", "list_projects", "index_status",
    }),
    "project_management": frozenset({
        "add_project", "update_project", "delete_project",
        "set_project_mode", "add_project_ignore_rule",
        "remove_project_ignore_rule", "project_status", "project_summary",
        "list_project_files", "get_project_ignore_rules", "preview_ignore_effect",
    }),
    "indexing": frozenset({"run_index", "reindex_project", "rebuild_index"}),
    "framework_management": frozenset({
        "list_dependencies", "add_dependency", "update_dependency",
        "set_project_dependencies", "remove_dependency",
        "list_frameworks", "link_framework", "unlink_framework", "reindex_framework",
        "framework_status",
    }),
    "collection_management": frozenset({
        "list_collections", "collection_status", "create_collection",
        "snapshot_collection", "restore_collection", "delete_collection",
    }),
    "configuration": frozenset({"get_config", "get_ignore_rules", "get_paths",
                                "update_config"}),
    "service_operations": frozenset({
        "service_status", "recent_activity", "tail_logs", "crash_history",
        "system_health", "list_indexed_paths",
    }),
    # Stopping the service is not a diagnostic. Folding `shutdown_service` into
    # `service_operations` would have let every profile granted "view the logs"
    # stop the machine's knowledge base; it gets its own group so granting it is
    # a separate, deliberate act.
    "service_control": frozenset({"shutdown_service"}),
    "profile_administration": frozenset({
        "list_client_profiles", "client_profile_status",
    }),
}


@dataclass
class ClientProfile:
    """A local client's identity, scope, and capabilities."""

    profile_id: str
    #: Projects this client may reach. ``None`` means ALL (the owner profile).
    allowed_projects: Optional[frozenset[str]]
    capability_groups: frozenset[str]
    #: Per-tool allow/deny that overrides the group grant.
    tool_overrides: dict[str, bool] = field(default_factory=dict)
    cross_project_policy: str = "none"  # none | selected | all
    destructive_policy: str = "forbidden"  # forbidden | confirm_token | owner_approval
    display_name: str = ""
    client_type: str = ""


def effective_tools(profile: ClientProfile) -> frozenset[str]:
    """The exact set of tools this profile may use (groups + overrides)."""
    tools: set[str] = set()
    for group in profile.capability_groups:
        tools |= CAPABILITY_GROUPS.get(group, frozenset())
    for tool, allow in profile.tool_overrides.items():
        if allow:
            tools.add(tool)
        else:
            tools.discard(tool)
    return frozenset(tools)


def is_tool_allowed(profile: ClientProfile, tool: str) -> bool:
    """Server-side re-check: may this profile execute ``tool``?"""
    return tool in effective_tools(profile)


def authorize_projects(
    profile: ClientProfile, requested: Optional[list[str]]
) -> list[str]:
    """Resolve a fail-closed project scope for ``profile``.

    * ``requested`` given → intersect with the profile's allowed set (``None`` =
      all). A caller-named foreign project is DROPPED, never honoured. If the
      intersection is empty, :class:`ScopeDenied`.
    * ``requested`` absent → auto-scope only if the profile has exactly one
      allowed project; otherwise refuse (no accidental global).
    """
    allowed = profile.allowed_projects

    if requested is None:
        if allowed is not None and len(allowed) == 1:
            return list(allowed)
        raise ScopeDenied(
            f"unscoped request and profile {profile.profile_id!r} does not "
            "resolve a single default project; specify a project"
        )

    req = [r.strip() for r in requested if r and r.strip()]
    if not req:
        raise ScopeDenied("empty project scope")

    if allowed is None:
        return req  # owner: everything requested is allowed

    granted = [r for r in req if r in allowed]
    if not granted:
        raise ScopeDenied(
            f"none of {req} are within profile {profile.profile_id!r}'s "
            "allowed projects"
        )
    return granted
