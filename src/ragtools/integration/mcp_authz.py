"""MCP-side profile resolution + capability guard (RAG v3, S12/S13 §24.2).

The MCP process learns which client it serves from its spawn (here, the
``RAG_CLIENT_PROFILE`` env var — the local equivalent of §24.2's
``spawn(profile=<id>)``) and loads that profile from the store. It then
re-checks every tool call server-side, so tool-list filtering is not the only
line of defence.

Backward compatibility is the design constraint: with no configuration the
active profile is the OWNER — all capability groups, all projects — so the
existing single-owner MCP server behaves exactly as before. A profile id that is
named but absent from the store is a misconfiguration and fails CLOSED (raises),
never silently escalating to owner.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S12/S13 §24.2)
"""

from __future__ import annotations

import os
from typing import Optional

from ragtools.authz import CapabilityDenied
from ragtools.profiles import CAPABILITY_GROUPS, ClientProfile, is_tool_allowed

#: The implicit profile when nothing is configured: the owner. All capability
#: groups, all projects (``allowed_projects=None``), destructive permitted.
DEFAULT_OWNER_PROFILE = ClientProfile(
    profile_id="owner",
    allowed_projects=None,
    capability_groups=frozenset(CAPABILITY_GROUPS),
    tool_overrides={},
    cross_project_policy="all",
    destructive_policy="owner_approval",
    display_name="Owner",
    client_type="owner",
)


def resolve_active_profile(*, env: Optional[dict] = None, store=None) -> ClientProfile:
    """Resolve the profile this MCP process serves.

    ``RAG_CLIENT_PROFILE`` unset → :data:`DEFAULT_OWNER_PROFILE` (backward-compatible
    single owner). Set and found in ``store`` → that profile. Set but absent →
    fail closed (``ValueError``); never fall back to owner for a named client.
    """
    env = env if env is not None else os.environ
    pid = env.get("RAG_CLIENT_PROFILE")
    if not pid:
        return DEFAULT_OWNER_PROFILE
    profile = store.get(pid) if store is not None else None
    if profile is None:
        raise ValueError(
            f"RAG_CLIENT_PROFILE={pid!r} is not a known client profile; refusing "
            "to run (a named client never silently becomes the owner)"
        )
    return profile


def require_capability(profile: ClientProfile, tool: str, *, audit=None) -> None:
    """Re-check, server-side, that ``profile`` may use ``tool``. Raise if not.

    Records a ``denied_capability`` event to ``audit`` (optional) when it
    refuses, so a client reaching for tools it should not have is observable.
    """
    if not is_tool_allowed(profile, tool):
        if audit is not None:
            audit.record_denied_capability(profile_id=profile.profile_id, tool=tool)
        raise CapabilityDenied(
            f"client profile {profile.profile_id!r} may not use tool {tool!r}"
        )
