"""S1 / A1 — one authoritative indexing hygiene step.

v2.7.0's service index path (``QdrantOwner.run_full_index`` /
``run_incremental_index``) called ``chunk_file -> chunks_to_points ->
upsert`` directly, bypassing ``index_file`` where secret redaction and
``source_class`` assignment live. Result: on the live service, secret VALUES
were embedded into vectors and stored in the payload, and every point defaulted
to ``source_class="owned"`` (so the rerank down-weight was inert).

This pins the shared hygiene step both paths must apply, plus an end-to-end
check that the SERVICE path now redacts before storage.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S1/A1 -> G1)
"""

from ragtools.chunking.dispatch import chunk_file
from ragtools.indexing.indexer import apply_source_class_and_redaction


_SECRET = "AKIAIOSFODNN7EXAMPLE"  # AWS access key id shape -> high-confidence redact


def test_helper_redacts_and_classifies(tmp_path):
    f = tmp_path / "bundle.js"
    f.write_text(f"const k = 'aws_access_key_id={_SECRET}';\n")
    chunks = chunk_file(
        file_path=f,
        project_id="p",
        relative_path="dist/bundle.js",
        chunk_size=400,
        chunk_overlap=100,
    )
    assert chunks, "expected the js file to produce chunks"

    apply_source_class_and_redaction(chunks, "dist/bundle.js")

    for c in chunks:
        assert _SECRET not in c.text
        assert _SECRET not in c.raw_text
        # dist/ -> generated (proves classification is actually applied, not defaulted)
        assert c.source_class == "generated"


def test_helper_assigns_owned_for_normal_paths(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("# Notes\n\nJust some ordinary documentation content here.\n")
    chunks = chunk_file(
        file_path=f,
        project_id="p",
        relative_path="docs/notes.md",
        chunk_size=400,
        chunk_overlap=100,
    )
    assert chunks
    apply_source_class_and_redaction(chunks, "docs/notes.md")
    for c in chunks:
        assert c.source_class == "owned"


def test_service_index_path_redacts_secret_in_stored_payload(tmp_path):
    """End-to-end: the SERVICE (owner) index path must redact before storage."""
    from ragtools.config import ProjectConfig, Settings
    from ragtools.service.owner import QdrantOwner

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "notes.md").write_text(
        f"# Notes\n\nOur key is aws_access_key_id={_SECRET} — do not share.\n"
    )
    settings = Settings(
        content_root=str(tmp_path),
        state_db=str(tmp_path / "state.db"),
        projects=[ProjectConfig(id="proj", path=str(proj))],
    )
    client = Settings.get_memory_client()
    owner = QdrantOwner(settings=settings, client=client)
    owner.run_full_index()

    points, _ = client.scroll(
        collection_name=settings.collection_name, with_payload=True, limit=1000
    )
    assert points, "expected indexed points"
    for p in points:
        payload = p.payload or {}
        assert _SECRET not in (payload.get("text") or "")
        assert payload.get("source_class"), "source_class must be assigned"
