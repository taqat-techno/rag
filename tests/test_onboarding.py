"""S14 — automatic onboarding: the automation-policy table and the approval
gate (pure safety core).

The live install shows why this matters: projects added *blind* left "12 of 15
with empty ignore_patterns and empty dependency_paths" and two projects holding
83% of the index. §15.2 draws the line — anything that only reads or proposes is
automatic; anything that writes vectors, spends real time, or changes scope
needs owner approval with a visible dry-run; modifying another client's profile
is owner-only and never an agent action.

This pins that policy table, the ``confirm_token == project_id`` guard extended
to destructive onboarding ops (checked BEFORE any cooldown or network call — the
ordering existing tests already pin), and the state-machine gate that refuses to
advance past APPROVAL without it.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S14 -> G14)
"""

import pytest

from ragtools.onboarding import (
    ConfirmTokenError,
    OnboardingStep,
    advance,
    check_confirm_token,
    confirm_token_for,
    is_owner_only,
    may_run_automatically,
    policy_for,
    requires_approval,
)


READ_OR_PROPOSE = [
    "analyze_project", "detect_framework", "detect_dependencies",
    "classify_sources", "propose_project_config", "preview_index_plan",
    "validate_project_config", "test_retrieval_quality", "detect_config_drift",
    "index_after_approval",
]

WRITE_OR_SCOPE = [
    "apply_project_config", "link_framework", "create_collection",
    "repair_config_drift", "delete_collection", "reindex_shared_framework",
    "change_embedding_model",
]


# --- automation policy (§15.2) ------------------------------------------


@pytest.mark.parametrize("op", READ_OR_PROPOSE)
def test_read_or_propose_ops_run_automatically(op):
    assert may_run_automatically(op) is True
    assert requires_approval(op) is False
    assert is_owner_only(op) is False


@pytest.mark.parametrize("op", WRITE_OR_SCOPE)
def test_write_or_scope_ops_need_approval_with_dry_run(op):
    p = policy_for(op)
    assert p.approval_required is True
    assert p.dry_run_shown is True          # approval is never blind
    assert may_run_automatically(op) is False


def test_propose_and_preview_are_automatic_but_show_a_dry_run():
    assert policy_for("propose_project_config").automatic is True
    assert policy_for("preview_index_plan").dry_run_shown is True


def test_modifying_another_profile_is_owner_only_never_agent():
    p = policy_for("modify_client_profile")
    assert is_owner_only("modify_client_profile") is True
    assert p.never_automatic is True
    assert may_run_automatically("modify_client_profile") is False


def test_unknown_operation_is_rejected():
    with pytest.raises(KeyError):
        policy_for("no_such_op")


# --- confirm_token == project_id, checked first -------------------------


def test_confirm_token_is_the_project_id():
    assert confirm_token_for("royal-preps") == "royal-preps"


def test_destructive_op_requires_matching_token():
    check_confirm_token("delete_collection", project_id="p", token="p")  # ok
    with pytest.raises(ConfirmTokenError):
        check_confirm_token("delete_collection", project_id="p", token="wrong")
    with pytest.raises(ConfirmTokenError):
        check_confirm_token("delete_collection", project_id="p", token=None)


def test_read_only_op_needs_no_token():
    # A non-approval op must not demand a confirm token.
    check_confirm_token("analyze_project", project_id="p", token=None)  # no raise


# --- approval gate ------------------------------------------------------


def test_gate_refuses_to_advance_past_approval_without_approval():
    with pytest.raises(PermissionError):
        advance(OnboardingStep.APPROVAL, approved=False)


def test_gate_advances_to_apply_once_approved():
    assert advance(OnboardingStep.APPROVAL, approved=True) is OnboardingStep.APPLY


def test_automatic_steps_advance_without_approval():
    # Steps before the gate flow automatically.
    assert advance(OnboardingStep.ANALYZE, approved=False) is OnboardingStep.DETECT
