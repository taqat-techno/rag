"""The single server-side authorization gate (RAG v3, S12/S13).

Every route and every MCP tool call funnels through :func:`enforce` — the one
place where authorization is decided, server-side, from the caller's stored
:class:`~ragtools.profiles.ClientProfile`. It never trusts the tool list the
client happened to see (§24.2: filtering alone is theatre), and never trusts a
caller-supplied project.

Three checks, in order:

1. **Capability** — is this tool in the profile's effective set? Else
   :class:`CapabilityDenied`.
2. **Destructive modifier** (§24.1) — a destructive tool is refused unless the
   profile's ``destructive_policy`` permits it, even when its functional group
   is granted.
3. **Scope** — resolve the project scope fail-closed
   (:func:`~ragtools.profiles.authorize_projects`); an unresolved scope raises
   :class:`~ragtools.profiles.ScopeDenied`, a foreign project is dropped.

Composes S12 (profiles) and S13 (capability descriptor); pure and dependency-light.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S12/S13 -> G12/G13)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ragtools.mcp_capabilities import destructive_tools
from ragtools.profiles import (
    ClientProfile,
    ScopeDenied,
    authorize_projects,
    is_tool_allowed,
)


class CapabilityDenied(PermissionError):
    """The profile may not use this tool (capability or destructive modifier)."""


@dataclass
class AuthzDecision:
    """The result of a granted authorization: the tool, its resolved scope, and
    whether destructive execution is permitted for this profile."""

    tool: str
    projects: list
    destructive_allowed: bool


def enforce(
    profile: ClientProfile,
    tool: str,
    *,
    requested_projects: Optional[list] = None,
    audit=None,
) -> AuthzDecision:
    """Authorize a single call, server-side. Raise on any failure; else decide.

    Order matters: capability before destructive-modifier before scope, so the
    cheapest, most fundamental refusal wins and no scope work happens for a tool
    the client may not use at all.

    ``audit`` (optional :class:`~ragtools.audit.AuditLog`) durably records the
    two behavioral signals §28.1 wants — a denied capability and a failed scope —
    the moment they happen, so a client drifting from expectation is observable.
    """
    if not is_tool_allowed(profile, tool):
        if audit is not None:
            audit.record_denied_capability(profile_id=profile.profile_id, tool=tool)
        raise CapabilityDenied(
            f"profile {profile.profile_id!r} may not use tool {tool!r}"
        )

    is_destructive = tool in destructive_tools()
    destructive_allowed = profile.destructive_policy != "forbidden"
    if is_destructive and not destructive_allowed:
        if audit is not None:
            audit.record_denied_capability(profile_id=profile.profile_id, tool=tool)
        raise CapabilityDenied(
            f"tool {tool!r} is destructive and profile {profile.profile_id!r} "
            f"has destructive_policy={profile.destructive_policy!r}"
        )

    try:
        projects = authorize_projects(profile, requested_projects)
    except ScopeDenied:
        if audit is not None:
            audit.record_failed_scope(profile_id=profile.profile_id, requested=requested_projects)
        raise

    return AuthzDecision(
        tool=tool,
        projects=projects,
        destructive_allowed=is_destructive and destructive_allowed,
    )
