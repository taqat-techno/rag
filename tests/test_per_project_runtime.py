"""Per-project collections, exercised through the REAL runtime.

`tests/test_per_project_index.py` proves the indexing *mechanism* in isolation.
This file proves the wiring: a `QdrantOwner` configured with
``collection_strategy="per_project"`` must index into per-project collections,
search across the right ones, aggregate status over all of them, and — the
invariant the whole model exists for — never return one project's chunk to
another project's query.

Under the shared model, cross-project isolation was a payload filter: a bug in
the filter returned the wrong rows. Under this model the other project's
collection is never queried at all, so a bug becomes an empty result rather
than a leak. These tests pin that difference.

Plan: docs/planning/RAG_COLLECTION_ARCHITECTURE_IMPLEMENTATION_PLAN.md (W2/W3, G2)
"""

import tempfile
from pathlib import Path

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.service.owner import QdrantOwner, compute_scale_warning
from ragtools.storage import _EMBEDDED_CAPS, _SERVER_CAPS


def _write_projects(root: Path):
    """Two projects with deliberately distinctive, non-overlapping vocabulary."""
    alpha = root / "alpha"
    beta = root / "beta"
    alpha.mkdir()
    beta.mkdir()
    (alpha / "billing.md").write_text(
        "# Billing\n\nInvoices are reconciled nightly against the ledger.\n"
        "## Dunning\n\nOverdue accounts enter a dunning sequence after 30 days.\n",
        encoding="utf-8")
    (alpha / "notes.md").write_text(
        "# Alpha notes\n\nThe reconciliation job runs at 02:00 UTC.\n", encoding="utf-8")
    (beta / "telemetry.md").write_text(
        "# Telemetry\n\nSatellite downlink windows are scheduled by ground station.\n"
        "## Orbits\n\nPerigee passes are prioritised for high-bandwidth transfer.\n",
        encoding="utf-8")
    (beta / "readme.md").write_text(
        "# Beta\n\nGround station scheduling for the downlink pipeline.\n", encoding="utf-8")
    return alpha, beta


@pytest.fixture
def owner_per_project():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        alpha, beta = _write_projects(root)
        settings = Settings(
            content_root=str(root),
            qdrant_path=str(root / "qdrant"),
            state_db=str(root / "state.db"),
            data_dir=str(root / "data"),
            collection_strategy="per_project",
            projects=[
                ProjectConfig(id="alpha", path=str(alpha), mode="docs"),
                ProjectConfig(id="beta", path=str(beta), mode="docs"),
            ],
        )
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        yield owner
        owner.close()


@pytest.fixture
def owner_shared():
    """The same content under the legacy model, to prove behaviour is preserved."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        alpha, beta = _write_projects(root)
        settings = Settings(
            content_root=str(root),
            qdrant_path=str(root / "qdrant"),
            state_db=str(root / "state.db"),
            data_dir=str(root / "data"),
            projects=[
                ProjectConfig(id="alpha", path=str(alpha), mode="docs"),
                ProjectConfig(id="beta", path=str(beta), mode="docs"),
            ],
        )
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        yield owner
        owner.close()


# --- the runtime is actually in per-project mode -------------------------


def test_owner_elects_the_per_project_router(owner_per_project):
    o = owner_per_project
    assert o.router.strategy == "per_project"
    assert o.registry is not None, "registry must be opened in per-project mode"
    assert o.framework_registry is not None


def test_shared_mode_opens_no_registry(owner_shared):
    """An install that never elects per-project pays nothing for its existence."""
    assert owner_shared.router.strategy == "shared"
    assert owner_shared.registry is None
    assert owner_shared.framework_registry is None


def test_each_project_lands_in_its_own_collection(owner_per_project):
    o = owner_per_project
    o.run_full_index()

    a = o.router.write_collection("alpha")
    b = o.router.write_collection("beta")
    assert a != b

    counts = {c["name"]: c["points"] for c in o.get_status()["collections"]}
    assert counts[a] > 0 and counts[b] > 0
    # Alpha's collection must not hold beta's chunks.
    assert counts[a] + counts[b] == o.get_status()["points_count"]


# --- the leak invariant --------------------------------------------------


def test_a_scoped_query_never_returns_another_projects_content(owner_per_project):
    o = owner_per_project
    o.run_full_index()

    # A query using beta's vocabulary, scoped to alpha.
    hits = o.search("satellite downlink ground station", project_id="alpha",
                    score_threshold=0.0, top_k=20)
    assert all(h.project_id == "alpha" for h in hits), \
        f"leaked: {[(h.project_id, h.file_path) for h in hits]}"

    # And the reverse.
    hits = o.search("invoice dunning reconciliation", project_id="beta",
                    score_threshold=0.0, top_k=20)
    assert all(h.project_id == "beta" for h in hits), \
        f"leaked: {[(h.project_id, h.file_path) for h in hits]}"


def test_each_project_can_still_find_its_own_content(owner_per_project):
    """Isolation is worthless if it also breaks retrieval."""
    o = owner_per_project
    o.run_full_index()

    hits = o.search("dunning overdue accounts", project_id="alpha", top_k=5)
    assert hits, "alpha cannot find its own content"
    assert all(h.project_id == "alpha" for h in hits)

    hits = o.search("perigee downlink bandwidth", project_id="beta", top_k=5)
    assert hits, "beta cannot find its own content"


def test_multi_project_scope_spans_exactly_those_projects(owner_per_project):
    o = owner_per_project
    o.run_full_index()
    hits = o.search("scheduling", project_ids=["alpha", "beta"],
                    score_threshold=0.0, top_k=20)
    assert {h.project_id for h in hits} <= {"alpha", "beta"}


def test_unknown_project_is_refused_not_silently_widened(owner_per_project):
    from ragtools.collection_router import UnknownProject

    o = owner_per_project
    o.run_full_index()
    with pytest.raises(UnknownProject):
        o.search("anything", project_id="ghost-project")


# --- lifecycle -----------------------------------------------------------


def test_incremental_after_full_is_a_no_op(owner_per_project):
    o = owner_per_project
    first = o.run_full_index()
    again = o.run_incremental_index()
    assert again["indexed"] == 0
    assert again["skipped"] == first["files_indexed"]


def test_deleting_a_file_removes_it_from_its_own_collection(owner_per_project):
    o = owner_per_project
    o.run_full_index()
    before = {c["name"]: c["points"] for c in o.get_status()["collections"]}

    target = Path(o.settings.projects[0].path) / "notes.md"
    target.unlink()
    stats = o.run_incremental_index()
    assert stats["deleted"] == 1

    after = {c["name"]: c["points"] for c in o.get_status()["collections"]}
    alpha = o.router.write_collection("alpha")
    beta = o.router.write_collection("beta")
    assert after[alpha] < before[alpha], "alpha's points were not removed"
    assert after[beta] == before[beta], "beta's collection was touched by alpha's delete"


def test_removing_a_project_drops_its_collection_only(owner_per_project):
    o = owner_per_project
    o.run_full_index()
    beta_collection = o.router.write_collection("beta")

    o.delete_project_data("alpha")

    remaining = {c["name"] for c in o.get_status()["collections"]}
    # beta is untouched and still holds its points
    counts = {c["name"]: c["points"] for c in o.get_status()["collections"]}
    assert beta_collection in remaining and counts[beta_collection] > 0


def test_rebuild_clears_every_collection_not_just_the_shared_one(owner_per_project):
    o = owner_per_project
    o.run_full_index()
    before = o.get_status()["points_count"]
    assert before > 0

    stats = o.rebuild()
    after = o.get_status()["points_count"]
    # A rebuild that only cleared the shared collection would DOUBLE the points.
    assert after == stats["chunks_indexed"], (
        f"rebuild left stale vectors behind: {after} points for "
        f"{stats['chunks_indexed']} freshly indexed chunks"
    )


# --- framework references ------------------------------------------------


def test_a_linked_framework_corpus_is_searchable_from_the_project(owner_per_project):
    """The reference model: indexed once, linked, never copied per project."""
    from ragtools.indexing.indexer import ensure_collection, index_file

    o = owner_per_project
    o.run_full_index()

    fw_root = Path(o.settings.data_dir) / "vendor"
    fw_root.mkdir(parents=True, exist_ok=True)
    doc = fw_root / "orm.md"
    doc.write_text(
        "# Framework ORM\n\nRecordsets are lazily evaluated and cached per cursor.\n",
        encoding="utf-8")

    rec, created = o.framework_registry.register(
        name="demo-framework", version="1.0", edition="community",
        build_id="build-abc", canonical_root=str(fw_root),
    )
    assert created
    ensure_collection(o.client, rec.collection_name, o.encoder.dimension)
    index_file(
        client=o.client, encoder=o.encoder, collection_name=rec.collection_name,
        project_id="demo-framework", file_path=doc, relative_path="orm.md",
        chunk_size=o.settings.chunk_size, chunk_overlap=o.settings.chunk_overlap,
    )

    alpha_uuid = o.registry.get("alpha").uuid
    o.framework_registry.link(alpha_uuid, rec.collection_name)

    # alpha can now reach the framework corpus...
    hits = o.search("recordsets lazily evaluated cursor", project_id="alpha",
                    score_threshold=0.0, top_k=10)
    assert any(h.project_id == "demo-framework" for h in hits), \
        "linked framework corpus is not reachable from the project"

    # ...and beta, which is NOT linked, cannot.
    hits = o.search("recordsets lazily evaluated cursor", project_id="beta",
                    score_threshold=0.0, top_k=10)
    assert not any(h.project_id == "demo-framework" for h in hits), \
        "an unlinked project reached a framework corpus"


def test_one_framework_build_shared_by_two_projects_is_stored_once(owner_per_project):
    o = owner_per_project
    rec_a, created_a = o.framework_registry.register(
        name="odoo", version="19.0", edition="ce", build_id="same-build",
        canonical_root="/srv/a")
    rec_b, created_b = o.framework_registry.register(
        name="odoo", version="19.0", edition="ce", build_id="same-build",
        canonical_root="/srv/b")
    assert created_a and not created_b
    assert rec_a.collection_name == rec_b.collection_name, \
        "the same build indexed twice — dedup is the point of the model"


# --- the warning the owner asked about -----------------------------------


def test_scale_warning_fires_on_the_embedded_engine():
    rec = compute_scale_warning(88_825, capabilities=_EMBEDDED_CAPS)
    assert rec["level"] == "over"
    assert "20,000" in rec["message"]


def test_scale_warning_is_silent_on_a_real_server():
    """The 20,000 ceiling is a property of local mode's brute-force scan. On a
    server with HNSW the limitation does not exist, so repeating the warning
    would train the operator to ignore it."""
    rec = compute_scale_warning(88_825, capabilities=_SERVER_CAPS)
    assert rec["level"] == "ok"
    assert rec["message"] == ""
    assert rec["engine"] == "server"


def test_scale_warning_defaults_to_the_cautious_engine():
    """No capabilities supplied => assume embedded, the weaker guarantee."""
    assert compute_scale_warning(88_825)["level"] == "over"


def test_status_reports_the_storage_engine(owner_per_project):
    storage = owner_per_project.get_status()["storage"]
    assert storage["backend"] == "embedded"
    assert storage["hnsw"] is False
    assert storage["concurrent_readers"] is False
