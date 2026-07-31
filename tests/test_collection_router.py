"""The one seam that decides which collection(s) a call touches.

Before this, `settings.collection_name` was read at 38 sites across 11 files —
so "which collection" was a decision re-made, and re-hardcoded, everywhere.
The router makes it one decision with two strategies:

* ``shared``      — every answer is ``settings.collection_name`` (v2, byte-identical)
* ``per_project`` — the registry's UUID-derived collection, plus any linked
                    framework corpora on the read path

The security-critical invariant: in ``per_project`` an unknown project is a
**KeyError**, never a fallback to the shared collection. A silent fallback
would answer project A's query out of project B's vectors.

Plan: docs/planning/RAG_COLLECTION_ARCHITECTURE_IMPLEMENTATION_PLAN.md (W1)
"""

import pytest

from ragtools.collection_router import CollectionRouter, UnknownProject
from ragtools.config import ProjectConfig, Settings
from ragtools.registry import FrameworkRegistry, ProjectRegistry


@pytest.fixture
def settings(tmp_path):
    return Settings(
        content_root=str(tmp_path),
        qdrant_path=str(tmp_path / "qdrant"),
        state_db=str(tmp_path / "state.db"),
        projects=[
            ProjectConfig(id="alpha", path=str(tmp_path / "alpha"), mode="docs"),
            ProjectConfig(id="beta", path=str(tmp_path / "beta"), mode="general"),
        ],
    )


@pytest.fixture
def registry(tmp_path):
    reg = ProjectRegistry(str(tmp_path / "registry.db"))
    reg.add("alpha", path=str(tmp_path / "alpha"), mode="docs")
    reg.add("beta", path=str(tmp_path / "beta"), mode="general")
    return reg


@pytest.fixture
def frameworks(tmp_path):
    return FrameworkRegistry(str(tmp_path / "frameworks.db"))


# --- shared strategy: the v2 behaviour, preserved by construction -------


def test_shared_routes_everything_to_the_one_collection(settings):
    r = CollectionRouter(settings)
    assert r.strategy == "shared"
    assert r.write_collection("alpha") == settings.collection_name
    assert r.write_collection("beta") == settings.collection_name
    assert r.read_collections("alpha") == [settings.collection_name]
    assert r.all_collections() == [settings.collection_name]


def test_shared_does_not_need_a_registry(settings):
    """No registry, no lookup, no failure — the legacy path is untouched."""
    r = CollectionRouter(settings)
    assert r.write_collection("never-registered") == settings.collection_name
    assert r.read_collections(None) == [settings.collection_name]


# --- per_project strategy ----------------------------------------------


def _per_project(settings, registry, frameworks=None):
    object.__setattr__(settings, "collection_strategy", "per_project")
    return CollectionRouter(settings, registry=registry, framework_registry=frameworks)


def test_per_project_gives_each_project_its_own_collection(settings, registry):
    r = _per_project(settings, registry)
    assert r.strategy == "per_project"
    a = r.write_collection("alpha")
    b = r.write_collection("beta")
    assert a != b
    assert a.startswith("proj_") and b.startswith("proj_")
    assert set(r.all_collections()) == {a, b}


def test_unknown_project_raises_and_never_falls_back(settings, registry):
    """A silent fallback here is a cross-project data leak."""
    r = _per_project(settings, registry)
    with pytest.raises(UnknownProject):
        r.write_collection("does-not-exist")
    with pytest.raises(UnknownProject):
        r.read_collections("does-not-exist")


def test_collection_survives_rename_and_move(settings, registry, tmp_path):
    """Identity is the UUID: the collection must not change when the
    user-facing id or the folder path does."""
    r = _per_project(settings, registry)
    before = r.write_collection("alpha")

    registry.rename("alpha", "alpha-renamed")
    registry.move("alpha-renamed", str(tmp_path / "moved"))

    assert r.write_collection("alpha-renamed") == before


def test_per_project_read_includes_linked_frameworks(settings, registry, frameworks):
    r = _per_project(settings, registry, frameworks)
    rec, created = frameworks.register(
        name="odoo", version="19.0", edition="community",
        build_id="abc123", canonical_root="/srv/odoo",
    )
    assert created
    frameworks.link(registry.get("alpha").uuid, rec.collection_name)

    reads = r.read_collections("alpha")
    assert reads[0] == r.write_collection("alpha"), "own collection must come first"
    assert rec.collection_name in reads
    # beta is not linked — it must not see the framework corpus.
    assert rec.collection_name not in r.read_collections("beta")


def test_frameworks_can_be_excluded_from_a_read(settings, registry, frameworks):
    r = _per_project(settings, registry, frameworks)
    rec, _ = frameworks.register(name="odoo", version="19.0", edition="ce",
                                 build_id="b1", canonical_root="/srv/odoo")
    frameworks.link(registry.get("alpha").uuid, rec.collection_name)

    assert r.read_collections("alpha", include_frameworks=False) == [
        r.write_collection("alpha")
    ]


def test_multi_project_read_is_the_union(settings, registry):
    r = _per_project(settings, registry)
    reads = r.read_collections(project_ids=["alpha", "beta"])
    assert set(reads) == {r.write_collection("alpha"), r.write_collection("beta")}


def test_all_collections_includes_frameworks_and_is_deduplicated(
    settings, registry, frameworks
):
    r = _per_project(settings, registry, frameworks)
    rec, _ = frameworks.register(name="odoo", version="19.0", edition="ce",
                                 build_id="shared-build", canonical_root="/srv/odoo")
    # BOTH projects link the SAME framework build — the dedup that makes this
    # architecture worth the migration.
    frameworks.link(registry.get("alpha").uuid, rec.collection_name)
    frameworks.link(registry.get("beta").uuid, rec.collection_name)

    every = r.all_collections()
    assert every.count(rec.collection_name) == 1, "framework corpus counted twice"
    assert len(every) == 3  # alpha + beta + one shared framework


def test_archived_project_keeps_its_collection_but_leaves_the_active_set(
    settings, registry
):
    """archive = stop treating it as active; the vectors are NOT destroyed."""
    r = _per_project(settings, registry)
    alpha = r.write_collection("alpha")
    registry.archive("alpha")

    assert alpha not in r.active_collections()
    assert alpha in r.all_collections(), "archiving must not hide points from status"


def test_per_project_requires_a_registry(settings):
    object.__setattr__(settings, "collection_strategy", "per_project")
    with pytest.raises(ValueError, match="registry"):
        CollectionRouter(settings, registry=None)


def test_unknown_strategy_is_refused(settings, registry):
    object.__setattr__(settings, "collection_strategy", "sharded")
    with pytest.raises(ValueError, match="collection_strategy"):
        CollectionRouter(settings, registry=registry)


# --- the display label -------------------------------------------------
#
# Every reporting surface (/health, /api/status, /api/config, the diagnostics
# and config cards, MCP get_config + index_status) printed
# `settings.collection_name`. On the installed v3.4 machine that named nothing:
# 15 `proj_<uuid>` collections and no `markdown_kb`.


def test_shared_labels_the_index_with_the_collection_it_really_uses(settings):
    """Under ``shared`` the configured name IS the collection — unchanged."""
    assert CollectionRouter(settings).display_name() == settings.collection_name


def test_per_project_never_labels_the_index_with_the_legacy_name(
    settings, registry
):
    """The regression: naming a collection Qdrant does not have.

    The label must describe the collections that exist, and must not be a name
    any caller could hand back to Qdrant.
    """
    r = _per_project(settings, registry)
    label = r.display_name()
    assert settings.collection_name not in label
    assert "2 collections" in label
    assert "per_project" in label


def test_the_label_survives_a_broken_registry(settings, registry):
    """It rides on /health; a registry fault must not break the liveness probe."""
    r = _per_project(settings, registry)
    registry.close()  # every subsequent registry call now raises
    assert r.display_name() == "per_project"


# --- the leak test -----------------------------------------------------


def test_a_scoped_read_never_returns_another_projects_collection(
    settings, registry, frameworks
):
    """The invariant the whole per-project model exists to guarantee."""
    r = _per_project(settings, registry, frameworks)
    beta_collection = r.write_collection("beta")
    assert beta_collection not in r.read_collections("alpha")
    assert beta_collection not in r.read_collections(project_ids=["alpha"])
