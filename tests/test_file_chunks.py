"""Inspecting what the index actually stored for a file.

The map's detail panel showed only what the map payload already carried — a
name, a path, a chunk count — and its single action ran a semantic search for
the *filename string*, which returns essentially unrelated results. So the one
question the map is well placed to answer, "what did the indexer keep for this
file", was unanswerable: you had to guess a query that would return the file
and hope.

This is a content surface, so it carries the same obligations as search:

* **redaction** — serve-time masking must apply, or the panel becomes the one
  read path that leaks what every other one masks;
* **scope** — a collection name arriving from the client must be checked
  against what the project may already read, or it is a way to read any
  collection by name;
* **disambiguation** — a project and a framework it vendors can both hold
  ``odoo/api.py``, and which copy you are looking at decides whether you may
  edit it.
"""

import tempfile
from pathlib import Path

import pytest

from ragtools.config import DependencyConfig, ProjectConfig, Settings

from tests.test_dependency_architecture import _owner, _project


def _project_with_code(tmp: Path, name: str) -> Path:
    proj = tmp / name
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "billing.py").write_text(
        '"""Billing rules."""\n'
        "import decimal\n"
        "\n"
        "\n"
        "class DunningLadder:\n"
        '    """Escalation steps for overdue invoices."""\n'
        "\n"
        "    def next_step(self, invoice):\n"
        "        return invoice.age_days // 30\n",
        encoding="utf-8")
    (proj / "README.md").write_text("# Docs\n\nProject notes.\n", encoding="utf-8")
    return proj


def test_the_stored_text_comes_back(tmp_path):
    """The point of the panel: see the content, not a count of it."""
    proj = _project_with_code(tmp_path, "alpha")
    owner = _owner(tmp_path, [ProjectConfig(id="alpha", path=str(proj), mode="general")])
    try:
        owner.run_full_index()
        result = owner.get_file_chunks("alpha", "alpha/src/billing.py")

        assert result["total"] >= 1
        assert result["chunks"], "no chunks returned for an indexed file"
        joined = "\n".join(c["text"] for c in result["chunks"])
        assert "DunningLadder" in joined, "the actual file content is missing"
        assert result["language"] == "python"
    finally:
        owner.close()


def test_chunks_carry_the_anchors_and_symbols_that_make_them_useful(tmp_path):
    proj = _project_with_code(tmp_path, "alpha")
    owner = _owner(tmp_path, [ProjectConfig(id="alpha", path=str(proj), mode="general")])
    try:
        owner.run_full_index()
        result = owner.get_file_chunks("alpha", "alpha/src/billing.py")
        chunk = result["chunks"][0]

        assert chunk["line_end"] >= chunk["line_start"] >= 0
        # Whatever the chunker recorded, the panel needs SOMETHING to label a
        # chunk by — a bare "chunk 3" tells the reader nothing.
        assert (chunk["symbols"] or chunk["class_name"] or chunk["function_name"]
                or chunk["signature"] or chunk["headings"])
    finally:
        owner.close()


def test_chunks_are_returned_in_file_order(tmp_path):
    """Out-of-order chunks read as a shuffled file."""
    proj = tmp_path / "alpha"
    (proj / "src").mkdir(parents=True)
    body = "\n\n".join(f"def step_{i}():\n    return {i}" for i in range(40))
    (proj / "src" / "long.py").write_text(body, encoding="utf-8")

    owner = _owner(tmp_path, [ProjectConfig(id="alpha", path=str(proj), mode="general")])
    try:
        owner.run_full_index()
        result = owner.get_file_chunks("alpha", "alpha/src/long.py")
        indexes = [c["index"] for c in result["chunks"]]
        assert indexes == sorted(indexes)
    finally:
        owner.close()


def test_a_missing_file_returns_empty_rather_than_raising(tmp_path):
    """The map is a snapshot; a file can be removed from the index between the
    map being built and a point being clicked."""
    proj = _project_with_code(tmp_path, "alpha")
    owner = _owner(tmp_path, [ProjectConfig(id="alpha", path=str(proj), mode="general")])
    try:
        owner.run_full_index()
        result = owner.get_file_chunks("alpha", "alpha/src/gone.py")
        assert result["chunks"] == []
        assert result["total"] == 0
    finally:
        owner.close()


# --- redaction ------------------------------------------------------------


def test_secrets_are_masked_the_same_way_search_masks_them(tmp_path):
    """A new read path that skips serve-time redaction is a leak, however
    convenient the panel is."""
    proj = tmp_path / "alpha"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "conf.py").write_text(
        "# deployment notes\n"
        'AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"\n',
        encoding="utf-8")
    owner = _owner(tmp_path, [ProjectConfig(id="alpha", path=str(proj), mode="general")])
    try:
        owner.run_full_index()
        result = owner.get_file_chunks("alpha", "alpha/src/conf.py")
        served = "\n".join(c["text"] for c in result["chunks"])
        if served:                      # only meaningful if the file was indexed
            assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in served
    finally:
        owner.close()


# --- scope ----------------------------------------------------------------


def test_a_collection_the_project_cannot_read_is_refused(tmp_path):
    """The collection name arrives from the client. Trusting it verbatim turns
    the panel into "read any collection by name", which is precisely the
    isolation the per-project model exists to enforce."""
    alpha = _project_with_code(tmp_path, "alpha")
    beta = _project_with_code(tmp_path, "beta")
    owner = _owner(tmp_path, [
        ProjectConfig(id="alpha", path=str(alpha), mode="general"),
        ProjectConfig(id="beta", path=str(beta), mode="general"),
    ])
    try:
        owner.run_full_index()
        beta_collection = owner.router.write_collection("beta")
        with pytest.raises(ValueError, match="not readable"):
            owner.get_file_chunks("alpha", "alpha/src/billing.py", collection=beta_collection)
    finally:
        owner.close()


def test_an_unknown_project_is_refused(tmp_path):
    proj = _project_with_code(tmp_path, "alpha")
    owner = _owner(tmp_path, [ProjectConfig(id="alpha", path=str(proj), mode="general")])
    try:
        owner.run_full_index()
        with pytest.raises(Exception):
            owner.get_file_chunks("ghost", "alpha/src/billing.py")
    finally:
        owner.close()


def test_a_framework_copy_is_distinguishable_from_the_project_copy():
    """A project and the framework it vendors can hold the same relative path.
    Returning "whichever we found first" tells the reader they may edit a file
    that is actually a vendored copy — or the reverse."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _ = _project(tmp, "alpha")
        entry = DependencyConfig(id="odoo", path=str(proj / "platform" / "odoo"))
        settings = Settings(
            content_root=str(tmp), qdrant_path=str(tmp / "q"), state_db=str(tmp / "s.db"),
            data_dir=str(tmp / "d"), collection_strategy="per_project",
            dependencies=[entry],
            projects=[ProjectConfig(id="alpha", path=str(proj), mode="general",
                                    dependencies=["odoo"])],
        )
        from ragtools.service.owner import QdrantOwner
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        try:
            owner.run_full_index()
            fw_collection = owner.sync_frameworks()[0]["collection"]

            # Ask for a framework file, naming its collection.
            fw_paths = [p.payload.get("file_path") for p in
                        owner.client.scroll(collection_name=fw_collection, limit=5,
                                            with_payload=True)[0]]
            result = owner.get_file_chunks("alpha", fw_paths[0], collection=fw_collection)
            assert result["scope"] == "framework"
            assert result["collection"] == fw_collection
            assert result["chunks"]
        finally:
            owner.close()


# --- the API --------------------------------------------------------------


def test_the_endpoint_returns_chunks_for_a_file(tmp_path):
    from ragtools.service.routes import file_chunks
    from tests.test_dependency_ui import _service_owner

    proj = _project_with_code(tmp_path, "alpha")
    projects = [ProjectConfig(id="alpha", path=str(proj), mode="general")]
    with _service_owner(tmp_path, projects) as owner:
        owner.run_full_index()
        payload = file_chunks(file="alpha/src/billing.py", project="alpha", collection="")

    assert payload["total"] >= 1
    assert "DunningLadder" in "\n".join(c["text"] for c in payload["chunks"])


def test_the_endpoint_refuses_a_foreign_collection_with_403(tmp_path):
    from fastapi import HTTPException

    from ragtools.service.routes import file_chunks
    from tests.test_dependency_ui import _service_owner

    alpha = _project_with_code(tmp_path, "alpha")
    beta = _project_with_code(tmp_path, "beta")
    projects = [ProjectConfig(id="alpha", path=str(alpha), mode="general"),
                ProjectConfig(id="beta", path=str(beta), mode="general")]
    with _service_owner(tmp_path, projects) as owner:
        owner.run_full_index()
        foreign = owner.router.write_collection("beta")
        with pytest.raises(HTTPException) as raised:
            file_chunks(file="alpha/src/billing.py", project="alpha", collection=foreign)
    assert raised.value.status_code == 403


# --- the panel ------------------------------------------------------------


def _map_js() -> str:
    return (Path(__file__).resolve().parents[1] / "src" / "ragtools" / "service"
            / "static" / "map.js").read_text(encoding="utf-8")


def test_the_panel_fetches_chunk_detail_instead_of_offering_a_filename_search():
    """Searching for the filename string is what made the panel feel unrelated
    to the file — it returns whatever is semantically near the NAME."""
    source = _map_js()
    assert "/api/files/chunks" in source
    assert "Search this file" not in source
    assert "/search?query=${encodeURIComponent(fname)}" not in source


def test_the_panel_escapes_chunk_text():
    """Chunk text is arbitrary indexed file content. Injecting it unescaped is
    stored XSS in the admin panel — the attacker only needs a file in a folder
    you indexed."""
    source = _map_js()
    assert "esc(c.text" in source, "chunk text is interpolated unescaped"


def test_attribute_interpolation_uses_a_quote_safe_escaper():
    """`esc` round-trips through textContent, which escapes & < > but NOT
    quotes — safe as element content, unsafe inside an attribute, where a path
    containing a double quote (legal on Linux and macOS) closes it early."""
    source = _map_js()
    assert "function escAttr" in source
    assert '="${esc(' not in source, "an attribute is interpolated with the content escaper"


def test_a_stale_response_cannot_overwrite_a_newer_selection():
    """Click a point, then another before the first responds: without a guard
    the slow first response repaints the panel over the point you are on."""
    source = _map_js()
    assert "detailRequest" in source
    assert "token !== detailRequest" in source


def test_the_panel_opens_before_the_fetch_resolves():
    """A panel that stays blank until a slow request lands reads as broken."""
    source = _map_js()
    body = source.split("function showDetail", 1)[1]
    assert "detail-loading" in body
    assert body.index("classList.add('open')") < body.index("fetch(")
