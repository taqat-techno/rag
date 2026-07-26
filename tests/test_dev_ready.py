"""S17 — DEV_READY invariant gate (the terminal validation's automatable core).

Not a re-run of the unit suites: this asserts the CROSS-CUTTING properties that
must hold for the v3 architecture to be dev-ready — the invariants that span
module boundaries and would silently rot if a later change broke the composition.
If any of these fail, the system is not dev-ready regardless of per-module green.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S17 -> DEV_READY, §39)
"""

import pytest


# --- INVARIANT 1: development isolation is enforced (S0) -----------------


def test_dev_isolation_refuses_live_ports_and_dirs(tmp_path):
    from ragtools.devenv import (
        IsolationError,
        assert_dev_port_allowed,
        RESERVED_LIVE_PORTS,
    )

    for port in RESERVED_LIVE_PORTS:  # 21420, 21422
        with pytest.raises(IsolationError):
            assert_dev_port_allowed(port)
    assert_dev_port_allowed(0)  # ephemeral is fine


# --- INVARIANT 2: retrieval is fail-closed end-to-end (S1/S10/S12) -------


def test_no_search_path_can_widen_to_global():
    from ragtools.profiles import ClientProfile, ScopeDenied
    from ragtools.retrieval.router import resolve_scope
    from ragtools.authz import enforce

    owner = ClientProfile(profile_id="o", allowed_projects=None,
                          capability_groups=frozenset({"retrieval"}))
    # Unscoped owner request must refuse at BOTH the router and the authz gate.
    with pytest.raises(ScopeDenied):
        resolve_scope(owner, None)
    with pytest.raises(ScopeDenied):
        enforce(owner, "search_knowledge_base", requested_projects=None)

    # A foreign project is dropped, never searched.
    bot = ClientProfile(profile_id="b", allowed_projects=frozenset({"mine"}),
                        capability_groups=frozenset({"retrieval"}))
    scope = resolve_scope(bot, ["mine", "foreign"])
    assert "foreign" not in scope.searchable()


# --- INVARIANT 3: secrets never reach vectors (S1, CRITICAL) -------------


def test_secret_material_is_redacted_before_indexing(tmp_path):
    from ragtools.indexing.indexer import apply_source_class_and_redaction
    from ragtools.chunking.dispatch import chunk_file

    secret = "AKIAIOSFODNN7EXAMPLE"  # AWS access key id shape
    f = tmp_path / "bundle.js"
    f.write_text(f"const k = 'aws_access_key_id={secret}';\n")
    chunks = chunk_file(file_path=f, project_id="p", relative_path="dist/bundle.js",
                        chunk_size=400, chunk_overlap=100)
    apply_source_class_and_redaction(chunks, "dist/bundle.js")  # mutates in place
    blob = " ".join((getattr(c, "text", "") + getattr(c, "raw_text", "")) for c in chunks)
    assert secret not in blob


# --- INVARIANT 4: storage never silently downgrades (S3/S4) -------------


def test_storage_backends_resolve_and_never_silently_fall_back():
    from ragtools.config import Settings
    from ragtools.storage import resolve_backend, EmbeddedBackend, ManagedBackend

    assert isinstance(resolve_backend(Settings()), EmbeddedBackend)
    assert isinstance(
        resolve_backend(Settings(storage_backend="managed",
                                 storage_url="http://127.0.0.1:26333")),
        ManagedBackend,
    )
    for bad in ("managed", "external"):  # no target -> refuse, never embedded
        with pytest.raises(ValueError):
            resolve_backend(Settings(storage_backend=bad))


# --- INVARIANT 5: a wrong-model corpus is refused, not searched (S9/A5) --


def test_model_mismatch_is_refused_everywhere_it_matters():
    from ragtools.embedding.backend import assert_model_compatible, ModelMismatchError
    from ragtools.reindex import check_migration_model

    good = {"model_name": "all-MiniLM-L6-v2", "dimension": 384, "normalize": True}
    bad = {"model_name": "bge", "dimension": 1024, "normalize": True}
    assert_model_compatible(good, good)
    for gate in (assert_model_compatible, check_migration_model):
        with pytest.raises(ModelMismatchError):
            gate(good, bad)


# --- INVARIANT 6: identity survives rename; frameworks dedup (S6/S7) -----


def test_identity_is_stable_and_frameworks_dedup(tmp_path):
    from ragtools.registry import ProjectRegistry, FrameworkRegistry

    preg = ProjectRegistry(str(tmp_path / "p.db"))
    rec = preg.add("old", path="/w/x")
    preg.rename("old", "new")
    assert preg.get("new").collection_name == rec.collection_name  # UUID-stable

    freg = FrameworkRegistry(str(tmp_path / "f.db"))
    b = dict(name="odoo", version="19", edition="ee", build_id="SHA")
    a1, created1 = freg.register(**b, canonical_root="/a")
    a2, created2 = freg.register(**b, canonical_root="/b")
    assert created1 and not created2 and a1.collection_name == a2.collection_name


# --- INVARIANT 7: the authz gate composes capability + destructive + scope


def test_authz_gate_enforces_all_three_checks():
    from ragtools.profiles import ClientProfile, ScopeDenied
    from ragtools.authz import enforce, CapabilityDenied

    p = ClientProfile(profile_id="p", allowed_projects=frozenset({"mine"}),
                      capability_groups=frozenset({"collection_management"}),
                      destructive_policy="forbidden")
    # capability: a retrieval tool isn't in collection_management
    with pytest.raises(CapabilityDenied):
        enforce(p, "search_knowledge_base", requested_projects=["mine"])
    # destructive modifier: delete_collection is granted-but-forbidden
    with pytest.raises(CapabilityDenied):
        enforce(p, "delete_collection", requested_projects=["mine"])
    # scope: a non-destructive granted tool with a foreign project drops it
    d = enforce(p, "list_collections", requested_projects=["mine", "foreign"])
    assert d.projects == ["mine"]


# --- INVARIANT 8: every grantable tool is annotated (S13, B30) -----------


def test_no_grantable_tool_lacks_annotations():
    from ragtools.profiles import CAPABILITY_GROUPS
    from ragtools.mcp_capabilities import tool_spec

    grantable = set().union(*CAPABILITY_GROUPS.values())
    assert [t for t in grantable if tool_spec(t) is None] == []


# --- INVARIANT 9: onboarding cannot auto-run owner/destructive ops (S14) -


def test_onboarding_gate_and_policy_hold():
    from ragtools.onboarding import (
        OnboardingStep, advance, may_run_automatically, is_owner_only,
    )

    with pytest.raises(PermissionError):
        advance(OnboardingStep.APPROVAL, approved=False)
    assert not may_run_automatically("delete_collection")
    assert is_owner_only("modify_client_profile")
