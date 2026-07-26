"""The framework/dependency architecture, end to end.

Declaring a dependency has to make three things simultaneously true. Doing only
some of them is worse than doing none:

1. the framework tree is **excluded** from the project's own scan;
2. the framework tree is **indexed into its own collection** — the scanner
   already excluded it, so without this step declaring a dependency *deletes*
   content from search;
3. the project's own collection is **purged** of that tree, so adopting a
   dependency on an already-indexed project does not leave a duplicate copy.

Plus the invariants around them: one corpus per build identity, different
identities stay separate, custom code never leaves the project collection, and
no project can see another project's code through a shared framework.

Plan: docs/planning/RAG_STABILITY_HARDENING_PLAN.md (H4) and the release scope.
"""

import tempfile
from pathlib import Path

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.frameworks import exclusion_globs_for, resolve_dependency_roots
from ragtools.service.owner import QdrantOwner


def _odoo(root: Path, *, version="19.0", enterprise=False, heads=None, addons=3):
    (root / "odoo").mkdir(parents=True, exist_ok=True)
    major, minor = version.split(".")
    (root / "odoo" / "release.py").write_text(
        f"version_info = ({major}, {minor}, 0, 'final', 0, "
        f"{'\'e\'' if enterprise else '\'f\''})\n", encoding="utf-8")
    (root / "odoo-bin").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    if heads:
        (root / "repos_heads").write_text(heads, encoding="utf-8")
    for i in range(addons):
        d = root / "addons" / f"core_mod_{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "README.md").write_text(
            f"# Core module {i}\n\nFramework recordset caching and ORM internals.\n",
            encoding="utf-8")
    return root


def _project(root: Path, name: str, *, vendor_inside=True, heads="odoo aaa\n"):
    proj = root / name
    (proj / "custom_addons" / "billing").mkdir(parents=True, exist_ok=True)
    (proj / "custom_addons" / "billing" / "README.md").write_text(
        f"# {name} billing\n\nOur dunning ladder and invoice reconciliation rules.\n",
        encoding="utf-8")
    (proj / "docs").mkdir(parents=True, exist_ok=True)
    (proj / "docs" / "arch.md").write_text(
        f"# {name} architecture\n\nProject-owned deployment notes.\n", encoding="utf-8")
    # NOT `vendor/`: that is a BUILT-IN ignore pattern (ignore.py), so a
    # framework there is already excluded by convention and the interesting
    # cases (adoption, duplication) cannot arise. Real projects frequently keep
    # a core at a non-conventional path — that is what needs declaring.
    vendor = (proj / "platform" / "odoo") if vendor_inside else (root / f"{name}_platform")
    _odoo(vendor, heads=heads)
    return proj, vendor


def _owner(tmp: Path, projects, strategy="per_project", dependencies=None):
    settings = Settings(
        content_root=str(tmp),
        qdrant_path=str(tmp / "qdrant"),
        state_db=str(tmp / "state.db"),
        data_dir=str(tmp / "data"),
        collection_strategy=strategy,
        projects=projects,
        dependencies=dependencies or [],
    )
    return QdrantOwner(settings=settings, client=Settings.get_memory_client())


def _paths_in(owner, collection):
    points, _ = owner.client.scroll(collection_name=collection, limit=5000,
                                    with_payload=True)
    return [p.payload.get("file_path", "") for p in points]


# --- path validation ----------------------------------------------------


def test_the_project_root_is_refused_as_a_dependency(tmp_path):
    """`dependency_paths = ["."]` would exclude every file from the project and
    move the whole thing into a 'framework' corpus — total loss as a typo."""
    roots, problems = resolve_dependency_roots(tmp_path, ["."])
    assert roots == []
    assert any("project root itself" in p for p in problems)


def test_an_ancestor_of_the_project_is_refused(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    roots, problems = resolve_dependency_roots(proj, [str(tmp_path)])
    assert roots == []
    assert any("contains the project root" in p for p in problems)


def test_absolute_and_relative_spellings_resolve_to_one_root(tmp_path):
    proj = tmp_path / "proj"
    (proj / "vendor").mkdir(parents=True)
    roots, _ = resolve_dependency_roots(proj, ["vendor", str(proj / "vendor")])
    assert len(roots) == 1, "the same directory produced two dependency roots"


def test_dotdot_segments_are_normalised(tmp_path):
    proj = tmp_path / "proj"
    (proj / "vendor").mkdir(parents=True)
    roots, _ = resolve_dependency_roots(proj, ["docs/../vendor"])
    assert len(roots) == 1
    assert roots[0].path == (proj / "vendor").resolve()


def test_nested_declarations_collapse_to_the_outermost(tmp_path):
    proj = tmp_path / "proj"
    (proj / "vendor" / "odoo").mkdir(parents=True)
    roots, problems = resolve_dependency_roots(proj, ["vendor", "vendor/odoo"])
    assert len(roots) == 1 and roots[0].relative == "vendor"
    assert any("already covered by" in p for p in problems)


def test_a_dependency_outside_the_project_needs_no_exclusion(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    outside = tmp_path / "shared"
    outside.mkdir()
    roots, problems = resolve_dependency_roots(proj, [str(outside)])
    assert problems == []
    assert len(roots) == 1
    assert roots[0].inside_project is False
    # The project scan is rooted at the project; it never reaches this path.
    assert roots[0].exclusion_globs == []


def test_a_missing_path_is_reported_not_fatal(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    roots, problems = resolve_dependency_roots(proj, ["nope"])
    assert roots == []
    assert any("does not exist" in p for p in problems)


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="no symlink support")
def test_a_symlinked_dependency_resolves_to_its_target(tmp_path):
    """A symlink/junction and its target must be ONE identity, or the same
    corpus is indexed twice into two collections."""
    proj = tmp_path / "proj"
    proj.mkdir()
    real = tmp_path / "real_vendor"
    real.mkdir()
    link = proj / "vendor"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this machine")

    roots, _ = resolve_dependency_roots(proj, ["vendor", str(real)])
    assert len(roots) == 1, "a symlink and its target became two dependency roots"


def test_exclusion_globs_cover_the_tree_not_just_the_directory(tmp_path):
    proj = tmp_path / "proj"
    (proj / "vendor" / "odoo").mkdir(parents=True)
    globs = exclusion_globs_for(proj, ["vendor/odoo"])
    assert "vendor/odoo/" in globs
    assert "vendor/odoo/**" in globs


# --- the three properties together --------------------------------------


def test_framework_files_are_in_the_framework_collection_and_not_the_project():
    """The headline invariant: once, in one place."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, vendor = _project(tmp, "alpha")
        owner = _owner(tmp, [ProjectConfig(id="alpha", path=str(proj), mode="general",
                                           dependency_paths=["platform/odoo"])])
        try:
            owner.run_full_index()
            synced = owner.sync_frameworks()
            assert len(synced) == 1
            entry = synced[0]
            assert entry["chunks_indexed"] > 0, "the framework corpus was not indexed"

            project_paths = _paths_in(owner, owner.router.write_collection("alpha"))
            fw_paths = _paths_in(owner, entry["collection"])

            # Zero framework files in the project collection.
            assert not [p for p in project_paths if "platform/odoo" in p], (
                f"framework files leaked into the project collection: "
                f"{[p for p in project_paths if 'vendor/odoo' in p][:5]}"
            )
            # And present in the framework collection.
            assert any("core_mod" in p for p in fw_paths), (
                "the framework corpus does not contain the framework files"
            )
        finally:
            owner.close()


def test_project_custom_code_stays_in_the_project_collection():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _vendor = _project(tmp, "alpha")
        owner = _owner(tmp, [ProjectConfig(id="alpha", path=str(proj), mode="general",
                                           dependency_paths=["platform/odoo"])])
        try:
            owner.run_full_index()
            entry = owner.sync_frameworks()[0]

            project_paths = _paths_in(owner, owner.router.write_collection("alpha"))
            fw_paths = _paths_in(owner, entry["collection"])

            assert any("custom_addons" in p for p in project_paths), (
                "the project's own custom addons were excluded — a dependency "
                "glob must not swallow project-owned overrides"
            )
            assert not any("custom_addons" in p for p in fw_paths)
        finally:
            owner.close()


def test_default_search_spans_the_project_and_its_framework_with_scope_labels():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _vendor = _project(tmp, "alpha")
        owner = _owner(tmp, [ProjectConfig(id="alpha", path=str(proj), mode="general",
                                           dependency_paths=["platform/odoo"])])
        try:
            owner.run_full_index()
            owner.sync_frameworks()

            own = owner.search("dunning ladder invoice reconciliation",
                               project_id="alpha", score_threshold=0.0, top_k=10)
            assert own and any(r.scope == "project" for r in own)

            fw = owner.search("recordset caching ORM internals",
                              project_id="alpha", score_threshold=0.0, top_k=10)
            assert any(r.scope == "framework" for r in fw), (
                "the linked framework corpus is not reachable from the project"
            )
            for r in fw:
                if r.scope == "framework":
                    assert r.scope_source.startswith("fw_")
        finally:
            owner.close()


def test_no_duplicate_results_for_the_same_file():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _vendor = _project(tmp, "alpha")
        owner = _owner(tmp, [ProjectConfig(id="alpha", path=str(proj), mode="general",
                                           dependency_paths=["platform/odoo"])])
        try:
            owner.run_full_index()
            owner.sync_frameworks()
            hits = owner.search("recordset caching ORM", project_id="alpha",
                                score_threshold=0.0, top_k=25)
            seen = [(h.file_path, h.line_start) for h in hits]
            assert len(seen) == len(set(seen)), f"duplicate results: {seen}"
        finally:
            owner.close()


# --- deduplication across projects --------------------------------------


def test_two_projects_on_the_same_build_share_one_collection():
    """The economic point: one corpus, referenced twice."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        a, _ = _project(tmp, "alpha", heads="odoo same\n")
        b, _ = _project(tmp, "beta", heads="odoo same\n")
        owner = _owner(tmp, [
            ProjectConfig(id="alpha", path=str(a), mode="general",
                          dependency_paths=["platform/odoo"]),
            ProjectConfig(id="beta", path=str(b), mode="general",
                          dependency_paths=["platform/odoo"]),
        ])
        try:
            owner.run_full_index()
            synced = owner.sync_frameworks()
            collections = {e["collection"] for e in synced}
            assert len(collections) == 1, (
                f"the same build produced {len(collections)} collections: {collections}"
            )
            assert [e["created"] for e in synced] == [True, False], (
                "the second project re-indexed the corpus instead of reusing it"
            )
        finally:
            owner.close()


def test_a_shared_framework_is_not_a_bridge_between_projects():
    """Two projects sharing a corpus must still not see each other's code."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        a, _ = _project(tmp, "alpha", heads="odoo same\n")
        b, _ = _project(tmp, "beta", heads="odoo same\n")
        owner = _owner(tmp, [
            ProjectConfig(id="alpha", path=str(a), mode="general",
                          dependency_paths=["platform/odoo"]),
            ProjectConfig(id="beta", path=str(b), mode="general",
                          dependency_paths=["platform/odoo"]),
        ])
        try:
            owner.run_full_index()
            owner.sync_frameworks()
            hits = owner.search("beta billing dunning", project_id="alpha",
                                score_threshold=0.0, top_k=25)
            leaked = [h.file_path for h in hits if h.file_path.startswith("beta/")]
            assert not leaked, f"alpha saw beta's custom code: {leaked}"
        finally:
            owner.close()


def _collection_of(root: Path) -> str:
    from ragtools.frameworks import detect_framework
    from ragtools.identity import framework_collection_name

    info = detect_framework(root)
    return framework_collection_name(info.name, version=info.version,
                                     edition=info.edition, build_id=info.build_id)


@pytest.mark.parametrize("differing", ["version", "edition"])
def test_different_checkouts_stay_separate(differing, tmp_path):
    """Checkouts carry no build id, so identity is (name, version, edition)."""
    a, b = tmp_path / "a", tmp_path / "b"
    _odoo(a, version="19.0", enterprise=False)
    _odoo(b, version=("18.0" if differing == "version" else "19.0"),
          enterprise=(differing == "edition"))
    assert _collection_of(a) != _collection_of(b), (
        f"a {differing} difference collapsed into one collection"
    )


def test_different_builds_stay_separate(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _odoo(a, heads="odoo aaa\n")
    _odoo(b, heads="odoo bbb\n")
    assert _collection_of(a) != _collection_of(b)


def test_a_checkout_and_a_packaged_build_are_separate(tmp_path):
    """Same version and edition, but one is a build and one is a checkout."""
    src, built = tmp_path / "src", tmp_path / "built"
    _odoo(src)                          # no repos_heads -> checkout
    _odoo(built, heads="odoo aaa\n")    # packaged build
    assert _collection_of(src) != _collection_of(built)


def test_the_build_id_is_authoritative_when_present(tmp_path):
    """Documented contract: a build id IS the identity, so it outranks the
    weaker (version, edition) signal. Two trees claiming the same build are the
    same corpus even if a stray release.py disagrees — the alternative is
    indexing one build twice because a file was edited."""
    a, b = tmp_path / "a", tmp_path / "b"
    _odoo(a, version="19.0", heads="odoo same\n")
    _odoo(b, version="18.0", heads="odoo same\n")
    assert _collection_of(a) == _collection_of(b)


# --- adoption on an already-indexed project ------------------------------


def _adopt(owner, proj):
    """Declare the dependency on a RUNNING owner — the real adoption flow: the
    service is up, the operator edits config, it hot-reloads."""
    owner.update_projects([ProjectConfig(id="alpha", path=str(proj), mode="general",
                                         dependency_paths=["platform/odoo"])])


def test_adopting_a_dependency_purges_it_from_the_project_collection():
    """The migration case: the project was indexed BEFORE the dependency was
    declared, so its own collection already holds the framework files."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _vendor = _project(tmp, "alpha")
        owner = _owner(tmp, [ProjectConfig(id="alpha", path=str(proj), mode="general")])
        try:
            owner.run_full_index()
            before = _paths_in(owner, owner.router.write_collection("alpha"))
            assert [p for p in before if "platform/odoo" in p], (
                "fixture is wrong: the framework files should be in the project"
            )

            _adopt(owner, proj)
            entry = owner.sync_frameworks()[0]
            assert entry["purged_from_project"] > 0, "nothing was purged"

            after = _paths_in(owner, owner.router.write_collection("alpha"))
            assert not [p for p in after if "platform/odoo" in p], (
                "the duplicate framework copy is still in the project collection"
            )
            assert any("custom_addons" in p for p in after), (
                "the purge removed project-owned code"
            )
            assert any("core_mod" in p for p in _paths_in(owner, entry["collection"]))
        finally:
            owner.close()


def test_purge_is_refused_when_the_framework_copy_is_missing(monkeypatch):
    """Deleting the only copy because the framework index silently produced
    nothing turns an optimisation into data loss."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _vendor = _project(tmp, "alpha")
        owner = _owner(tmp, [ProjectConfig(id="alpha", path=str(proj), mode="general")])
        try:
            owner.run_full_index()
            _adopt(owner, proj)
            monkeypatch.setattr(
                owner, "_index_framework_corpus",
                lambda *a, **k: {"files": 0, "chunks": 0},
            )
            entry = owner.sync_frameworks()[0]
            assert entry["purged_from_project"] == 0, (
                "purged the project's copy without a verified framework copy"
            )
            still = _paths_in(owner, owner.router.write_collection("alpha"))
            assert [p for p in still if "platform/odoo" in p], "content was lost"
        finally:
            owner.close()


def test_search_still_finds_the_framework_content_after_adoption():
    """End-to-end: content moved collections, but stayed findable — and is now
    labelled as framework rather than project code."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _vendor = _project(tmp, "alpha")
        owner = _owner(tmp, [ProjectConfig(id="alpha", path=str(proj), mode="general")])
        try:
            owner.run_full_index()
            _adopt(owner, proj)
            owner.sync_frameworks()

            hits = owner.search("recordset caching ORM internals", project_id="alpha",
                                score_threshold=0.0, top_k=15)
            assert any(h.scope == "framework" for h in hits), (
                "framework content became unfindable after adoption"
            )
            own = owner.search("dunning ladder invoice reconciliation",
                               project_id="alpha", score_threshold=0.0, top_k=15)
            assert any(h.scope == "project" for h in own)
        finally:
            owner.close()


def test_sync_is_idempotent():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _vendor = _project(tmp, "alpha")
        owner = _owner(tmp, [ProjectConfig(id="alpha", path=str(proj), mode="general",
                                           dependency_paths=["platform/odoo"])])
        try:
            owner.run_full_index()
            first = owner.sync_frameworks()[0]
            fw_before = len(_paths_in(owner, first["collection"]))

            second = owner.sync_frameworks()[0]
            assert second["created"] is False
            assert second["purged_from_project"] == 0
            assert len(_paths_in(owner, second["collection"])) == fw_before, (
                "re-syncing duplicated the framework corpus"
            )
        finally:
            owner.close()


def test_reindexing_after_adoption_does_not_reintroduce_the_duplicate():
    """The scanner exclusion and the purge have to agree."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj, _vendor = _project(tmp, "alpha")
        owner = _owner(tmp, [ProjectConfig(id="alpha", path=str(proj), mode="general",
                                           dependency_paths=["platform/odoo"])])
        try:
            owner.run_full_index()
            owner.sync_frameworks()
            owner.run_full_index()          # a later routine re-index
            paths = _paths_in(owner, owner.router.write_collection("alpha"))
            assert not [p for p in paths if "platform/odoo" in p], (
                "a re-index put the framework files back into the project"
            )
        finally:
            owner.close()
