"""Can a user SEE that a declared dependency worked?

The feature was correct and unobservable. Declaring a dependency on a real
32,782-file Odoo core produced, from the user's side: a project file count that
went *down*, an empty framework roster, and a completely static page — because

* the corpus is registered first and **linked last**, and every roster
  enumerated only linked corpora, so the entire multi-minute import reported
  "no frameworks";
* `job.progress` was emitted over SSE and re-dispatched as a DOM event that
  **nothing listened to**;
* the map grouped points by ``project_id``, which for a framework corpus is the
  *framework's* id — so a vendored core drew as though it were a project of
  your own, and collided with any project sharing that name.

A count going down with no other signal reads as data loss, not success.
"""

import tempfile
from pathlib import Path

import numpy as np

from ragtools.config import ProjectConfig, Settings

from tests.test_dependency_architecture import _owner, _project
from tests.test_dependency_ui import _service_owner


# --- the roster shows work in progress ----------------------------------


def test_a_corpus_being_indexed_is_visible_before_it_is_linked(tmp_path):
    """Registered-but-unlinked is the whole import window. Reporting nothing
    there is what made a working feature look like a no-op."""
    from ragtools.service.routes import frameworks_list

    proj, _ = _project(tmp_path, "alpha")
    with _service_owner(tmp_path, [ProjectConfig(id="alpha", path=str(proj),
                                                 mode="general")]) as owner:
        # Exactly the mid-sync state: registered, not yet linked.
        owner.framework_registry.register(
            name="odoo", version="18.0", edition="community",
            build_id="deadbeefdeadbeef", canonical_root=str(proj / "platform" / "odoo"))
        listed = frameworks_list()

    assert listed["supported"] is True
    assert len(listed["frameworks"]) == 1
    entry = listed["frameworks"][0]
    assert entry["state"] == "indexing"
    assert entry["linked"] is False
    assert entry["projects"] == []
    assert entry["name"] == "odoo" and entry["version"] == "18.0"


def test_a_linked_corpus_reports_ready(tmp_path):
    from ragtools.service.routes import frameworks_list

    proj, _ = _project(tmp_path, "alpha")
    projects = [ProjectConfig(id="alpha", path=str(proj), mode="general",
                              dependency_paths=["platform/odoo"])]
    with _service_owner(tmp_path, projects) as owner:
        owner.run_full_index()
        owner.sync_frameworks()
        listed = frameworks_list()

    entry = listed["frameworks"][0]
    assert entry["state"] == "ready"
    assert entry["linked"] is True
    assert entry["projects"] == ["alpha"]
    assert entry["points"] > 0


def test_the_registry_lists_unlinked_corpora(tmp_path):
    """`FrameworkRegistry.list` is the primitive the roster needs; without it
    the only enumeration available went through the router, which by design
    knows only what projects link."""
    from ragtools.registry import FrameworkRegistry

    with FrameworkRegistry(str(tmp_path / "reg.db")) as reg:
        reg.register(name="odoo", version="19.0", edition="community",
                     build_id="aaaa", canonical_root="/a")
        reg.register(name="odoo", version="18.0", edition="community",
                     build_id="bbbb", canonical_root="/b")
        listed = reg.list()

    assert [r.version for r in listed] == ["18.0", "19.0"]   # ordered, stable
    assert all(r.collection_name.startswith("fw_odoo_") for r in listed)


# --- the roster fragment -------------------------------------------------


def test_the_frameworks_card_distinguishes_indexing_from_ready(tmp_path):
    from ragtools.service import pages
    from ragtools.service import routes

    def _listed():
        return {"supported": True, "frameworks": [
            {"collection": "fw_odoo_1", "name": "odoo", "version": "19.0",
             "edition": "community", "canonical_root": "C:/x/odoo", "points": 40123,
             "projects": ["alpha", "beta"], "linked": True, "state": "ready"},
            {"collection": "fw_sdk_2", "name": "sdk", "version": "",
             "edition": "generic", "canonical_root": "C:/y/sdk", "points": 91,
             "projects": [], "linked": False, "state": "indexing"},
        ]}

    original = routes.frameworks_list
    routes.frameworks_list = _listed
    try:
        html = pages.ui_frameworks()
    finally:
        routes.frameworks_list = original

    assert "Ready" in html and "Indexing…" in html
    assert "not searchable yet" in html          # the honest part
    assert "40,123" in html
    assert "alpha, beta" in html


def test_the_frameworks_card_explains_itself_when_empty(tmp_path):
    from ragtools.service import pages, routes

    original = routes.frameworks_list
    routes.frameworks_list = lambda: {"supported": True, "frameworks": []}
    try:
        html = pages.ui_frameworks()
    finally:
        routes.frameworks_list = original

    assert "<table" not in html
    assert "Shared dependencies" in html or "shared dependencies" in html


def test_the_frameworks_card_says_so_in_shared_mode():
    from ragtools.service import pages, routes

    original = routes.frameworks_list
    routes.frameworks_list = lambda: {"supported": False, "frameworks": []}
    try:
        html = pages.ui_frameworks()
    finally:
        routes.frameworks_list = original

    assert "per-project collection" in html


# --- the map -------------------------------------------------------------


def _fake_client(chunks_by_collection):
    """A scroll-only stand-in: {collection: [(file_path, project_id), ...]}."""

    class _Rec:
        def __init__(self, fp, pid, vec):
            self.payload = {"file_path": fp, "project_id": pid, "headings": []}
            self.vector = vec

    class _Client:
        def scroll(self, collection_name, limit=500, offset=None,
                   with_payload=True, with_vectors=True):
            if collection_name not in chunks_by_collection:
                raise RuntimeError("no such collection")
            if offset is not None:
                return [], None
            out = []
            for i, (fp, pid) in enumerate(chunks_by_collection[collection_name]):
                vec = list(np.linspace(i, i + 1, 8).astype(float))
                out.append(_Rec(fp, pid, vec))
            return out, None

    return _Client()


def test_map_points_are_tagged_with_their_scope(tmp_path):
    """The map is fed every routed collection, so framework points were already
    on it — indistinguishable from your own code."""
    from ragtools.service.map_data import compute_map_points

    client = _fake_client({
        "proj_abc": [("docs/a.md", "alpha"), ("src/b.py", "alpha")],
        "fw_odoo_1": [("odoo/api.py", "odoo")],
    })
    settings = Settings(content_root=str(tmp_path), qdrant_path=str(tmp_path / "q"),
                        state_db=str(tmp_path / "s.db"), data_dir=str(tmp_path / "d"))
    points = compute_map_points(client, settings, ["proj_abc", "fw_odoo_1"])

    by_path = {p["file_path"]: p for p in points}
    assert by_path["docs/a.md"]["scope"] == "project"
    assert by_path["docs/a.md"]["scope_source"] == ""
    assert by_path["odoo/api.py"]["scope"] == "framework"
    assert by_path["odoo/api.py"]["scope_source"] == "fw_odoo_1"


def test_a_project_file_and_a_framework_file_sharing_a_path_stay_separate(tmp_path):
    """`odoo/api.py` can exist in a project AND in the corpus it vendors. Keyed
    by path alone they merged into ONE map point averaging both collections'
    vectors — a file that exists nowhere."""
    from ragtools.service.map_data import compute_map_points

    client = _fake_client({
        "proj_abc": [("odoo/api.py", "alpha")],
        "fw_odoo_1": [("odoo/api.py", "odoo")],
    })
    settings = Settings(content_root=str(tmp_path), qdrant_path=str(tmp_path / "q"),
                        state_db=str(tmp_path / "s.db"), data_dir=str(tmp_path / "d"))
    points = compute_map_points(client, settings, ["proj_abc", "fw_odoo_1"])

    same_path = [p for p in points if p["file_path"] == "odoo/api.py"]
    assert len(same_path) == 2, "the project copy and the framework copy were merged"
    assert {p["scope"] for p in same_path} == {"project", "framework"}


def test_the_map_scripts_group_by_scope_not_by_project_id_alone():
    """Both renderers colour and label by group; grouping on project_id alone
    puts a vendored core in the same bucket as a project of that name."""
    static = Path(__file__).resolve().parents[1] / "src" / "ragtools" / "service" / "static"
    for name in ("map.js", "map3d.js"):
        source = (static / name).read_text(encoding="utf-8")
        assert "scope === 'framework'" in source, f"{name} ignores scope"
        assert "(shared)" in source, f"{name} does not label shared corpora"
        assert "p.project_id ===" not in source, (
            f"{name} still groups by project_id alone")


# --- the running-job strip ----------------------------------------------


def test_the_progress_event_has_a_consumer():
    """The service emitted `job.progress`, the client re-dispatched it as
    `rag-job-progress`, and nothing listened — so every long operation looked
    identical to an idle page."""
    base = (Path(__file__).resolve().parents[1] / "src" / "ragtools" / "service"
            / "templates" / "base.html").read_text(encoding="utf-8")

    assert base.count("rag-job-progress") >= 2, (
        "rag-job-progress is dispatched but never listened for")
    assert "addEventListener('rag-job-progress'" in base
    assert 'id="job-strip"' in base
    # Hidden until there is something to report.
    strip = base.split('id="job-strip"', 1)[1].split(">", 1)[0]
    assert "hidden" in strip


def test_the_strip_shows_work_already_running_on_page_load():
    """A page opened mid-import must not wait for the next tick — for a slow
    phase that is tens of seconds of apparently-idle UI."""
    base = (Path(__file__).resolve().parents[1] / "src" / "ragtools" / "service"
            / "templates" / "base.html").read_text(encoding="utf-8")
    strip_js = base.split("Running-job strip", 1)[1]
    assert "/api/jobs?limit=5" in strip_js
    assert "'running'" in strip_js


def test_the_strip_does_not_invent_a_percentage_it_does_not_have():
    base = (Path(__file__).resolve().parents[1] / "src" / "ragtools" / "service"
            / "templates" / "base.html").read_text(encoding="utf-8")
    strip_js = base.split("Running-job strip", 1)[1]
    assert "job-strip-indeterminate" in strip_js
    assert "sync_frameworks: 'Indexing shared dependencies'" in strip_js


def test_the_projects_page_surfaces_the_dependency_roster():
    page = (Path(__file__).resolve().parents[1] / "src" / "ragtools" / "service"
            / "templates" / "projects.html").read_text(encoding="utf-8")
    assert '/ui/frameworks' in page
    # It must refresh while a corpus is filling, and when a job finishes.
    assert "every 15s" in page
    assert "rag-job-done from:body" in page
