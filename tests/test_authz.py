"""S12/S13 — the single server-side authorization gate.

"Tool-list filtering alone is theatre if the endpoint is open" (§24.2). So every
call, regardless of what the client's tool list showed, passes through ONE gate
that re-checks — server-side — three things against the stored profile: may this
client use this tool (capability), is a destructive tool actually permitted
(group-9 modifier, §24.1), and what project scope does the request resolve to
(fail-closed). This pins that gate; it composes the S12 profiles core and the
S13 capability descriptor.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S12/S13 -> G12/G13)
"""

import pytest

from ragtools.authz import AuthzDecision, CapabilityDenied, enforce
from ragtools.profiles import ClientProfile, ScopeDenied


def _profile(**kw) -> ClientProfile:
    base = dict(
        profile_id="p",
        allowed_projects=frozenset({"mine"}),
        capability_groups=frozenset({"retrieval"}),
        tool_overrides={},
        cross_project_policy="none",
        destructive_policy="forbidden",
    )
    base.update(kw)
    return ClientProfile(**base)


# --- capability re-check ------------------------------------------------


def test_allowed_tool_and_scope_yields_decision():
    d = enforce(_profile(), "search_knowledge_base", requested_projects=["mine"])
    assert isinstance(d, AuthzDecision)
    assert d.tool == "search_knowledge_base"
    assert d.projects == ["mine"]


def test_tool_outside_profile_is_denied():
    with pytest.raises(CapabilityDenied):
        enforce(_profile(), "add_project", requested_projects=["mine"])


def test_tool_override_deny_is_enforced():
    p = _profile(tool_overrides={"search_knowledge_base": False})
    with pytest.raises(CapabilityDenied):
        enforce(p, "search_knowledge_base", requested_projects=["mine"])


# --- destructive modifier (§24.1) ---------------------------------------


def test_destructive_tool_denied_when_policy_forbidden():
    # collection_management grants delete_collection in the tool list, but the
    # destructive modifier must still refuse it under a 'forbidden' policy.
    p = _profile(capability_groups=frozenset({"collection_management"}),
                 destructive_policy="forbidden")
    with pytest.raises(CapabilityDenied):
        enforce(p, "delete_collection", requested_projects=["mine"])


def test_destructive_tool_allowed_when_policy_permits():
    p = _profile(capability_groups=frozenset({"collection_management"}),
                 destructive_policy="confirm_token")
    d = enforce(p, "delete_collection", requested_projects=["mine"])
    assert d.destructive_allowed is True


def test_non_destructive_tool_unaffected_by_destructive_policy():
    p = _profile(capability_groups=frozenset({"collection_management"}),
                 destructive_policy="forbidden")
    d = enforce(p, "list_collections", requested_projects=["mine"])
    assert d.destructive_allowed is False


# --- scope re-check (fail-closed) ---------------------------------------


def test_unresolved_scope_is_refused():
    p = _profile(allowed_projects=frozenset({"a", "b"}))
    with pytest.raises(ScopeDenied):
        enforce(p, "search_knowledge_base", requested_projects=None)


def test_foreign_project_dropped_from_scope():
    d = enforce(_profile(), "search_knowledge_base", requested_projects=["mine", "yours"])
    assert d.projects == ["mine"]
    assert "yours" not in d.projects


# --- audit composition (S16 §28): denials are recorded durably ----------


def test_capability_denial_is_audited(tmp_path):
    from ragtools.audit import AuditLog

    audit = AuditLog(str(tmp_path / "a.db"))
    with pytest.raises(CapabilityDenied):
        enforce(_profile(), "add_project", requested_projects=["mine"], audit=audit)
    events = audit.recent(event_type="denied_capability")
    assert len(events) == 1 and events[0].tool == "add_project"


def test_scope_failure_is_audited(tmp_path):
    from ragtools.audit import AuditLog

    audit = AuditLog(str(tmp_path / "a.db"))
    p = _profile(allowed_projects=frozenset({"a", "b"}))
    with pytest.raises(ScopeDenied):
        enforce(p, "search_knowledge_base", requested_projects=None, audit=audit)
    assert len(audit.recent(event_type="failed_scope")) == 1


def test_successful_enforce_records_no_denial(tmp_path):
    from ragtools.audit import AuditLog

    audit = AuditLog(str(tmp_path / "a.db"))
    enforce(_profile(), "search_knowledge_base", requested_projects=["mine"], audit=audit)
    assert audit.recent(event_type="denied_capability") == []
