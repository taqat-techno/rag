"""S16 §28 — durable audit log for the highest-value new signals.

§28.1: denied-capability and failed-scope-expansion events "are how the owner
learns a client is behaving differently from expectation — currently
unobservable by construction." §28.2: the audit "must be durable — SQLite, not
the in-memory ring buffer a restart erases." This pins that store; `authz`
composes it (a denial records an event).

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S16 §28 -> G16)
"""

import pytest

from ragtools.audit import AuditEvent, AuditLog


@pytest.fixture
def audit(tmp_path):
    return AuditLog(str(tmp_path / "audit.db"))


def test_record_and_read_back(audit):
    audit.record("search", profile_id="roy", tool="search_knowledge_base", detail="q=x")
    events = audit.recent()
    assert len(events) == 1
    e = events[0]
    assert isinstance(e, AuditEvent)
    assert e.event_type == "search" and e.profile_id == "roy"
    assert e.tool == "search_knowledge_base"
    assert e.ts  # a timestamp was recorded


def test_recent_is_newest_first_and_limited(audit):
    for i in range(5):
        audit.record("search", profile_id="p", tool=f"t{i}")
    recent = audit.recent(limit=3)
    assert len(recent) == 3
    assert [e.tool for e in recent] == ["t4", "t3", "t2"]  # newest first


def test_recent_filters_by_event_type(audit):
    audit.record("search", profile_id="p", tool="s")
    audit.record_denied_capability(profile_id="p", tool="delete_collection")
    denied = audit.recent(event_type="denied_capability")
    assert [e.event_type for e in denied] == ["denied_capability"]
    assert denied[0].tool == "delete_collection"


def test_denied_capability_and_failed_scope_helpers(audit):
    audit.record_denied_capability(profile_id="bot", tool="add_project")
    audit.record_failed_scope(profile_id="bot", requested=["foreign"])
    kinds = {e.event_type for e in audit.recent()}
    assert kinds == {"denied_capability", "failed_scope"}
    scope_evt = audit.recent(event_type="failed_scope")[0]
    assert "foreign" in (scope_evt.detail or "")


def test_persists_across_reopen(tmp_path):
    path = str(tmp_path / "a.db")
    AuditLog(path).record_denied_capability(profile_id="p", tool="x")
    assert len(AuditLog(path).recent()) == 1
