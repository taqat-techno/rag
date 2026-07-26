"""Cross-project isolation is a security boundary now, not a filter.

Under the shared-collection model, scoping was a payload filter: every
project's vectors lived in one collection and a bug in the filter returned the
wrong rows. Under per-project collections the other project's collection is
never queried, so the same bug yields an empty result instead of a leak.

That is a real improvement, but only if the whole chain holds:

    client profile -> authorize_projects -> router.read_collections -> Qdrant

These tests exercise that chain end to end with real vectors, including the
framework case (a shared corpus a project legitimately reads) and the
denied case (a project the profile was never granted).

Plan: docs/planning/RAG_COLLECTION_ARCHITECTURE_IMPLEMENTATION_PLAN.md (W8, W12)
"""

import tempfile
from pathlib import Path

import pytest

from ragtools.collection_router import UnknownProject
from ragtools.config import ProjectConfig, Settings
from ragtools.profiles import ClientProfile, ScopeDenied, authorize_projects
from ragtools.service.owner import QdrantOwner


@pytest.fixture
def owner():
    """Two projects with disjoint vocabulary, under per-project collections."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        finance = root / "finance"
        medical = root / "medical"
        finance.mkdir()
        medical.mkdir()
        (finance / "ledger.md").write_text(
            "# Ledger\n\nQuarterly revenue is posted to the general ledger.\n"
            "The audit trail records every journal entry.\n", encoding="utf-8")
        (medical / "triage.md").write_text(
            "# Triage\n\nPatients are triaged by acuity on arrival.\n"
            "Vital signs are recorded at intake.\n", encoding="utf-8")

        settings = Settings(
            content_root=str(root),
            qdrant_path=str(root / "qdrant"),
            state_db=str(root / "state.db"),
            data_dir=str(root / "data"),
            collection_strategy="per_project",
            projects=[
                ProjectConfig(id="finance", path=str(finance), mode="docs"),
                ProjectConfig(id="medical", path=str(medical), mode="docs"),
            ],
        )
        o = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        o.run_full_index()
        yield o
        o.close()


def _profile(pid, projects):
    """A client profile granted read access to `projects` only."""
    return ClientProfile(
        profile_id=pid,
        allowed_projects=None if projects is None else frozenset(projects),
        capability_groups=frozenset({"retrieval"}),
        destructive_policy="forbidden",
    )


# --- the boundary --------------------------------------------------------


def test_a_scoped_profile_cannot_reach_another_projects_collection(owner):
    """The whole point: not filtered out — never queried."""
    profile = _profile("finance-bot", ["finance"])
    granted = authorize_projects(profile, ["finance"])

    collections = owner.router.read_collections(project_ids=granted)
    medical_collection = owner.router.write_collection("medical")
    assert medical_collection not in collections

    hits = owner.search("patient triage acuity vital signs",
                        project_ids=granted, score_threshold=0.0, top_k=20)
    assert all(h.project_id == "finance" for h in hits), \
        f"leaked medical content: {[(h.project_id, h.file_path) for h in hits]}"


def test_naming_a_foreign_project_is_dropped_before_it_reaches_the_router(owner):
    """authorize_projects drops it; the router therefore never sees it."""
    profile = _profile("finance-bot", ["finance"])
    granted = authorize_projects(profile, ["finance", "medical"])
    assert granted == ["finance"]

    collections = owner.router.read_collections(project_ids=granted)
    assert owner.router.write_collection("medical") not in collections


def test_requesting_only_a_foreign_project_is_denied_outright(owner):
    profile = _profile("finance-bot", ["finance"])
    with pytest.raises(ScopeDenied):
        authorize_projects(profile, ["medical"])


def test_an_unscoped_request_from_a_multi_project_profile_is_refused(owner):
    """No accidental global read."""
    profile = _profile("wide-bot", ["finance", "medical"])
    with pytest.raises(ScopeDenied):
        authorize_projects(profile, None)


def test_the_owner_profile_still_reaches_everything(owner):
    """allowed_projects=None is the owner — full access is intended."""
    profile = _profile("owner", None)
    granted = authorize_projects(profile, ["finance", "medical"])
    collections = owner.router.read_collections(project_ids=granted)
    assert owner.router.write_collection("finance") in collections
    assert owner.router.write_collection("medical") in collections


def test_an_unregistered_project_cannot_be_smuggled_in(owner):
    """A granted-but-unknown id must raise, not silently widen to shared."""
    profile = _profile("ghost-bot", ["not-a-project"])
    granted = authorize_projects(profile, ["not-a-project"])
    with pytest.raises(UnknownProject):
        owner.router.read_collections(project_ids=granted)


# --- frameworks are shared on purpose, project content is not ------------


def test_a_framework_corpus_is_reachable_only_by_projects_that_link_it(owner):
    from ragtools.indexing.indexer import ensure_collection, index_file

    fw_root = Path(owner.settings.data_dir) / "vendor"
    fw_root.mkdir(parents=True, exist_ok=True)
    doc = fw_root / "sdk.md"
    doc.write_text("# SDK\n\nThe transport layer retries idempotent calls.\n",
                   encoding="utf-8")

    rec, _ = owner.framework_registry.register(
        name="acme-sdk", version="2.1", edition="oss", build_id="bid-1",
        canonical_root=str(fw_root))
    ensure_collection(owner.client, rec.collection_name, owner.encoder.dimension)
    index_file(client=owner.client, encoder=owner.encoder,
               collection_name=rec.collection_name, project_id="acme-sdk",
               file_path=doc, relative_path="sdk.md",
               chunk_size=owner.settings.chunk_size,
               chunk_overlap=owner.settings.chunk_overlap)

    owner.framework_registry.link(owner.registry.get("finance").uuid,
                                  rec.collection_name)

    finance_reads = owner.router.read_collections("finance")
    medical_reads = owner.router.read_collections("medical")
    assert rec.collection_name in finance_reads
    assert rec.collection_name not in medical_reads, \
        "an unlinked project can read a framework corpus"


def test_a_shared_framework_does_not_become_a_bridge_between_projects(owner):
    """Two projects may share a framework WITHOUT seeing each other's code —
    the corpus is shared, the project collections are not."""
    from ragtools.indexing.indexer import ensure_collection, index_file

    fw_root = Path(owner.settings.data_dir) / "vendor2"
    fw_root.mkdir(parents=True, exist_ok=True)
    doc = fw_root / "core.md"
    doc.write_text("# Core\n\nSchedulers dispatch work to a bounded pool.\n",
                   encoding="utf-8")
    rec, _ = owner.framework_registry.register(
        name="core-fw", version="1.0", edition="oss", build_id="bid-2",
        canonical_root=str(fw_root))
    ensure_collection(owner.client, rec.collection_name, owner.encoder.dimension)
    index_file(client=owner.client, encoder=owner.encoder,
               collection_name=rec.collection_name, project_id="core-fw",
               file_path=doc, relative_path="core.md",
               chunk_size=owner.settings.chunk_size,
               chunk_overlap=owner.settings.chunk_overlap)

    for pid in ("finance", "medical"):
        owner.framework_registry.link(owner.registry.get(pid).uuid,
                                      rec.collection_name)

    # finance sees: its own + the framework. NOT medical's.
    reads = owner.router.read_collections("finance")
    assert owner.router.write_collection("medical") not in reads

    hits = owner.search("patient triage acuity", project_id="finance",
                        score_threshold=0.0, top_k=20)
    assert not any(h.project_id == "medical" for h in hits), \
        "the shared framework became a bridge between two projects"


# --- credentials ---------------------------------------------------------


def test_storage_api_key_is_never_returned_by_the_config_endpoint():
    """/api/config is readable by anything that can reach the loopback port."""
    from starlette.testclient import TestClient

    from ragtools.service import app as app_module
    from ragtools.service.app import create_app

    with tempfile.TemporaryDirectory() as td:
        settings = Settings(
            qdrant_path=str(Path(td) / "q"),
            state_db=str(Path(td) / "s.db"),
            data_dir=str(Path(td) / "d"),
            storage_api_key="super-secret-key-value",
        )
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        app_module._owner = owner
        app_module._settings = settings
        try:
            with TestClient(create_app()) as c:
                body = c.get("/api/config").text
            assert "super-secret-key-value" not in body
            assert "storage_api_key" not in body
        finally:
            app_module._owner = None
            app_module._settings = None
            owner.close()
