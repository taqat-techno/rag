"""S6/S7 — project & framework identity: one shared validator, UUID-derived
collection names, and build-id framework dedup (pure core).

Three real divergences exist today (plan §11.1): HTTP validates
``^[a-z0-9][a-z0-9_-]*$``, the CLI *generates* ``[a-z0-9-]`` only, and the CLI
offline branch validates nothing. This pins ONE validator whose accepted set is
a superset of what the ONE generator produces — so a generated id always
validates — plus the deterministic collection-name derivations S6/S7 require:
identity is a stable UUID (rename/move safe), and a framework corpus dedups on
its build id.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S6->G6, S7->G7)
"""

import re

import pytest

from ragtools.identity import (
    InvalidProjectId,
    framework_collection_name,
    is_valid_project_id,
    project_collection_name,
    slugify_project_id,
    validate_project_id,
)

_COLLECTION_TOKEN = re.compile(r"^[a-z0-9_]+$")


# --- one shared project-id validator ------------------------------------


def test_valid_ids_accepted_and_returned_normalized():
    assert validate_project_id("royal-preps") == "royal-preps"
    assert validate_project_id("  royal-preps  ") == "royal-preps"  # trimmed
    assert validate_project_id("relief_center_19") == "relief_center_19"  # underscore ok


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "Royal-Preps",   # uppercase
        "-leading",       # must start alphanumeric
        "_leading",
        "has space",
        "slash/es",
        "dot.dot",
        "x" * 65,         # too long
    ],
)
def test_invalid_ids_rejected(bad):
    with pytest.raises(InvalidProjectId):
        validate_project_id(bad)
    assert not is_valid_project_id(bad)


def test_is_valid_matches_validate():
    assert is_valid_project_id("ok-1")
    assert not is_valid_project_id("Bad")


# --- the generator, and the consistency property it must satisfy --------


def test_slugify_produces_canonical_ids():
    assert slugify_project_id("Royal Preps") == "royal-preps"
    assert slugify_project_id("a__b!!c") == "a-b-c"      # runs collapse to one hyphen
    assert slugify_project_id("---Trim---") == "trim"    # edges stripped
    assert slugify_project_id("!!!") == ""               # no alphanumerics


@pytest.mark.parametrize(
    "raw",
    ["Royal Preps", "Some Weird Name!!", "Odoo 19 (EE)", "a__b--c", "Über Café 2"],
)
def test_generated_id_always_validates(raw):
    # The whole point of S6: what the generator emits, the validator accepts.
    slug = slugify_project_id(raw)
    if slug:  # empty slug is the generator's documented "caller decides" signal
        assert validate_project_id(slug) == slug


# --- S6: UUID-derived project collection names --------------------------


def test_project_collection_name_is_stable_and_uuid_derived():
    u = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    assert project_collection_name(u) == project_collection_name(u)  # deterministic
    assert _COLLECTION_TOKEN.match(project_collection_name(u))       # valid token
    assert project_collection_name(u).startswith("proj_")


def test_project_collection_name_independent_of_display_id_and_path():
    # Rename/move safety: identity is the UUID, nothing else. Two projects with
    # the same UUID but different display ids/paths share the collection; two
    # different UUIDs never collide.
    u1 = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    u2 = "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
    assert project_collection_name(u1) != project_collection_name(u2)


# --- S7: framework corpus dedups on build identity ----------------------


def test_same_build_id_dedups_to_one_collection():
    # Two trees sharing a repos_heads SHA are the same build -> one collection,
    # even if a weaker signal (version string) is reported differently.
    a = framework_collection_name("odoo", version="19.0", edition="enterprise",
                                  build_id="abc123def456")
    b = framework_collection_name("odoo", version="19.0.1", edition="enterprise",
                                  build_id="abc123def456")
    assert a == b
    assert _COLLECTION_TOKEN.match(a)
    assert a.startswith("fw_")


def test_different_build_id_gets_its_own_collection():
    a = framework_collection_name("odoo", version="19.0", edition="enterprise",
                                  build_id="abc123def456")
    c = framework_collection_name("odoo", version="19.0", edition="enterprise",
                                  build_id="999888777666")
    assert a != c


def test_absent_build_id_distinguishes_checkout_from_packaged():
    # §12.3: the *absence* of repos_heads distinguishes a git checkout from a
    # packaged build. Same framework, one with a build id and one without ->
    # different collections.
    packaged = framework_collection_name("odoo", version="19.0", edition="community",
                                         build_id="abc123def456")
    checkout = framework_collection_name("odoo", version="19.0", edition="community",
                                         build_id=None)
    assert packaged != checkout


def test_absent_build_id_falls_back_to_stable_tuple():
    a = framework_collection_name("odoo", version="19.0", edition="community",
                                  build_id=None)
    b = framework_collection_name("odoo", version="19.0", edition="community",
                                  build_id=None)
    assert a == b                                       # stable
    # edition is part of the fallback identity
    ee = framework_collection_name("odoo", version="19.0", edition="enterprise",
                                   build_id=None)
    assert a != ee
