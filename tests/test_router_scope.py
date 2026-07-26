"""S10 §16 — the ResolvedScope resolution matrix (server-side, fail-closed).

Every search resolves to a scope computed from the CLIENT PROFILE, never from a
caller-supplied project/collection. §16.1's non-negotiables: an unresolved scope
is refused (never widened); ``linked`` frameworks come from the profile+links,
not the caller; a requested-but-foreign project lands in ``refused``, never in
``primary``/``linked`` (zero cross-project leakage). This pins that matrix; the
fan-out/fusion half is separate.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S10 §16 -> G10)
"""

import pytest

from ragtools.profiles import ClientProfile, ScopeDenied
from ragtools.retrieval.router import ResolvedScope, resolve_scope


def _profile(**kw) -> ClientProfile:
    base = dict(
        profile_id="p",
        allowed_projects=None,  # None = owner/all
        capability_groups=frozenset({"retrieval"}),
    )
    base.update(kw)
    return ClientProfile(**base)


# --- resolution: explicit / inferred / unresolved -----------------------


def test_explicit_scope_when_project_requested():
    s = resolve_scope(_profile(allowed_projects=frozenset({"a"})), ["a"])
    assert isinstance(s, ResolvedScope)
    assert s.resolution == "explicit"
    assert s.primary == ["a"]
    assert s.refused == []


def test_inferred_scope_for_single_project_profile():
    s = resolve_scope(_profile(allowed_projects=frozenset({"solo"})), None)
    assert s.resolution == "inferred"
    assert s.primary == ["solo"]


def test_unresolved_scope_is_refused_never_widened():
    # Owner profile, no project named -> cannot infer -> refuse (never "all").
    with pytest.raises(ScopeDenied):
        resolve_scope(_profile(allowed_projects=None), None)
    # Multi-project profile, unscoped -> also refuse.
    with pytest.raises(ScopeDenied):
        resolve_scope(_profile(allowed_projects=frozenset({"a", "b"})), None)


# --- zero cross-project leakage -----------------------------------------


def test_foreign_project_is_refused_not_leaked():
    s = resolve_scope(_profile(allowed_projects=frozenset({"mine"})), ["mine", "foreign"])
    assert s.primary == ["mine"]
    refused_refs = [r for r, _reason in s.refused]
    assert "foreign" in refused_refs
    # The foreign project appears NOWHERE searchable.
    assert "foreign" not in s.primary
    assert "foreign" not in s.linked
    assert "foreign" not in s.cross_project


def test_refused_entries_carry_a_reason():
    s = resolve_scope(_profile(allowed_projects=frozenset({"mine"})), ["mine", "x"])
    assert all(reason for _ref, reason in s.refused)


# --- linked frameworks come from the profile, not the caller ------------


def test_linked_frameworks_resolved_from_links_not_request():
    s = resolve_scope(
        _profile(allowed_projects=frozenset({"mine"})),
        ["mine"],
        links_by_project={"mine": ["fw_odoo_build123"]},
    )
    assert s.linked == ["fw_odoo_build123"]


def test_linked_frameworks_dedup_across_projects():
    s = resolve_scope(
        _profile(allowed_projects=None),  # owner
        ["a", "b"],
        links_by_project={"a": ["fw_shared"], "b": ["fw_shared"]},
    )
    assert s.linked == ["fw_shared"]  # one framework corpus, indexed once


def test_caller_cannot_inject_a_framework_via_request():
    # A framework-looking string in the request is treated as a project id and
    # dropped as foreign — it never becomes a linked collection.
    s = resolve_scope(
        _profile(allowed_projects=frozenset({"mine"})),
        ["mine", "fw_evil"],
        links_by_project={"mine": []},
    )
    assert "fw_evil" not in s.linked
    assert "fw_evil" in [r for r, _ in s.refused]


# --- cross-project only on explicit authorized intent -------------------


def test_cross_project_populated_only_on_explicit_multi():
    s = resolve_scope(_profile(allowed_projects=None), ["a", "b"])  # owner, explicit
    assert s.primary == ["a"]
    assert s.cross_project == ["b"]


def test_scope_echoes_profile_id():
    s = resolve_scope(_profile(profile_id="roy", allowed_projects=frozenset({"a"})), ["a"])
    assert s.client_profile_id == "roy"
    assert s.degraded == []  # fan-out populates this, resolution does not
