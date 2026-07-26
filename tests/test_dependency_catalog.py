"""Shared dependencies as first-class objects: catalog, links, MCP.

The path-per-project model made the same shared thing an invisible, repeated
string. Two projects vendoring one Odoo core each typed a path; dedup happened
only if both spellings resolved to the same build identity; nothing listed what
existed. The catalog replaces that with: declare once, select from any project.

The rules split in two, and keeping them apart is the whole design:

* **catalog validity** — is it a folder, and is it already registered under
  another id? Project-independent.
* **link validity** — may THIS project use it? A dependency that is a project's
  own root is legal in the catalog and illegal as that project's link, so an
  entry can be fine everywhere except one place.

Plan: docs/planning/RAG_DEPENDENCY_CATALOG_PLAN.md
"""

import tempfile
from pathlib import Path

import pytest

from ragtools.config import DependencyConfig, ProjectConfig, Settings
from ragtools.dependency_catalog import (
    CatalogError,
    check_link,
    find_by_path,
    normalize_id,
    projects_using,
    resolve_project_dependency_paths,
    unlink_everywhere,
    validate_link_set,
    validate_new_entry,
)

from tests.test_dependency_architecture import _odoo, _owner, _paths_in, _project
from tests.test_dependency_ui import _service_owner


def _dirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def _proxy_state():
    """An McpState that looks like a live service, for argument-gate tests."""
    from ragtools.integration.mcp_common import McpState

    state = McpState()
    state.mode = "proxy"
    return state


# --- catalog validity ----------------------------------------------------


def test_a_duplicate_id_is_refused(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _dirs(a, b)
    catalog = [DependencyConfig(id="odoo", path=str(a))]
    with pytest.raises(CatalogError, match="already exists"):
        validate_new_entry(catalog, "odoo", str(b))


def test_the_same_folder_under_a_second_id_is_refused(tmp_path):
    """Two names for one folder defeats the entire point of declaring once —
    and would create two catalog entries competing for one collection."""
    shared = tmp_path / "shared" / "odoo"
    _dirs(shared)
    catalog = [DependencyConfig(id="odoo", name="Odoo", path=str(shared))]
    with pytest.raises(CatalogError, match="already in the catalog as 'odoo'"):
        validate_new_entry(catalog, "odoo-again", str(shared))


def test_a_different_spelling_of_the_same_folder_is_still_a_duplicate(tmp_path):
    """`..` segments, a trailing separator and drive-letter case must all
    collapse — otherwise the dedup is defeated by typing style."""
    shared = tmp_path / "shared" / "odoo"
    _dirs(shared)
    catalog = [DependencyConfig(id="odoo", path=str(shared))]
    spelled = str(tmp_path / "shared" / ".." / "shared" / "odoo")
    with pytest.raises(CatalogError, match="already in the catalog"):
        validate_new_entry(catalog, "other", spelled)


def test_a_relative_path_is_refused_with_a_reason(tmp_path):
    """A catalog entry has no project to be relative to — that is the point."""
    with pytest.raises(CatalogError, match="absolute"):
        validate_new_entry([], "odoo", "platform/odoo")


def test_a_missing_folder_is_refused_at_add_time(tmp_path):
    with pytest.raises(CatalogError, match="does not exist"):
        validate_new_entry([], "odoo", str(tmp_path / "nope"))


def test_a_file_is_not_a_dependency_folder(tmp_path):
    f = tmp_path / "odoo.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(CatalogError, match="not a folder"):
        validate_new_entry([], "odoo", str(f))


def test_an_edit_may_keep_its_own_id_and_path(tmp_path):
    """Without `exclude_id`, editing a display name would collide with itself."""
    shared = tmp_path / "odoo"
    _dirs(shared)
    catalog = [DependencyConfig(id="odoo", path=str(shared))]
    assert validate_new_entry(catalog, "odoo", str(shared), exclude_id="odoo")


def test_an_empty_id_is_refused():
    with pytest.raises(CatalogError, match="id is required"):
        normalize_id("   ")


def test_ids_are_normalised_not_rejected():
    assert normalize_id("Odoo 18 Core") == "odoo-18-core"


def test_find_by_path_matches_any_spelling(tmp_path):
    shared = tmp_path / "shared" / "odoo"
    _dirs(shared)
    catalog = [DependencyConfig(id="odoo", path=str(shared))]
    assert find_by_path(catalog, str(tmp_path / "shared" / ".." / "shared" / "odoo")).id == "odoo"
    assert find_by_path(catalog, str(tmp_path / "elsewhere")) is None


# --- link validity -------------------------------------------------------


def test_a_project_cannot_use_its_own_folder_as_a_dependency(tmp_path):
    proj = tmp_path / "alpha"
    _dirs(proj)
    verdict = check_link(ProjectConfig(id="alpha", path=str(proj)),
                         DependencyConfig(id="self", path=str(proj)))
    assert verdict.blocked
    assert "project's own folder" in verdict.reason


def test_a_project_cannot_use_a_parent_folder_as_a_dependency(tmp_path):
    proj = tmp_path / "workspace" / "alpha"
    _dirs(proj)
    verdict = check_link(ProjectConfig(id="alpha", path=str(proj)),
                         DependencyConfig(id="ws", path=str(tmp_path / "workspace")))
    assert verdict.blocked
    assert "contains the project" in verdict.reason


def test_a_folder_that_vanished_is_blocked_with_a_reason(tmp_path):
    """A catalog entry can rot after it was added; the project form has to say
    so rather than silently offering a link that will never index anything."""
    proj = tmp_path / "alpha"
    _dirs(proj)
    verdict = check_link(ProjectConfig(id="alpha", path=str(proj)),
                         DependencyConfig(id="gone", path=str(tmp_path / "gone")))
    assert verdict.blocked
    assert "no longer exists" in verdict.reason


def test_an_entry_illegal_for_one_project_is_fine_for_another(tmp_path):
    """The reason link validity is separate from catalog validity."""
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    _dirs(alpha, beta)
    entry = DependencyConfig(id="alpha-folder", path=str(alpha))
    assert check_link(ProjectConfig(id="alpha", path=str(alpha)), entry).blocked
    assert check_link(ProjectConfig(id="beta", path=str(beta)), entry).ok


def test_a_link_set_refuses_an_unknown_id(tmp_path):
    proj = ProjectConfig(id="alpha", path=str(tmp_path))
    with pytest.raises(CatalogError, match="No dependency named 'ghost'"):
        validate_link_set(proj, [], ["ghost"])


def test_a_link_set_refuses_an_illegal_link_naming_the_reason(tmp_path):
    proj_dir = tmp_path / "alpha"
    _dirs(proj_dir)
    proj = ProjectConfig(id="alpha", path=str(proj_dir))
    entry = DependencyConfig(id="self", name="Alpha itself", path=str(proj_dir))
    with pytest.raises(CatalogError, match="cannot be used here"):
        validate_link_set(proj, [entry], ["self"])


def test_a_link_set_is_deduplicated_and_order_preserving(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _dirs(a, b)
    proj = ProjectConfig(id="alpha", path=str(tmp_path / "alpha"))
    (tmp_path / "alpha").mkdir()
    catalog = [DependencyConfig(id="a", path=str(a)), DependencyConfig(id="b", path=str(b))]
    assert validate_link_set(proj, catalog, ["b", "a", "b", ""]) == ["b", "a"]


# --- resolution: one definition for scanner and sync ---------------------


def test_links_and_legacy_paths_resolve_to_one_set(tmp_path):
    """The scanner excludes these roots and the sync indexes them. If the two
    disagree, content either vanishes from search or appears twice."""
    proj_dir = tmp_path / "alpha"
    dep = tmp_path / "shared" / "odoo"
    _dirs(proj_dir, dep)
    entry = DependencyConfig(id="odoo", path=str(dep))
    project = ProjectConfig(id="alpha", path=str(proj_dir), dependencies=["odoo"],
                            dependency_paths=[str(dep)])   # same folder, both ways
    resolved = resolve_project_dependency_paths(project, [entry])
    assert len(resolved) == 1, "one folder resolved to two roots"


def test_a_disabled_entry_contributes_nothing(tmp_path):
    dep = tmp_path / "odoo"
    _dirs(dep)
    entry = DependencyConfig(id="odoo", path=str(dep), enabled=False)
    project = ProjectConfig(id="alpha", path=str(tmp_path), dependencies=["odoo"])
    assert resolve_project_dependency_paths(project, [entry]) == []


def test_an_unknown_link_is_ignored_not_fatal(tmp_path):
    """A sync must never fail wholesale because one entry was deleted
    underneath it — the write path is what refuses bad links."""
    project = ProjectConfig(id="alpha", path=str(tmp_path), dependencies=["ghost"])
    assert resolve_project_dependency_paths(project, []) == []


# --- migration from the legacy field ------------------------------------


def test_legacy_paths_become_catalog_entries_and_links(tmp_path):
    proj = tmp_path / "alpha"
    dep = tmp_path / "shared" / "odoo"
    _dirs(proj, dep)
    s = Settings(projects=[ProjectConfig(id="alpha", path=str(proj),
                                         dependency_paths=[str(dep)])])
    assert [d.id for d in s.dependencies] == ["odoo"]
    assert s.projects[0].dependencies == ["odoo"]


def test_two_projects_on_one_folder_collapse_to_a_single_entry(tmp_path):
    """The deduplication the old model could only reach by accident."""
    a, b = tmp_path / "a", tmp_path / "b"
    dep = tmp_path / "shared" / "odoo"
    _dirs(a, b, dep)
    s = Settings(projects=[
        ProjectConfig(id="a", path=str(a), dependency_paths=[str(dep)]),
        ProjectConfig(id="b", path=str(b), dependency_paths=[str(dep)]),
    ])
    assert len(s.dependencies) == 1
    assert s.projects[0].dependencies == s.projects[1].dependencies == ["odoo"]


def test_two_different_folders_with_one_name_get_distinct_ids(tmp_path):
    """`<project>/odoo` is the common Odoo layout, so a name clash is the
    normal case, not an edge case."""
    a, b = tmp_path / "alpha", tmp_path / "beta"
    _dirs(a / "odoo", b / "odoo")
    s = Settings(projects=[
        ProjectConfig(id="a", path=str(a), dependency_paths=[str(a / "odoo")]),
        ProjectConfig(id="b", path=str(b), dependency_paths=[str(b / "odoo")]),
    ])
    ids = [d.id for d in s.dependencies]
    assert len(ids) == len(set(ids)) == 2
    assert s.projects[0].dependencies != s.projects[1].dependencies


def test_a_relative_legacy_path_is_anchored_to_its_project(tmp_path):
    proj = tmp_path / "alpha"
    _dirs(proj / "platform" / "odoo")
    s = Settings(projects=[ProjectConfig(id="alpha", path=str(proj),
                                         dependency_paths=["platform/odoo"])])
    assert Path(s.dependencies[0].path).name == "odoo"
    assert Path(s.dependencies[0].path).is_absolute()


def test_the_legacy_field_is_consumed_so_it_cannot_become_a_dead_control(tmp_path):
    """Leaving both populated makes clearing the legacy field a silent no-op:
    the adopted link still stands. Same treatment `index_source_code` gets."""
    proj = tmp_path / "alpha"
    _dirs(proj / "platform" / "odoo")
    s = Settings(projects=[ProjectConfig(id="alpha", path=str(proj),
                                         dependency_paths=["platform/odoo"])])
    assert s.projects[0].dependency_paths == []
    assert s.projects[0].dependencies == ["odoo"]


def test_adoption_is_idempotent(tmp_path):
    """Settings are constructed repeatedly; adoption must not accumulate."""
    proj = tmp_path / "alpha"
    _dirs(proj / "platform" / "odoo")
    kwargs = dict(projects=[ProjectConfig(id="alpha", path=str(proj),
                                          dependency_paths=["platform/odoo"])])
    first = Settings(**kwargs)
    again = Settings(dependencies=first.dependencies,
                     projects=[ProjectConfig(id="alpha", path=str(proj),
                                             dependencies=["odoo"])])
    assert len(again.dependencies) == 1
    assert again.projects[0].dependencies == ["odoo"]


def test_an_existing_catalog_entry_is_reused_by_adoption(tmp_path):
    proj = tmp_path / "alpha"
    dep = tmp_path / "shared" / "odoo"
    _dirs(proj, dep)
    s = Settings(
        dependencies=[DependencyConfig(id="odoo-core", name="Odoo", path=str(dep))],
        projects=[ProjectConfig(id="alpha", path=str(proj), dependency_paths=[str(dep)])],
    )
    assert len(s.dependencies) == 1
    assert s.projects[0].dependencies == ["odoo-core"]


# --- the end-to-end effect ----------------------------------------------


def test_a_linked_dependency_is_indexed_once_and_kept_out_of_the_project():
    """The three properties the whole feature rests on, driven by a LINK."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _ = _project(tmp, "alpha")
        entry = DependencyConfig(id="odoo", path=str(proj / "platform" / "odoo"))
        project = ProjectConfig(id="alpha", path=str(proj), mode="general",
                                dependencies=["odoo"])
        settings = Settings(
            content_root=str(tmp), qdrant_path=str(tmp / "q"), state_db=str(tmp / "s.db"),
            data_dir=str(tmp / "d"), collection_strategy="per_project",
            projects=[project], dependencies=[entry],
        )
        from ragtools.service.owner import QdrantOwner
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        try:
            owner.run_full_index()
            synced = owner.sync_frameworks()
            assert len(synced) == 1
            collection = synced[0]["collection"]

            project_paths = _paths_in(owner, owner.router.write_collection("alpha"))
            assert not [p for p in project_paths if "platform/odoo" in p], (
                "the linked framework was indexed into the project's own collection")
            assert any("core_mod" in p for p in _paths_in(owner, collection))
        finally:
            owner.close()


def test_two_projects_linking_one_entry_share_a_single_collection():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        alpha, _ = _project(tmp, "alpha")
        beta, _ = _project(tmp, "beta")
        # ONE catalog entry, used by both — the model's whole point.
        entry = DependencyConfig(id="odoo", path=str(alpha / "platform" / "odoo"))
        settings = Settings(
            content_root=str(tmp), qdrant_path=str(tmp / "q"), state_db=str(tmp / "s.db"),
            data_dir=str(tmp / "d"), collection_strategy="per_project",
            dependencies=[entry],
            projects=[
                ProjectConfig(id="alpha", path=str(alpha), mode="general",
                              dependencies=["odoo"]),
                ProjectConfig(id="beta", path=str(beta), mode="general",
                              dependencies=["odoo"]),
            ],
        )
        from ragtools.service.owner import QdrantOwner
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        try:
            owner.run_full_index()
            synced = [e for e in owner.sync_frameworks() if e.get("action") != "released"]
            collections = {e["collection"] for e in synced}
            assert len(collections) == 1, "one entry produced two collections"
            assert [e["created"] for e in synced] == [True, False], "the corpus was re-created"
        finally:
            owner.close()


def test_unlinking_one_project_leaves_the_other_reading_the_corpus():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        alpha, _ = _project(tmp, "alpha")
        beta, _ = _project(tmp, "beta")
        entry = DependencyConfig(id="odoo", path=str(alpha / "platform" / "odoo"))
        a = ProjectConfig(id="alpha", path=str(alpha), mode="general", dependencies=["odoo"])
        b = ProjectConfig(id="beta", path=str(beta), mode="general", dependencies=["odoo"])
        settings = Settings(
            content_root=str(tmp), qdrant_path=str(tmp / "q"), state_db=str(tmp / "s.db"),
            data_dir=str(tmp / "d"), collection_strategy="per_project",
            dependencies=[entry], projects=[a, b],
        )
        from ragtools.service.owner import QdrantOwner
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        try:
            owner.run_full_index()
            collection = [e for e in owner.sync_frameworks()
                          if e.get("action") != "released"][0]["collection"]

            b.dependencies = []                      # beta un-selects it
            released = [e for e in owner.sync_frameworks() if e.get("action") == "released"]
            assert len(released) == 1 and released[0]["dropped"] is False
            assert any("core_mod" in p for p in _paths_in(owner, collection))
        finally:
            owner.close()


# --- catalog deletion ----------------------------------------------------


def test_deleting_a_used_entry_is_refused_and_names_the_projects(tmp_path):
    from fastapi import HTTPException

    from ragtools.service.routes import dependency_delete

    proj, _ = _project(tmp_path, "alpha")
    entry = DependencyConfig(id="odoo", name="Odoo", path=str(proj / "platform" / "odoo"))
    projects = [ProjectConfig(id="alpha", path=str(proj), dependencies=["odoo"])]
    with _service_owner(tmp_path, projects, dependencies=[entry]):
        with pytest.raises(HTTPException) as raised:
            dependency_delete("odoo")
    assert raised.value.status_code == 409
    assert "alpha" in raised.value.detail


def test_cascade_delete_unlinks_everywhere(tmp_path):
    projects = [ProjectConfig(id="a", path=str(tmp_path), dependencies=["odoo", "sdk"]),
                ProjectConfig(id="b", path=str(tmp_path), dependencies=["odoo"])]
    changed = unlink_everywhere(projects, "odoo")
    assert changed == ["a", "b"]
    assert projects[0].dependencies == ["sdk"]
    assert projects[1].dependencies == []


def test_projects_using_reports_every_linker(tmp_path):
    projects = [ProjectConfig(id="a", path=str(tmp_path), dependencies=["odoo"]),
                ProjectConfig(id="b", path=str(tmp_path), dependencies=[]),
                ProjectConfig(id="c", path=str(tmp_path), dependencies=["odoo"])]
    assert projects_using(projects, "odoo") == ["a", "c"]


# --- the API -------------------------------------------------------------


def test_the_catalog_api_round_trips(tmp_path):
    from ragtools.service.routes import (
        DependencyCreateRequest, ProjectDependencyLinkRequest,
        dependencies_list, dependency_create, project_dependencies_set,
    )

    proj, _ = _project(tmp_path, "alpha")
    projects = [ProjectConfig(id="alpha", path=str(proj), mode="general")]
    with _service_owner(tmp_path, projects) as owner:
        dependency_create(DependencyCreateRequest(
            id="odoo", name="Odoo core", path=str(proj / "platform" / "odoo")))
        listed = dependencies_list()["dependencies"]
        assert [d["id"] for d in listed] == ["odoo"]
        assert listed[0]["projects"] == [], "nothing links it yet"
        assert listed[0]["framework"] == "odoo", "detection did not run"

        project_dependencies_set("alpha", ProjectDependencyLinkRequest(dependencies=["odoo"]))
        assert dependencies_list()["dependencies"][0]["projects"] == ["alpha"]


def test_the_api_refuses_an_illegal_link(tmp_path):
    from fastapi import HTTPException

    from ragtools.service.routes import (
        ProjectDependencyLinkRequest, project_dependencies_set,
    )

    proj, _ = _project(tmp_path, "alpha")
    entry = DependencyConfig(id="self", name="Alpha itself", path=str(proj))
    projects = [ProjectConfig(id="alpha", path=str(proj))]
    with _service_owner(tmp_path, projects, dependencies=[entry]):
        with pytest.raises(HTTPException) as raised:
            project_dependencies_set("alpha",
                                     ProjectDependencyLinkRequest(dependencies=["self"]))
    assert raised.value.status_code == 422
    assert "cannot be used here" in raised.value.detail


def test_the_options_endpoint_explains_why_an_entry_is_unusable(tmp_path):
    from ragtools.service.routes import project_dependency_options

    proj, _ = _project(tmp_path, "alpha")
    entries = [DependencyConfig(id="self", path=str(proj)),
               DependencyConfig(id="odoo", path=str(proj / "platform" / "odoo"))]
    projects = [ProjectConfig(id="alpha", path=str(proj))]
    with _service_owner(tmp_path, projects, dependencies=entries):
        options = {o["id"]: o for o in project_dependency_options("alpha")["options"]}

    assert options["self"]["selectable"] is False
    assert "project's own folder" in options["self"]["reason"]
    assert options["odoo"]["selectable"] is True
    assert options["odoo"]["reason"] == ""


def test_the_legacy_project_api_still_works_and_creates_catalog_entries(tmp_path):
    """`dependency_paths` on the project API is a supported INPUT — it is
    translated into the catalog rather than stored as a second source of
    truth."""
    from ragtools.service.routes import ProjectUpdateRequest, project_update

    proj, _ = _project(tmp_path, "alpha")
    projects = [ProjectConfig(id="alpha", path=str(proj), mode="general")]
    with _service_owner(tmp_path, projects) as owner:
        project_update("alpha", ProjectUpdateRequest(
            dependency_paths=[str(proj / "platform" / "odoo")]))
        settings = owner.settings
        assert len(settings.dependencies) == 1
        assert settings.projects[0].dependencies == [settings.dependencies[0].id]
        assert settings.projects[0].dependency_paths == [], "stored as a second source of truth"


# --- MCP -----------------------------------------------------------------


def test_the_dependency_mcp_tools_exist_and_are_gateable():
    """Optional tools are only registered when the user enables them, so a
    tool missing from the registration table is invisible forever."""
    import inspect

    from ragtools.integration import mcp_server

    source = inspect.getsource(mcp_server._register_ops_tools)
    for name in ("list_dependencies", "add_dependency",
                 "set_project_dependencies", "remove_dependency"):
        assert hasattr(mcp_server, name), f"{name} is not defined"
        assert f'("{name}"' in source, f"{name} is never registered"


def test_the_destructive_dependency_tool_requires_a_confirm_token(monkeypatch):
    """Checked with the service UP — availability is gated first (the same
    order `set_project_mode` uses), so a service-down run would never reach
    the safety gate this test is about."""
    from ragtools.integration import mcp_server

    monkeypatch.setattr(mcp_server, "_ops_state", _proxy_state())

    result = mcp_server.remove_dependency("odoo", confirm_token="wrong")
    assert result["ok"] is False
    assert "confirm_token" in result["error"]


def test_dependency_writes_refuse_without_the_service():
    """Config writes cannot be persisted in direct mode; saying so beats a
    write that appears to succeed and vanishes."""
    from ragtools.integration import mcp_server

    for call in (lambda: mcp_server.add_dependency("odoo", "C:/x"),
                 lambda: mcp_server.set_project_dependencies("alpha", []),
                 lambda: mcp_server.list_dependencies()):
        result = call()
        assert result["ok"] is False
        assert "service" in result["error"].lower()


def test_set_project_dependencies_refuses_a_missing_list(monkeypatch):
    """`None` is not "leave unchanged" for a REPLACE operation — treating it as
    such would silently clear or silently ignore, depending on the reader."""
    from ragtools.integration import mcp_server

    monkeypatch.setattr(mcp_server, "_ops_state", _proxy_state())

    result = mcp_server.set_project_dependencies("alpha", None)
    assert result["ok"] is False
    assert "[]" in result["error"] or "list" in result["error"]


# --- the UI ---------------------------------------------------------------


def test_the_project_form_offers_a_multi_select_not_a_path_box(tmp_path):
    from ragtools.service import pages

    proj, _ = _project(tmp_path, "alpha")
    entry = DependencyConfig(id="odoo", name="Odoo core",
                             path=str(proj / "platform" / "odoo"))
    project = ProjectConfig(id="alpha", path=str(proj), dependencies=["odoo"])
    settings = Settings(content_root=str(tmp_path), qdrant_path=str(tmp_path / "q"),
                        state_db=str(tmp_path / "s.db"), data_dir=str(tmp_path / "d"),
                        projects=[project], dependencies=[entry])
    original = pages.get_settings
    pages.get_settings = lambda: settings
    try:
        html = pages._deps_field(project)
    finally:
        pages.get_settings = original

    assert 'type="checkbox"' in html
    assert 'name="dependencies"' in html
    assert "checked" in html                       # the linked one is selected
    assert "<textarea" not in html                 # the old path box is gone
    assert 'name="deps_present"' in html           # disambiguates "none selected"


def test_an_unusable_entry_is_shown_disabled_with_its_reason(tmp_path):
    """Hiding it makes an entry the user definitely added look like it never
    was."""
    from ragtools.service import pages

    proj, _ = _project(tmp_path, "alpha")
    entry = DependencyConfig(id="self", name="Alpha itself", path=str(proj))
    project = ProjectConfig(id="alpha", path=str(proj))
    settings = Settings(content_root=str(tmp_path), qdrant_path=str(tmp_path / "q"),
                        state_db=str(tmp_path / "s.db"), data_dir=str(tmp_path / "d"),
                        projects=[project], dependencies=[entry])
    original = pages.get_settings
    pages.get_settings = lambda: settings
    try:
        html = pages._deps_field(project)
    finally:
        pages.get_settings = original

    assert "Alpha itself" in html
    assert "disabled" in html
    assert "project&#x27;s own folder" in html or "project's own folder" in html


def test_the_empty_catalog_points_at_the_dependencies_page(tmp_path):
    from ragtools.service import pages

    project = ProjectConfig(id="alpha", path=str(tmp_path))
    settings = Settings(content_root=str(tmp_path), qdrant_path=str(tmp_path / "q"),
                        state_db=str(tmp_path / "s.db"), data_dir=str(tmp_path / "d"),
                        projects=[project])
    original = pages.get_settings
    pages.get_settings = lambda: settings
    try:
        html = pages._deps_field(project)
    finally:
        pages.get_settings = original

    assert "/dependencies" in html
    assert "catalog is empty" in html


def test_the_dependencies_page_is_reachable_from_the_nav():
    base = (Path(__file__).resolve().parents[1] / "src" / "ragtools" / "service"
            / "templates" / "base.html").read_text(encoding="utf-8")
    assert 'href="/dependencies"' in base


def test_saving_a_form_without_the_selector_does_not_clear_links(tmp_path, monkeypatch):
    """Unchecked boxes submit nothing, so "no key" is ambiguous. Without the
    presence marker, any form lacking the selector clears every link."""
    from ragtools.service import pages

    proj, _ = _project(tmp_path, "alpha")
    project = ProjectConfig(id="alpha", path=str(proj), dependencies=["odoo"])
    settings = Settings(content_root=str(tmp_path), qdrant_path=str(tmp_path / "q"),
                        state_db=str(tmp_path / "s.db"), data_dir=str(tmp_path / "d"),
                        projects=[project],
                        dependencies=[DependencyConfig(
                            id="odoo", path=str(proj / "platform" / "odoo"))])
    calls = []
    monkeypatch.setattr("ragtools.service.routes.project_update",
                        lambda pid, req: {"status": "updated"})
    monkeypatch.setattr("ragtools.service.routes.project_dependencies_set",
                        lambda pid, req: calls.append(req.dependencies))
    monkeypatch.setattr(pages, "get_settings", lambda: settings)

    pages.ui_projects_save("alpha", name="A", path=str(proj), ignore_patterns="",
                           mode="general")
    assert calls == [], "a form with no selector cleared the links"

    pages.ui_projects_save("alpha", name="A", path=str(proj), ignore_patterns="",
                           mode="general", dependencies=[], deps_present="1")
    assert calls == [[]], "an explicitly empty selection did not reach the writer"


# --- indexed once, actually ----------------------------------------------


def test_linking_a_second_project_reuses_the_corpus_instead_of_re_importing():
    """The model promises "indexed once, shared". Before this, every dependency
    edit anywhere re-imported EVERY declared corpus — linking a second project
    to a 32,782-file Odoo core re-embedded all of it to change one row.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        alpha, _ = _project(tmp, "alpha")
        beta, _ = _project(tmp, "beta")
        entry = DependencyConfig(id="odoo", path=str(alpha / "platform" / "odoo"))
        a = ProjectConfig(id="alpha", path=str(alpha), mode="general", dependencies=["odoo"])
        b = ProjectConfig(id="beta", path=str(beta), mode="general")
        settings = Settings(
            content_root=str(tmp), qdrant_path=str(tmp / "q"), state_db=str(tmp / "s.db"),
            data_dir=str(tmp / "d"), collection_strategy="per_project",
            dependencies=[entry], projects=[a, b],
        )
        from ragtools.service.owner import QdrantOwner
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        try:
            owner.run_full_index()
            first = owner.sync_frameworks()[0]
            assert first["files_indexed"] > 0, "the first link did not import anything"
            collection = first["collection"]
            points_after_first = owner._count_points(collection)

            b.dependencies = ["odoo"]                  # second project selects it
            second = [e for e in owner.sync_frameworks() if e.get("action") != "released"]
            beta_entry = next(e for e in second if e["project"] == "beta")

            assert beta_entry["files_indexed"] == 0, "the corpus was re-imported"
            assert owner._count_points(collection) == points_after_first
            # ...and beta really can read it.
            assert any("core_mod" in p for p in _paths_in(owner, collection))
        finally:
            owner.close()


def test_an_interrupted_import_is_completed_not_skipped():
    """A LINK is the completeness signal, not a point count: an interrupted run
    leaves plenty of points and no link, and skipping that would strand the
    corpus half-indexed forever."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _ = _project(tmp, "alpha")
        entry = DependencyConfig(id="odoo", path=str(proj / "platform" / "odoo"))
        project = ProjectConfig(id="alpha", path=str(proj), mode="general",
                                dependencies=["odoo"])
        settings = Settings(
            content_root=str(tmp), qdrant_path=str(tmp / "q"), state_db=str(tmp / "s.db"),
            data_dir=str(tmp / "d"), collection_strategy="per_project",
            dependencies=[entry], projects=[project],
        )
        from ragtools.service.owner import QdrantOwner
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        try:
            owner.run_full_index()
            collection = owner.sync_frameworks()[0]["collection"]

            # Simulate the interrupted state: corpus present, link never written.
            record = owner.registry.get("alpha")
            owner.framework_registry.unlink(record.uuid, collection)

            entry_after = owner.sync_frameworks()[0]
            assert entry_after["files_indexed"] > 0, (
                "a corpus with no link was treated as complete and left partial")
        finally:
            owner.close()


def test_refresh_forces_a_re_import_of_a_complete_corpus():
    """Framework corpora are not watcher-refreshed, so there must be a way to
    pick up changes on demand."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _ = _project(tmp, "alpha")
        entry = DependencyConfig(id="odoo", path=str(proj / "platform" / "odoo"))
        project = ProjectConfig(id="alpha", path=str(proj), mode="general",
                                dependencies=["odoo"])
        settings = Settings(
            content_root=str(tmp), qdrant_path=str(tmp / "q"), state_db=str(tmp / "s.db"),
            data_dir=str(tmp / "d"), collection_strategy="per_project",
            dependencies=[entry], projects=[project],
        )
        from ragtools.service.owner import QdrantOwner
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        try:
            owner.run_full_index()
            owner.sync_frameworks()
            assert owner.sync_frameworks()[0]["files_indexed"] == 0     # reused
            assert owner.sync_frameworks(refresh=True)[0]["files_indexed"] > 0
        finally:
            owner.close()


def test_the_inspector_reports_catalog_declarations_not_just_legacy_paths():
    """A project that declares through the catalog read as declaring nothing.

    `dependency_paths` is the legacy input and is consumed into the catalog at
    load, so it is empty for every project after an upgrade. The endpoint
    reported only that field, so a project with a linked, working corpus came
    back as `declared: []` — found while probing a live service for cross-
    project leakage, where "declares nothing but is linked to a framework"
    looked exactly like a leak.
    """
    from ragtools.service.routes import project_dependencies

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _ = _project(tmp, "alpha")
        entry = DependencyConfig(id="odoo", path=str(proj / "platform" / "odoo"))
        project = ProjectConfig(id="alpha", path=str(proj), mode="general",
                                dependencies=["odoo"])
        with _service_owner(tmp, [project], dependencies=[entry]):
            result = project_dependencies("alpha")

    assert result["declared_dependencies"] == ["odoo"]
    assert result["declared"] == [], "the legacy field stays empty, as it should"
