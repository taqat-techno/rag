"""Client-profile administration — the shared service behind the CLI and UI (S12).

Both surfaces (``rag client ...`` and the ``/config`` panel) turn owner input
into a validated :class:`~ragtools.profiles.ClientProfile` through THIS module,
so the security rules are enforced in exactly one place:

* the implicit ``owner`` id is reserved (a stored "owner" would shadow the
  real owner default);
* the owner-only ``profile_administration`` group is never grantable to an agent
  (:data:`~ragtools.mcp_capabilities.NON_AGENT_GRANTABLE_GROUPS`);
* destructive access is an explicit opt-in (a checkbox), never implied by a group;
* scope must be chosen — all-projects OR a specific set, never an accidental
  empty/global;
* unknown capabilities and malformed ids are refused, not silently dropped.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S12 -> G12)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ragtools.mcp_capabilities import NON_AGENT_GRANTABLE_GROUPS
from ragtools.profiles import (
    CAPABILITY_GROUPS,
    ClientProfile,
    effective_tools,
)

#: Reserved — the implicit owner (no ``RAG_CLIENT_PROFILE``) uses this id; a
#: stored profile of the same name would be ambiguous.
RESERVED_PROFILE_IDS = frozenset({"owner"})

_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_MAX_ID = 64

#: Destructive opt-in maps to this policy (destructive ops still need a confirm
#: token at call time; the checkbox only lifts the blanket ``forbidden``).
_DESTRUCTIVE_ON = "confirm_token"
_DESTRUCTIVE_OFF = "forbidden"


class ClientAdminError(ValueError):
    """Owner input for a client profile is invalid or violates a security rule."""


@dataclass(frozen=True)
class CapabilityInfo:
    """One security checkbox: a capability group with owner-facing metadata."""

    group: str
    label: str
    description: str
    tier: str  # "read" | "write" | "owner"


#: The checkbox catalog. Order = display order. Kept in sync with
#: ``profiles.CAPABILITY_GROUPS`` (a test asserts no drift).
CAPABILITY_CATALOG: tuple[CapabilityInfo, ...] = (
    CapabilityInfo("retrieval", "Search & read",
                   "Search the knowledge base, find definitions, list projects.", "read"),
    CapabilityInfo("configuration", "View configuration",
                   "Read configuration, paths, and ignore rules.", "read"),
    CapabilityInfo("service_operations", "Service diagnostics",
                   "View logs, activity, health, and crash history.", "read"),
    CapabilityInfo("project_management", "Manage projects",
                   "Add and edit projects and their ignore rules.", "write"),
    CapabilityInfo("indexing", "Run indexing",
                   "Index and re-index projects.", "write"),
    CapabilityInfo("framework_management", "Manage frameworks",
                   "Link, unlink, and re-index framework corpora.", "write"),
    CapabilityInfo("collection_management", "Manage collections",
                   "Create, snapshot, restore, and delete collections.", "write"),
    CapabilityInfo("profile_administration", "Administer client profiles",
                   "List and inspect other client profiles (owner only).", "owner"),
)

#: The groups an owner may grant an agent — everything except the owner-only
#: administration group.
AGENT_GRANTABLE_GROUPS: frozenset[str] = frozenset(
    c.group for c in CAPABILITY_CATALOG if c.group not in NON_AGENT_GRANTABLE_GROUPS
)


def validate_profile_id(profile_id: str) -> str:
    """Return the normalized profile id, or raise :class:`ClientAdminError`."""
    if profile_id is None or not str(profile_id).strip():
        raise ClientAdminError("client id is required")
    pid = str(profile_id).strip()
    if pid.lower() in RESERVED_PROFILE_IDS:
        raise ClientAdminError(
            f"{pid!r} is reserved — the owner is the implicit default "
            "(no RAG_CLIENT_PROFILE). Choose a different id."
        )
    if len(pid) > _MAX_ID or not _PROFILE_ID_RE.match(pid):
        raise ClientAdminError(
            "client id must be lowercase alphanumeric, starting with a letter or "
            "digit, using only hyphens and underscores"
        )
    return pid


def build_profile(
    *,
    profile_id: str,
    display_name: str = "",
    all_projects: bool = False,
    projects: Optional[list] = None,
    capabilities: Optional[list] = None,
    allow_destructive: bool = False,
    client_type: str = "agent",
    tool_overrides: Optional[dict] = None,
) -> ClientProfile:
    """Validate owner input and return a :class:`ClientProfile` (fail-closed)."""
    pid = validate_profile_id(profile_id)

    # --- scope: exactly one of all-projects / specific projects ---
    projects = projects or []
    if all_projects and projects:
        raise ClientAdminError(
            "choose EITHER all projects OR a specific set of projects, not both"
        )
    if not all_projects and not projects:
        raise ClientAdminError(
            "scope is required: grant all projects, or name at least one project"
        )
    if all_projects:
        allowed_projects = None
    else:
        cleaned = [p.strip() for p in projects]
        if any(not p for p in cleaned):
            raise ClientAdminError("project names cannot be blank")
        allowed_projects = frozenset(cleaned)

    # --- capabilities: known + agent-grantable ---
    caps = list(capabilities or [])
    unknown = [c for c in caps if c not in CAPABILITY_GROUPS]
    if unknown:
        raise ClientAdminError(f"unknown capabilities: {sorted(set(unknown))}")
    ungrantable = [c for c in caps if c not in AGENT_GRANTABLE_GROUPS]
    if ungrantable:
        raise ClientAdminError(
            f"{sorted(set(ungrantable))} is owner-only and cannot be granted to a client"
        )

    return ClientProfile(
        profile_id=pid,
        allowed_projects=allowed_projects,
        capability_groups=frozenset(caps),
        tool_overrides=dict(tool_overrides or {}),
        cross_project_policy="all" if all_projects else "selected",
        destructive_policy=_DESTRUCTIVE_ON if allow_destructive else _DESTRUCTIVE_OFF,
        display_name=display_name or pid,
        client_type=client_type,
    )


def client_config_snippet(
    profile: ClientProfile, *, command: str = "rag-mcp", server_name: str = "rag-mcp"
) -> dict:
    """The ``.mcp.json`` snippet that binds a client to this profile at spawn."""
    return {
        "mcpServers": {
            server_name: {
                "command": command,
                "env": {"RAG_CLIENT_PROFILE": profile.profile_id},
            }
        }
    }


def profile_summary(profile: ClientProfile) -> dict:
    """A display-ready summary for the CLI and UI list views."""
    return {
        "profile_id": profile.profile_id,
        "display_name": profile.display_name or profile.profile_id,
        "scope": "all projects" if profile.allowed_projects is None
                 else sorted(profile.allowed_projects),
        "capabilities": sorted(profile.capability_groups),
        "destructive": profile.destructive_policy != _DESTRUCTIVE_OFF,
        "tool_count": len(effective_tools(profile)),
    }
