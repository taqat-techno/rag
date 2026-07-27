"""Declaring a dependency from the admin panel — the whole loop.

The engine could already index, dedupe and link framework corpora, but nothing
a user could reach ever called it: ``dependency_paths`` was absent from the API
request models, absent from both project forms, and ``sync_frameworks`` had no
production caller at all. This suite covers the parts that turn that engine
into a feature:

* the field survives create/update and reaches TOML;
* changing it schedules the sync, and *not* changing it does not;
* the dry-run preview answers the two questions worth asking before saving —
  "did this path resolve?" and "is this corpus already shared?";
* **removing** a dependency unlinks it, and drops the corpus only when nobody
  else is reading it.

That last one is the reason this is not just a form field. Without release,
un-declaring leaves the project linked to the corpus AND re-indexes the same
files into its own collection, so every hit appears twice with no way to tell
which is which.
"""

import tempfile
from pathlib import Path

import pytest

from ragtools.config import DependencyConfig, ProjectConfig, Settings
from ragtools.service.owner import QdrantOwner

from tests.test_dependency_architecture import _odoo, _owner, _paths_in, _project


# --- the dry-run preview -------------------------------------------------


def test_preview_reports_the_detected_framework(tmp_path):
    """The user typed a folder; they need to know what it was recognised as
    before they commit to it."""
    from ragtools.service.routes import _inspect_dependencies

    proj, _vendor = _project(tmp_path, "alpha")
    with _service_owner(tmp_path, [ProjectConfig(id="alpha", path=str(proj))]):
        result = _inspect_dependencies(str(proj), ["platform/odoo"])

    entry = result["entries"][0]
    assert entry["ok"] is True
    assert entry["framework"] == "odoo"
    assert entry["version"] == "19.0"
    assert entry["detector"] == "odoo"
    assert entry["collection"].startswith("fw_odoo_")


def test_preview_rejects_the_project_root_with_a_reason_on_the_row(tmp_path):
    """A page-level blob of problems makes the user match text to line by eye.
    Each declared path carries its own verdict."""
    from ragtools.service.routes import _inspect_dependencies

    proj, _vendor = _project(tmp_path, "alpha")
    with _service_owner(tmp_path, [ProjectConfig(id="alpha", path=str(proj))]):
        result = _inspect_dependencies(str(proj), [".", "platform/odoo"])

    rejected = next(e for e in result["entries"] if e["declared"] == ".")
    assert rejected["ok"] is False
    assert "project root" in rejected["problem"]
    # The valid sibling is unaffected — one bad line does not void the rest.
    assert next(e for e in result["entries"] if e["declared"] == "platform/odoo")["ok"]


def test_preview_reports_a_missing_folder_rather_than_guessing(tmp_path):
    from ragtools.service.routes import _inspect_dependencies

    proj, _vendor = _project(tmp_path, "alpha")
    with _service_owner(tmp_path, [ProjectConfig(id="alpha", path=str(proj))]):
        result = _inspect_dependencies(str(proj), ["platform/nope"])

    assert result["entries"][0]["ok"] is False
    assert "does not exist" in result["entries"][0]["problem"]


def test_preview_says_when_a_corpus_is_already_shared(tmp_path):
    """"Already indexed, shared by 1 project" is the single most useful fact
    here: it means declaring this costs nothing extra."""
    from ragtools.service.routes import _inspect_dependencies

    alpha, _ = _project(tmp_path, "alpha")
    beta, _ = _project(tmp_path, "beta")
    projects = [ProjectConfig(id="alpha", path=str(alpha), mode="general",
                              dependency_paths=["platform/odoo"]),
                ProjectConfig(id="beta", path=str(beta), mode="general")]
    with _service_owner(tmp_path, projects) as owner:
        owner.run_full_index()
        owner.sync_frameworks()
        # beta has not declared it yet — the preview is what tells them it is free.
        result = _inspect_dependencies(str(beta), ["platform/odoo"])

    entry = result["entries"][0]
    assert entry["exists"] is True
    assert entry["shared_with"] == 1
    assert entry["points"] > 0


def test_preview_changes_nothing(tmp_path):
    """A dry run that registers a corpus is not a dry run."""
    from ragtools.service.routes import _inspect_dependencies

    proj, _vendor = _project(tmp_path, "alpha")
    with _service_owner(tmp_path, [ProjectConfig(id="alpha", path=str(proj))]) as owner:
        before = owner.router.all_collections()
        _inspect_dependencies(str(proj), ["platform/odoo"])
        assert owner.router.all_collections() == before
        assert owner.framework_registry.get(
            _inspect_dependencies(str(proj), ["platform/odoo"])["entries"][0]["collection"]
        ) is None


def test_two_undetected_cores_with_the_same_folder_name_stay_separate(tmp_path):
    """The layout that exposed this is the common one: the Odoo core sits at
    ``<project>/odoo`` while the checkout root IS the project (which is refused
    as a dependency), so the odoo detector never fires and both projects fall
    back to a generic corpus named "odoo".

    Keyed by name alone, those two different cores merge into one collection and
    project A's search starts returning project B's code. Duplicate storage is
    the acceptable failure; cross-project bleed is not.
    """
    from ragtools.frameworks import describe_dependency
    from ragtools.identity import framework_collection_name

    a = tmp_path / "alpha" / "odoo"
    b = tmp_path / "beta" / "odoo"
    for d in (a, b):
        d.mkdir(parents=True)
        (d / "core.py").write_text("# core\n", encoding="utf-8")

    info_a, info_b = describe_dependency(a), describe_dependency(b)
    assert info_a.detector == "generic" and info_b.detector == "generic"
    assert info_a.name == info_b.name == "odoo"

    collection = lambda i: framework_collection_name(  # noqa: E731
        i.name, version=i.version, edition=i.edition, build_id=i.build_id)
    assert collection(info_a) != collection(info_b)


def test_the_same_generic_folder_reached_two_ways_is_one_corpus(tmp_path):
    """Path identity must still be resolved identity — otherwise `..` or a
    symlink spelling would defeat the dedup this whole model exists for."""
    from ragtools.frameworks import describe_dependency

    real = tmp_path / "shared" / "sdk"
    real.mkdir(parents=True)
    (real / "core.py").write_text("# sdk\n", encoding="utf-8")

    direct = describe_dependency(real)
    indirect = describe_dependency(tmp_path / "shared" / ".." / "shared" / "sdk")
    assert direct.build_id == indirect.build_id


# --- removing a dependency ----------------------------------------------


def test_undeclaring_a_dependency_unlinks_and_drops_the_corpus():
    """The other half of the lifecycle. Adding was implemented; removing was not,
    so search kept returning a corpus the project no longer declared."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _vendor = _project(tmp, "alpha")
        project = ProjectConfig(id="alpha", path=str(proj), mode="general",
                                dependency_paths=["platform/odoo"])
        owner = _owner(tmp, [project])
        try:
            owner.run_full_index()
            collection = owner.sync_frameworks()[0]["collection"]
            record = owner.registry.get("alpha")
            assert owner.framework_registry.framework_collections_for(record.uuid) == [collection]

            # The user un-selects it in the project's multi-select and saves.
            project.dependencies = []
            released = [e for e in owner.sync_frameworks() if e.get("action") == "released"]

            assert len(released) == 1
            assert released[0]["collection"] == collection
            assert released[0]["dropped"] is True
            assert owner.framework_registry.framework_collections_for(record.uuid) == []
            assert owner.framework_registry.get(collection) is None
            assert collection not in owner.router.all_collections()
        finally:
            owner.close()


def test_a_shared_corpus_is_never_dropped_while_another_project_reads_it():
    """The dedup that makes two projects share one Odoo build must not turn the
    second project's cleanup into the first project's data loss."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        alpha, _ = _project(tmp, "alpha")
        beta, _ = _project(tmp, "beta")
        a = ProjectConfig(id="alpha", path=str(alpha), mode="general",
                          dependency_paths=["platform/odoo"])
        b = ProjectConfig(id="beta", path=str(beta), mode="general",
                          dependency_paths=["platform/odoo"])
        owner = _owner(tmp, [a, b])
        try:
            owner.run_full_index()
            synced = owner.sync_frameworks()
            collections = {e["collection"] for e in synced if e.get("action") != "released"}
            assert len(collections) == 1, "the shared build was not deduplicated"
            collection = collections.pop()

            # beta stops using it; alpha still does.
            b.dependencies = []
            released = [e for e in owner.sync_frameworks() if e.get("action") == "released"]

            assert len(released) == 1
            assert released[0]["project"] == "beta"
            assert released[0]["dropped"] is False, "dropped a corpus alpha still reads"
            assert released[0]["still_linked_by"] == 1

            alpha_record = owner.registry.get("alpha")
            beta_record = owner.registry.get("beta")
            fw = owner.framework_registry
            assert fw.framework_collections_for(alpha_record.uuid) == [collection]
            assert fw.framework_collections_for(beta_record.uuid) == []
            # And alpha can still actually read it.
            assert any("core_mod" in p for p in _paths_in(owner, collection))
        finally:
            owner.close()


def test_release_is_idempotent():
    """Sync runs on every dependency edit; a second pass must not re-report a
    release that already happened."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _vendor = _project(tmp, "alpha")
        project = ProjectConfig(id="alpha", path=str(proj), mode="general",
                                dependency_paths=["platform/odoo"])
        owner = _owner(tmp, [project])
        try:
            owner.run_full_index()
            owner.sync_frameworks()
            project.dependencies = []
            assert len([e for e in owner.sync_frameworks()
                        if e.get("action") == "released"]) == 1
            assert owner.sync_frameworks() == []
        finally:
            owner.close()


def test_swapping_one_dependency_for_another_releases_only_the_old_one():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _vendor = _project(tmp, "alpha")
        _odoo(proj / "platform" / "odoo18", version="18.0", heads="odoo bbb\n")
        project = ProjectConfig(id="alpha", path=str(proj), mode="general",
                                dependency_paths=["platform/odoo"])
        owner = _owner(tmp, [project])
        try:
            owner.run_full_index()
            first = owner.sync_frameworks()[0]["collection"]

            # Swap the link to a second catalog entry (a different Odoo build).
            newer = DependencyConfig(id="odoo18", path=str(proj / "platform" / "odoo18"))
            owner.settings.dependencies = list(owner.settings.dependencies) + [newer]
            project.dependencies = [newer.id]
            synced = owner.sync_frameworks()
            linked = [e for e in synced if e.get("action") != "released"]
            released = [e for e in synced if e.get("action") == "released"]

            assert len(linked) == 1 and linked[0]["collection"] != first
            assert len(released) == 1 and released[0]["collection"] == first
        finally:
            owner.close()


# --- what search reports -------------------------------------------------


def test_search_results_say_which_collection_answered(tmp_path):
    """The searcher tags every hit project|framework, and the API serializer
    hand-lists its fields — so the label was computed and then dropped at the
    boundary. "Your code does X" and "the framework you vendor does X" lead to
    different next actions; the payload alone cannot distinguish them, because a
    framework chunk carries the framework's id rather than the project's.
    """
    proj, _ = _project(tmp_path, "alpha")
    projects = [ProjectConfig(id="alpha", path=str(proj), mode="general",
                              dependency_paths=["platform/odoo"])]
    with _service_owner(tmp_path, projects) as owner:
        owner.run_full_index()
        owner.sync_frameworks()
        # search_formatted is what /api/search serves — the layer that was
        # dropping the label.
        payload = owner.search_formatted(
            "recordset caching ORM internals dunning ladder",
            project_id="alpha", top_k=20)

    results = payload["results"]
    assert results, "nothing came back to label"
    assert all("scope" in r for r in results), "the API dropped the scope label"

    scopes = {r["scope"] for r in results}
    assert scopes <= {"project", "framework"}
    framework_hits = [r for r in results if r["scope"] == "framework"]
    assert framework_hits, "the framework corpus was unreachable from the project"
    # A framework hit names the corpus it came from, so the reader can tell
    # WHICH dependency answered when a project links more than one.
    assert all(r["scope_source"].startswith("fw_") for r in framework_hits)
    assert all(r["scope_source"] == "" for r in results if r["scope"] == "project")


# --- the API surface ----------------------------------------------------


def test_dependency_paths_round_trip_through_create_and_update(tmp_path, monkeypatch):
    """The field existed on ProjectConfig and was read by the indexer, but the
    request models dropped it — so nothing sent from the panel ever arrived."""
    from ragtools.service.routes import ProjectCreateRequest, ProjectUpdateRequest

    create = ProjectCreateRequest(id="alpha", path=str(tmp_path),
                                  dependency_paths=["platform/odoo"])
    assert create.dependency_paths == ["platform/odoo"]

    # None means "not provided" — a partial update must not silently clear it.
    assert ProjectUpdateRequest(name="x").dependency_paths is None
    # [] is a real, distinct instruction: remove them all.
    assert ProjectUpdateRequest(dependency_paths=[]).dependency_paths == []


def test_saving_dependencies_persists_them_to_toml(tmp_path):
    """model_dump round-trips every field, so this is really a guard that the
    field is not excluded on the way out."""
    import tomllib

    from ragtools.service.pages import _save_projects_to_toml

    config_path = tmp_path / "ragtools.toml"
    import ragtools.config as config_module
    original = config_module.get_config_write_path
    config_module.get_config_write_path = lambda: config_path
    try:
        _save_projects_to_toml([ProjectConfig(id="alpha", path=str(tmp_path),
                                              dependency_paths=["platform/odoo"])])
    finally:
        config_module.get_config_write_path = original

    saved = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert saved["projects"][0]["dependency_paths"] == ["platform/odoo"]


def test_the_frameworks_roster_names_the_projects_sharing_each_corpus(tmp_path):
    """Links are stored as UUIDs; the roster is only useful if it maps them back
    to the ids the user typed. (The first cut read a field ProjectRecord does
    not have and 500'd — caught live, not here, hence this test.)"""
    from ragtools.service.routes import frameworks_list

    alpha, _ = _project(tmp_path, "alpha")
    beta, _ = _project(tmp_path, "beta")
    projects = [ProjectConfig(id="alpha", path=str(alpha), mode="general",
                              dependency_paths=["platform/odoo"]),
                ProjectConfig(id="beta", path=str(beta), mode="general",
                              dependency_paths=["platform/odoo"])]
    with _service_owner(tmp_path, projects) as owner:
        owner.run_full_index()
        owner.sync_frameworks()
        result = frameworks_list()

    assert result["supported"] is True
    assert len(result["frameworks"]) == 1, "the shared build was not deduplicated"
    corpus = result["frameworks"][0]
    assert corpus["name"] == "odoo"
    assert corpus["version"] == "19.0"
    assert corpus["projects"] == ["alpha", "beta"]
    assert corpus["points"] > 0


def test_the_frameworks_roster_is_empty_not_broken_in_shared_mode(tmp_path):
    from ragtools.service.routes import frameworks_list

    proj, _ = _project(tmp_path, "alpha")
    owner = _owner(tmp_path, [ProjectConfig(id="alpha", path=str(proj))],
                   strategy="shared")
    from ragtools.service import routes
    original = routes.get_owner
    routes.get_owner = lambda: owner
    try:
        assert frameworks_list() == {"supported": False, "frameworks": []}
    finally:
        routes.get_owner = original
        owner.close()


def test_the_sync_job_kind_is_registered():
    """A scheduled job whose kind has no handler fails at run time, in the
    background, where nobody is looking."""
    from ragtools.service.job_handlers import make_handlers

    assert "sync_frameworks" in make_handlers(lambda: None)


def test_an_index_job_waits_for_the_lock_instead_of_reporting_a_no_op_as_success(monkeypatch):
    """`run_*_index` returns a `busy` no-op when another run holds the index
    mutex. Recording that as success loses whatever correction the job existed
    to make — observed live: the reindex that restores files after a dependency
    is un-declared ran while the watcher (restarted by the same config write)
    held the lock, so the files stayed missing and the job log said success.
    """
    from ragtools.service import job_handlers

    monkeypatch.setattr(job_handlers, "_BUSY_RETRY_SECONDS", 0.0)

    class _Owner:
        def __init__(self):
            self.calls = 0

        def run_incremental_index(self, project_id=None, progress=None):
            self.calls += 1
            if self.calls < 3:
                return {"indexed": 0, "skipped": 0, "busy": True}
            return {"indexed": 67, "skipped": 0, "chunks_indexed": 784}

    class _Ctx:
        verified = False

        def progress(self, **kw):
            pass

        def check_cancel(self):
            pass

    class _Job:
        scope = {"project": "rag-docs", "full": False}

    owner = _Owner()
    result = job_handlers.make_handlers(lambda: owner)["index"](_Job(), _Ctx())
    assert owner.calls == 3, "the job gave up instead of waiting for the lock"
    assert result["indexed"] == 67
    assert "busy" not in result


class _BusyOwner:
    """An owner whose index lock is never free. ``beat`` is the heartbeat the
    waiter reads; ``None`` means the holder publishes nothing at all."""

    def __init__(self, beat=None):
        self._beat = beat

    def run_incremental_index(self, project_id=None, progress=None):
        return {"indexed": 0, "busy": True}

    def index_activity(self):
        return self._beat


class _Ctx:
    verified = False

    def progress(self, **kw):
        pass

    def check_cancel(self):
        pass


class _Job:
    scope = {"project": "rag-docs", "full": False}


def _run_index_job(owner):
    from ragtools.service.job_handlers import make_handlers
    return make_handlers(lambda: owner)["index"](_Job(), _Ctx())


def test_an_index_job_fails_loudly_when_the_lock_never_frees(monkeypatch):
    """Waiting forever is its own failure. A permanently stuck indexer must
    surface as a failed job, not a parked thread.

    Still true; the trigger changed in v3.0.1. It used to be a flat 900 s of
    elapsed time, which cannot tell a wedged run from a slow one — see
    `test_an_index_job_waits_for_a_run_that_is_still_moving`.
    """
    from ragtools.service import job_handlers

    monkeypatch.setattr(job_handlers, "_BUSY_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(job_handlers, "_STALL_SECONDS", 0.0)

    owner = _BusyOwner({"what": "Full index", "phase": "chunk",
                        "done": 10, "total": 100, "age": 999.0})

    with pytest.raises(RuntimeError, match="appears stalled"):
        _run_index_job(owner)


def test_an_index_job_still_gives_up_when_the_holder_says_nothing(monkeypatch):
    """No heartbeat means no way to tell slow from dead, so elapsed time is all
    that is left — and the message must claim only that."""
    from ragtools.service import job_handlers

    monkeypatch.setattr(job_handlers, "_BUSY_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(job_handlers, "_BLIND_MAX_WAIT_SECONDS", 0.0)

    with pytest.raises(RuntimeError, match="no progress information"):
        _run_index_job(_BusyOwner(None))


def test_an_index_job_waits_for_a_run_that_is_still_moving(monkeypatch):
    """The 3.0.0 defect, as a test.

    A healthy startup sync of 25 projects outlives any fixed ceiling. Under the
    old rule this job failed after 900 s and announced that the other run was
    "stuck"; the other run went on to finish normally.
    """
    from ragtools.service import job_handlers

    monkeypatch.setattr(job_handlers, "_BUSY_RETRY_SECONDS", 0.0)
    # Elapsed time far past ANY ceiling the old code would have applied.
    monkeypatch.setattr(job_handlers, "_BLIND_MAX_WAIT_SECONDS", 0.0)

    beat = {"what": "Full index", "phase": "chunk", "done": 0, "total": 37637,
            "age": 0.5}
    owner = _BusyOwner(beat)
    # Free the lock only after many rounds, exactly as a long index would.
    rounds = {"n": 0}
    real = owner.run_incremental_index

    def _eventually(project_id=None, progress=None):
        rounds["n"] += 1
        if rounds["n"] > 500:
            return {"indexed": 3, "chunks_indexed": 9, "projects": ["rag-docs"]}
        return real(project_id=project_id, progress=progress)

    owner.run_incremental_index = _eventually

    stats = _run_index_job(owner)

    assert stats["indexed"] == 3, "a progressing run must be waited for, not failed"
    assert rounds["n"] > 500


def test_the_sync_handler_summarises_both_directions():
    from ragtools.service.job_handlers import make_handlers

    class _Owner:
        def sync_frameworks(self, progress=None):
            return [
                {"collection": "fw_odoo_1", "files_indexed": 7, "chunks_indexed": 20,
                 "purged_from_project": 3},
                {"action": "released", "collection": "fw_old_2", "dropped": True},
            ]

    class _Ctx:
        verified = False

        def progress(self, **kw):
            pass

        def check_cancel(self):
            pass

    result = make_handlers(lambda: _Owner())["sync_frameworks"](object(), _Ctx())
    assert result["linked"] == 2
    assert result["files_indexed"] == 7
    assert result["purged_from_projects"] == 3
    assert result["collections"] == ["fw_odoo_1", "fw_old_2"]


# --- the forms ----------------------------------------------------------


def test_the_add_form_points_at_the_catalog_instead_of_a_path_box(tmp_path):
    """Dependencies are now SELECTED from a catalog, and at add time there is no
    project yet to validate a link against. Offering a half-validated path box
    there would reintroduce exactly the retyping the catalog removes — so the
    add form explains the flow instead.

    Selection itself is covered by
    test_dependency_catalog.test_the_project_form_offers_a_multi_select_not_a_path_box.
    """
    add_form = (Path(__file__).resolve().parents[1] / "src" / "ragtools" / "service"
                / "templates" / "projects.html").read_text(encoding="utf-8")
    assert 'name="dependency_paths"' not in add_form, "the superseded path box is back"
    assert "shared dependencies" in add_form.lower()


def test_the_edit_field_opens_when_a_dependency_is_linked(tmp_path, monkeypatch):
    """Collapsed-by-default hides the fact that a whole tree is being searched
    from somewhere else."""
    from ragtools.config import DependencyConfig
    from ragtools.service import pages

    dep = tmp_path / "shared" / "odoo"
    dep.mkdir(parents=True)
    entry = DependencyConfig(id="odoo", name="Odoo core", path=str(dep))
    empty_project = ProjectConfig(id="alpha", path=str(tmp_path))
    linked_project = ProjectConfig(id="alpha", path=str(tmp_path), dependencies=["odoo"])
    settings = Settings(content_root=str(tmp_path), qdrant_path=str(tmp_path / "q"),
                        state_db=str(tmp_path / "s.db"), data_dir=str(tmp_path / "d"),
                        projects=[linked_project], dependencies=[entry])
    monkeypatch.setattr(pages, "get_settings", lambda: settings)

    empty = pages._deps_field(empty_project)
    linked = pages._deps_field(linked_project)
    assert "<details" in empty and " open" not in empty.split(">")[0]
    assert " open" in linked.split(">")[0]
    assert "Odoo core" in linked


def test_the_check_fragment_renders_a_verdict_per_row(tmp_path):
    from ragtools.service.pages import _render_deps_check

    html = _render_deps_check({"entries": [
        {"declared": "platform/odoo", "ok": True, "framework": "odoo", "version": "19.0",
         "edition": "community", "detector": "odoo", "collection": "fw_odoo_1",
         "exists": True, "shared_with": 2, "points": 1234},
        {"declared": ".", "ok": False, "problem": "is the project root itself"},
    ]})
    assert "Already indexed" in html
    assert "1,234 chunks, shared by 2 projects" in html
    assert "Rejected" in html
    assert "is the project root itself" in html


def test_the_check_fragment_says_nothing_is_declared_rather_than_rendering_an_empty_table(tmp_path):
    from ragtools.service.pages import _render_deps_check

    html = _render_deps_check({"entries": []})
    assert "<table" not in html
    assert "No dependency folders declared" in html


def test_the_project_row_shows_a_linked_dependency(tmp_path):
    """It changes where a tree is searched from — that belongs on the row, not
    only behind an edit click. The tooltip names the dependency, since that is
    what the user selected; the raw path is on the Dependencies page."""
    from ragtools.config import DependencyConfig
    from ragtools.service import pages

    proj = tmp_path / "alpha"
    proj.mkdir()
    dep = tmp_path / "shared" / "odoo"
    dep.mkdir(parents=True)
    settings = Settings(
        content_root=str(tmp_path), qdrant_path=str(tmp_path / "q"),
        state_db=str(tmp_path / "s.db"), data_dir=str(tmp_path / "d"),
        dependencies=[DependencyConfig(id="odoo", name="Odoo core", path=str(dep))],
        projects=[ProjectConfig(id="alpha", path=str(proj), dependencies=["odoo"])],
    )
    original = pages.get_settings
    pages.get_settings = lambda: settings
    try:
        html = pages._render_projects_list()
    finally:
        pages.get_settings = original

    assert "+1 shared" in html
    assert "Odoo core" in html   # in the tooltip


# --- helpers -------------------------------------------------------------


class _service_owner:
    """An owner installed as the service singleton, for the route helpers.

    ``_inspect_dependencies`` reads the live framework registry to answer "is
    this corpus already shared?", so the routes' ``get_owner`` has to resolve.
    """

    def __init__(self, tmp: Path, projects, dependencies=None):
        self._tmp, self._projects = tmp, projects
        self._dependencies = dependencies

    def __enter__(self):
        from ragtools.service import routes
        self._owner = _owner(self._tmp, self._projects,
                             dependencies=self._dependencies)
        self._original = routes.get_owner
        # The catalog endpoints read BOTH — settings for the config it mutates,
        # owner for the live index state.
        self._original_settings = routes.get_settings
        routes.get_owner = lambda: self._owner
        routes.get_settings = lambda: self._owner.settings
        return self._owner

    def __exit__(self, *exc):
        from ragtools.service import routes
        routes.get_owner = self._original
        routes.get_settings = self._original_settings
        self._owner.close()
