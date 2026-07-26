"""Automatic onboarding — automation policy + approval gate (RAG v3, Stage S14).

Adding a project should require no hand-editing of TOML, collection names, vector
dimensions, or ignore rules. But automation has a hard line, and this module is
where the line lives (§15.2):

    read or propose  -> automatic
    write vectors / spend real time / change scope -> owner approval + visible dry-run
    modify another client's profile -> owner-only, never an agent action

The safety core is pure: the per-operation policy table, the
``confirm_token == project_id`` guard extended to destructive onboarding ops
(evaluated BEFORE any cooldown or network call — the ordering existing tests
pin), and the state-machine gate that will not advance past OWNER APPROVAL
without it. The analyze/detect/classify *implementations* (which need framework
adapters and real file walks) layer on top; the policy that governs them does
not.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S14 -> G14)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConfirmTokenError(PermissionError):
    """A destructive onboarding op was invoked without the matching token."""


class OnboardingStep(Enum):
    """The onboarding flow (§15.1). OWNER APPROVAL sits between propose and apply."""

    ANALYZE = "analyze"
    DETECT = "detect"
    CLASSIFY = "classify"
    MATCH = "match"
    PROPOSE = "propose"
    PREVIEW = "preview"
    APPROVAL = "approval"
    APPLY = "apply"
    INDEX = "index"
    WATCHER = "watcher"
    VALIDATE = "validate"
    REPORT = "report"


_ORDER = list(OnboardingStep)


@dataclass(frozen=True)
class OperationPolicy:
    """How a single onboarding operation may run."""

    automatic: bool
    dry_run_shown: bool
    approval_required: bool
    never_automatic: bool = False
    owner_only: bool = False


def _auto(dry_run: bool = False) -> OperationPolicy:
    return OperationPolicy(automatic=True, dry_run_shown=dry_run, approval_required=False)


def _approval() -> OperationPolicy:
    # Writes vectors / spends time / changes scope: never automatic, always with
    # a visible dry-run before the owner approves.
    return OperationPolicy(automatic=False, dry_run_shown=True, approval_required=True)


#: The §15.2 policy table — the one place the automation line is drawn.
_OP_POLICY: dict[str, OperationPolicy] = {
    # read-only analysis
    "analyze_project": _auto(),
    "detect_framework": _auto(),
    "detect_dependencies": _auto(),
    "classify_sources": _auto(),
    # proposals — automatic, but they PRODUCE the dry-run the owner reviews
    "propose_project_config": _auto(dry_run=True),
    "preview_index_plan": _auto(dry_run=True),
    # post-approval / validation — automatic
    "index_after_approval": _auto(),
    "validate_project_config": _auto(),
    "test_retrieval_quality": _auto(),
    "detect_config_drift": _auto(),
    # writes vectors / changes scope — approval + dry-run
    "apply_project_config": _approval(),
    "link_framework": _approval(),
    "create_collection": _approval(),
    "repair_config_drift": _approval(),
    "delete_collection": _approval(),
    "reindex_shared_framework": _approval(),
    "change_embedding_model": _approval(),
    # owner-only, never an agent action
    "modify_client_profile": OperationPolicy(
        automatic=False, dry_run_shown=False, approval_required=False,
        never_automatic=True, owner_only=True,
    ),
}


def policy_for(op: str) -> OperationPolicy:
    """The :class:`OperationPolicy` for ``op``; ``KeyError`` if unknown.

    An unknown operation is a ``KeyError``, not a permissive default — an
    unrecognised op must never fall through to "automatic".
    """
    return _OP_POLICY[op]


def may_run_automatically(op: str) -> bool:
    """True iff ``op`` may execute with no owner approval."""
    p = policy_for(op)
    return p.automatic and not p.never_automatic


def requires_approval(op: str) -> bool:
    """True iff ``op`` needs explicit owner approval before it runs."""
    return policy_for(op).approval_required


def is_owner_only(op: str) -> bool:
    """True iff ``op`` is owner-only and never an agent action."""
    return policy_for(op).owner_only


def confirm_token_for(project_id: str) -> str:
    """The confirm token for a project — its id (the preserved convention)."""
    return project_id


def check_confirm_token(op: str, *, project_id: str, token: object) -> None:
    """Guard a destructive onboarding op with ``confirm_token == project_id``.

    Call this FIRST — before any cooldown check or network call — so a missing
    or wrong token is refused before side effects can begin. Operations that do
    not require approval need no token and pass through.
    """
    if not policy_for(op).approval_required:
        return
    if token != confirm_token_for(project_id):
        raise ConfirmTokenError(
            f"{op} on {project_id!r} requires confirm_token == project_id"
        )


def advance(step: OnboardingStep, *, approved: bool) -> OnboardingStep:
    """Return the next onboarding step, enforcing the approval gate.

    Advancing *out of* APPROVAL requires ``approved`` — otherwise
    :class:`PermissionError`. Every other transition is automatic.
    """
    if step is OnboardingStep.APPROVAL and not approved:
        raise PermissionError("cannot advance past OWNER APPROVAL without approval")
    idx = _ORDER.index(step)
    if idx + 1 >= len(_ORDER):
        return step  # REPORT is terminal
    return _ORDER[idx + 1]
