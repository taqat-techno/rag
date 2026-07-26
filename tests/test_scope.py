"""S1 / A2 — fail-closed scope resolution.

The v2.7.0 searcher turned an *empty* or *absent* project scope into a global
search (``Filter(should=should or None)`` collapses ``[]`` to "no constraint").
Reproduced live: an unscoped Odoo query returned a KhairGate chunk. This pins
the invariant that a requested-but-empty scope, or an unscoped call without
explicit opt-in, FAILS CLOSED — it never silently widens to every project.

Pure, Qdrant-free unit so the contract is testable without any index.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S1/A2 -> G1)
"""

import pytest

from ragtools.retrieval.scope import ScopeUnresolvedError, resolve_scope


# --- resolves a real scope ----------------------------------------------


def test_single_project_resolves():
    d = resolve_scope(project_id="alpha", project_ids=None)
    assert d.project_ids == ("alpha",)
    assert d.unscoped is False


def test_multi_project_resolves_and_preserves_order():
    d = resolve_scope(project_id=None, project_ids=["a", "b", "c"])
    assert d.project_ids == ("a", "b", "c")
    assert d.unscoped is False


def test_multi_project_strips_blanks():
    d = resolve_scope(project_id=None, project_ids=["a", "", "  ", "b"])
    assert d.project_ids == ("a", "b")


def test_project_ids_take_precedence_over_project_id():
    d = resolve_scope(project_id="ignored", project_ids=["a", "b"])
    assert d.project_ids == ("a", "b")


# --- the dangerous silent cases now FAIL CLOSED --------------------------


def test_empty_project_ids_list_refuses():
    """A scope was requested (``[]``) but is empty — must refuse, not widen."""
    with pytest.raises(ScopeUnresolvedError):
        resolve_scope(project_id=None, project_ids=[])


def test_blank_only_project_ids_refuses():
    with pytest.raises(ScopeUnresolvedError):
        resolve_scope(project_id=None, project_ids=["", "  "])


def test_blank_project_id_refuses():
    with pytest.raises(ScopeUnresolvedError):
        resolve_scope(project_id="   ", project_ids=None)


def test_unscoped_without_optin_refuses():
    """Both absent + no explicit opt-in — refuse (this was the global leak)."""
    with pytest.raises(ScopeUnresolvedError):
        resolve_scope(project_id=None, project_ids=None)


def test_refusal_message_is_actionable():
    with pytest.raises(ScopeUnresolvedError, match="(?i)scope"):
        resolve_scope(project_id=None, project_ids=None)


# --- explicit global is the ONLY way to search everything ---------------


def test_unscoped_with_explicit_optin_is_allowed():
    d = resolve_scope(project_id=None, project_ids=None, allow_unscoped=True)
    assert d.unscoped is True
    assert d.project_ids == ()


def test_optin_does_not_rescue_a_requested_but_empty_scope():
    """opt-in sanctions only the no-arg case, never an empty requested list."""
    with pytest.raises(ScopeUnresolvedError):
        resolve_scope(project_id=None, project_ids=[], allow_unscoped=True)
    with pytest.raises(ScopeUnresolvedError):
        resolve_scope(project_id="", project_ids=None, allow_unscoped=True)
