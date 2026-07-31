"""MCP capability descriptor — tool annotations, destructive modifier, ordering.

The MCP contract half of the client-profile model (RAG v3, Stage S13). Where
:mod:`ragtools.profiles` answers *who may use a tool*, this answers *what a tool
is*: its ``ToolAnnotations`` hints, whether it is destructive, and the stable
order the tool list is emitted in.

Two plan facts are encoded here:

* **B30** — every tool carries ``readOnlyHint`` / ``destructiveHint`` /
  ``idempotentHint``. Today all 26 are ``None``; a client cannot tell a search
  from a delete. The table below is the fix, and a cross-module test asserts no
  authz-known tool is missing from it.
* **§24.1** — "Destructive operations" is a *modifier* on the functional groups,
  not a standalone grant: a destructive tool belongs to its group
  (``delete_collection`` is collection-management) but is only actually granted
  when the profile permits destructive ops. Profile-administration (group 8) is
  never grantable to an agent by an agent.

Pure and dependency-free (imports only the sibling group names for no cycle).

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S13 -> G13)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: Group 9 is a modifier layered onto 2–7, never a plain grant (§24.1).
MODIFIER_GROUPS: frozenset[str] = frozenset({"destructive"})

#: Group 8 — an agent can never hand another agent profile-administration.
NON_AGENT_GRANTABLE_GROUPS: frozenset[str] = frozenset({"profile_administration"})


@dataclass(frozen=True)
class ToolSpec:
    """A tool's MCP contract metadata: its group and its three annotation hints."""

    name: str
    group: str
    read_only: bool
    destructive: bool
    idempotent: bool

    def annotations(self) -> dict:
        """The MCP ``ToolAnnotations`` object for this tool (B30)."""
        return {
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
        }


def _ro(name: str, group: str) -> ToolSpec:
    """A read-only, idempotent, non-destructive tool (the common case)."""
    return ToolSpec(name, group, read_only=True, destructive=False, idempotent=True)


def _write(name: str, group: str, *, idempotent: bool, destructive: bool = False) -> ToolSpec:
    """A mutating tool; ``idempotent`` and ``destructive`` stated explicitly."""
    return ToolSpec(name, group, read_only=False, destructive=destructive, idempotent=idempotent)


# The registry. Groups mirror ragtools.profiles.CAPABILITY_GROUPS exactly; the
# cross-module test guarantees every authz-known tool appears here.
_SPECS: tuple[ToolSpec, ...] = (
    # retrieval — all read-only
    _ro("search_knowledge_base", "retrieval"),
    _ro("search_project_context", "retrieval"),
    _ro("search_current_project", "retrieval"),
    _ro("search_project_and_frameworks", "retrieval"),
    _ro("search_selected_projects", "retrieval"),
    _ro("find_definition", "retrieval"),
    _ro("secret_audit", "retrieval"),
    _ro("list_projects", "retrieval"),
    _ro("index_status", "retrieval"),
    # project_management — read views + config writes
    _ro("project_status", "project_management"),
    _ro("project_summary", "project_management"),
    _ro("list_project_files", "project_management"),
    _ro("get_project_ignore_rules", "project_management"),
    _ro("preview_ignore_effect", "project_management"),
    _write("add_project", "project_management", idempotent=False),
    _write("update_project", "project_management", idempotent=True),
    # Removing a project drops its collection AND its config entry. Nothing
    # brings either back, which is the same standard `delete_collection` is
    # held to — so it carries the same modifier and a profile granted
    # "Manage projects" still cannot delete one without the destructive opt-in.
    _write("delete_project", "project_management", idempotent=True, destructive=True),
    _write("set_project_mode", "project_management", idempotent=True),
    _write("add_project_ignore_rule", "project_management", idempotent=False),
    _write("remove_project_ignore_rule", "project_management", idempotent=True),
    # indexing — rebuild is recoverable, so not destructive (B30 example)
    _write("run_index", "indexing", idempotent=True),
    _write("reindex_project", "indexing", idempotent=True),
    _write("rebuild_index", "indexing", idempotent=True),
    # framework_management — the shared-dependency catalog and its links.
    # set_project_dependencies REPLACES a project's list, so it is idempotent:
    # sending the same list twice lands in the same state.
    _ro("list_dependencies", "framework_management"),
    _write("add_dependency", "framework_management", idempotent=False),
    _write("update_dependency", "framework_management", idempotent=True),
    _write("set_project_dependencies", "framework_management", idempotent=True),
    _write("remove_dependency", "framework_management", idempotent=True),
    _ro("list_frameworks", "framework_management"),
    _ro("framework_status", "framework_management"),
    _write("link_framework", "framework_management", idempotent=True),
    _write("unlink_framework", "framework_management", idempotent=True),
    _write("reindex_framework", "framework_management", idempotent=True),
    # collection_management — restore/delete are the irreversible ones
    _ro("list_collections", "collection_management"),
    _ro("collection_status", "collection_management"),
    _write("create_collection", "collection_management", idempotent=True),
    _write("snapshot_collection", "collection_management", idempotent=False),
    _write("restore_collection", "collection_management", idempotent=False, destructive=True),
    _write("delete_collection", "collection_management", idempotent=True, destructive=True),
    # configuration — reads, plus the settings write the admin panel performs.
    # No MCP tool exposes `update_config`; it is here because the HTTP endpoint
    # is authorized against the same vocabulary, so one table answers for both.
    _ro("get_config", "configuration"),
    _ro("get_ignore_rules", "configuration"),
    _ro("get_paths", "configuration"),
    _write("update_config", "configuration", idempotent=True),
    # service_operations — read-only diagnostics
    _ro("service_status", "service_operations"),
    _ro("recent_activity", "service_operations"),
    _ro("tail_logs", "service_operations"),
    _ro("crash_history", "service_operations"),
    _ro("system_health", "service_operations"),
    _ro("list_indexed_paths", "service_operations"),
    # service_control — stopping the service. Not destructive to the index, so
    # it is not behind the destructive modifier; it is behind its OWN group plus
    # an unconditional confirmation contract (see service/destructive.py).
    _write("shutdown_service", "service_control", idempotent=True),
    # profile_administration — owner-only, read views
    _ro("list_client_profiles", "profile_administration"),
    _ro("client_profile_status", "profile_administration"),
)

_BY_NAME: dict[str, ToolSpec] = {s.name: s for s in _SPECS}


def tool_spec(name: str) -> Optional[ToolSpec]:
    """The :class:`ToolSpec` for ``name``, or ``None`` if unknown."""
    return _BY_NAME.get(name)


def tool_annotations(name: str) -> dict:
    """The MCP ``ToolAnnotations`` for ``name``; ``KeyError`` if unknown."""
    return _BY_NAME[name].annotations()


def destructive_tools() -> frozenset[str]:
    """The set of tools whose ``destructiveHint`` is true (§24.1 modifier gate)."""
    return frozenset(s.name for s in _SPECS if s.destructive)


def apply_destructive_modifier(tools, *, allow_destructive: bool) -> set:
    """Drop destructive tools from ``tools`` unless the profile permits them.

    This is the group-9 modifier: functional-group membership grants the tool,
    but a destructive tool stays out of the effective set until destructive ops
    are explicitly allowed for the profile.
    """
    granted = set(tools)
    if allow_destructive:
        return granted
    return granted - destructive_tools()


def ordered_tools(names) -> list[str]:
    """Deterministic (name-sorted) tool ordering for a spec-compliant list."""
    return sorted(names)


def build_tool_list(names) -> list[dict]:
    """Build the ``tools/list`` payload: ordered ``{name, annotations}`` entries.

    Deterministically ordered (spec requirement for client-side caching) and
    each entry carries the B30 annotations. An unknown tool name raises
    ``KeyError`` rather than being silently dropped — the registered set and the
    spec table must agree.
    """
    return [
        {"name": name, "annotations": tool_annotations(name)}
        for name in ordered_tools(names)
    ]
