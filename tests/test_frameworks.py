"""Recognising a vendored framework, so it is indexed once and referenced.

A project that vendors a framework is mostly not its own code: `khayrgate` here
is 38,136 files, overwhelmingly an Odoo core. Under the shared model that corpus
is embedded once per project that uses it.

Everything needed to do better already existed and was unreachable —
`FrameworkRegistry` deduplicates by build identity, `framework_collection_name`
derives the shared collection, the router already searches a project's linked
corpora — except the step that turns "this path is a framework" into a
registration. `ProjectConfig.dependency_paths`, the field meant to declare it,
was read by nothing.

Detection is pure and writes nothing, so it is tested from a temp directory.
The wiring is opt-in: no project declares a dependency path today, so an
existing install cannot change behaviour until someone sets one.

Plan: docs/planning/RAG_STABILITY_HARDENING_PLAN.md (H4)
"""

import json
import tempfile
from pathlib import Path

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.frameworks import (
    FrameworkInfo, describe_dependency, detect_framework, detector_names,
)
from ragtools.identity import framework_collection_name
from ragtools.service.owner import QdrantOwner


# --- detection ----------------------------------------------------------


def _odoo_tree(root: Path, *, version="19.0", enterprise=False, repos_heads=None):
    (root / "odoo").mkdir(parents=True, exist_ok=True)
    major, minor = version.split(".")
    suffix = "'e'" if enterprise else "'f'"
    (root / "odoo" / "release.py").write_text(
        f"version_info = ({major}, {minor}, 0, 'final', 0, {suffix})\n",
        encoding="utf-8")
    (root / "odoo-bin").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    if repos_heads:
        (root / "repos_heads").write_text(repos_heads, encoding="utf-8")
    return root


def test_odoo_is_detected_with_its_version(tmp_path):
    info = detect_framework(_odoo_tree(tmp_path / "odoo19"))
    assert info is not None
    assert info.name == "odoo"
    assert info.version == "19.0"
    assert info.detector == "odoo"


def test_odoo_enterprise_is_a_different_edition(tmp_path):
    ce = detect_framework(_odoo_tree(tmp_path / "ce", enterprise=False))
    ee = detect_framework(_odoo_tree(tmp_path / "ee", enterprise=True))
    assert ce.edition != ee.edition
    # Different editions must not share a corpus.
    assert framework_collection_name(**_ident(ce)) != framework_collection_name(**_ident(ee))


def _ident(info: FrameworkInfo) -> dict:
    return {"name": info.name, "version": info.version,
            "edition": info.edition, "build_id": info.build_id}


def test_the_same_build_in_two_places_is_one_collection(tmp_path):
    """The dedup that makes the whole model worth it."""
    heads = "odoo abc123\nenterprise def456\n"
    a = detect_framework(_odoo_tree(tmp_path / "copy-a", repos_heads=heads))
    b = detect_framework(_odoo_tree(tmp_path / "copy-b", repos_heads=heads))
    assert a.build_id and a.build_id == b.build_id
    assert framework_collection_name(**_ident(a)) == framework_collection_name(**_ident(b))


def test_different_builds_are_different_collections(tmp_path):
    a = detect_framework(_odoo_tree(tmp_path / "a", repos_heads="odoo aaa\n"))
    b = detect_framework(_odoo_tree(tmp_path / "b", repos_heads="odoo bbb\n"))
    assert a.build_id != b.build_id
    assert framework_collection_name(**_ident(a)) != framework_collection_name(**_ident(b))


def test_a_checkout_without_a_build_id_is_not_a_packaged_build(tmp_path):
    """Absence of a build id is information: `framework_collection_name` keeps
    a git checkout and a packaged build apart."""
    plain = detect_framework(_odoo_tree(tmp_path / "src"))
    built = detect_framework(_odoo_tree(tmp_path / "built", repos_heads="odoo x\n"))
    assert plain.build_id is None and built.build_id
    assert framework_collection_name(**_ident(plain)) != framework_collection_name(**_ident(built))


def test_git_head_is_used_when_there_is_no_repos_heads(tmp_path):
    root = _odoo_tree(tmp_path / "gitco")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")
    info = detect_framework(root)
    assert info.build_id == "a" * 16


def test_npm_package_is_detected(tmp_path):
    root = tmp_path / "sdk"
    root.mkdir()
    (root / "package.json").write_text(
        json.dumps({"name": "@acme/sdk", "version": "3.2.1"}), encoding="utf-8")
    info = detect_framework(root)
    assert info.name == "@acme/sdk" and info.version == "3.2.1"


def test_python_distribution_is_detected(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "acme-lib"\nversion = "1.4.0"\n', encoding="utf-8")
    info = detect_framework(root)
    assert info.name == "acme-lib" and info.version == "1.4.0"


def test_an_ordinary_folder_is_not_a_framework(tmp_path):
    plain = tmp_path / "notes"
    plain.mkdir()
    (plain / "a.md").write_text("# hi\n", encoding="utf-8")
    assert detect_framework(plain) is None


def test_a_declared_dependency_is_described_even_if_unrecognised(tmp_path):
    """The owner said it is a shared dependency; that is not conditional on a
    detector recognising the flavour."""
    plain = tmp_path / "shared-docs"
    plain.mkdir()
    info = describe_dependency(plain)
    assert info is not None
    assert info.name == "shared-docs"
    assert info.detector == "generic"


def test_a_missing_path_describes_as_none(tmp_path):
    assert describe_dependency(tmp_path / "nope") is None
    assert detect_framework(tmp_path / "nope") is None


def test_a_broken_detector_cannot_block_the_others(tmp_path, monkeypatch):
    import ragtools.frameworks as fw

    def explode(_root):
        raise RuntimeError("bad detector")

    monkeypatch.setattr(fw, "_DETECTORS", [("boom", explode)] + fw._DETECTORS)
    info = detect_framework(_odoo_tree(tmp_path / "odoo"))
    assert info is not None and info.name == "odoo"


def test_detectors_are_registered_in_priority_order():
    names = detector_names()
    assert "odoo" in names
    assert names.index("odoo") < names.index("python"), (
        "a generic python detector would shadow Odoo (it ships a pyproject)"
    )


# --- wiring -------------------------------------------------------------


def _owner(tmp: Path, dependency_paths=None, strategy="per_project"):
    proj = tmp / "proj"
    proj.mkdir(exist_ok=True)
    (proj / "app.md").write_text("# App\n\nOur own code lives here.\n", encoding="utf-8")
    settings = Settings(
        content_root=str(tmp),
        qdrant_path=str(tmp / "qdrant"),
        state_db=str(tmp / "state.db"),
        data_dir=str(tmp / "data"),
        collection_strategy=strategy,
        projects=[ProjectConfig(id="p", path=str(proj), mode="docs",
                                dependency_paths=dependency_paths or [])],
    )
    return QdrantOwner(settings=settings, client=Settings.get_memory_client())


def test_no_declared_dependencies_means_no_change():
    """Every project today declares none — this must be a no-op."""
    with tempfile.TemporaryDirectory() as td:
        owner = _owner(Path(td))
        try:
            assert owner.sync_frameworks() == []
        finally:
            owner.close()


def test_a_declared_framework_is_registered_and_linked():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vendor = _odoo_tree(tmp / "vendor" / "odoo19", repos_heads="odoo sha1\n")
        owner = _owner(tmp, dependency_paths=[str(vendor)])
        try:
            linked = owner.sync_frameworks()
            assert len(linked) == 1
            entry = linked[0]
            assert entry["framework"] == "odoo"
            assert entry["version"] == "19.0"
            assert entry["created"] is True

            # And the router now searches it for that project.
            reads = owner.router.read_collections("p")
            assert entry["collection"] in reads
            assert reads[0] == owner.router.write_collection("p"), "own collection first"
        finally:
            owner.close()


def test_syncing_twice_reuses_the_collection():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vendor = _odoo_tree(tmp / "vendor", repos_heads="odoo sha1\n")
        owner = _owner(tmp, dependency_paths=[str(vendor)])
        try:
            first = owner.sync_frameworks()
            second = owner.sync_frameworks()
            assert first[0]["created"] is True
            assert second[0]["created"] is False, "the corpus was registered twice"
            assert first[0]["collection"] == second[0]["collection"]
        finally:
            owner.close()


def test_a_relative_dependency_path_resolves_against_the_project():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj = tmp / "proj"
        proj.mkdir()
        (proj / "app.md").write_text("# App\n", encoding="utf-8")
        _odoo_tree(proj / "vendor" / "odoo")
        owner = _owner(tmp, dependency_paths=["vendor/odoo"])
        try:
            linked = owner.sync_frameworks()
            assert len(linked) == 1 and linked[0]["framework"] == "odoo"
        finally:
            owner.close()


def test_a_missing_dependency_path_is_reported_not_fatal():
    with tempfile.TemporaryDirectory() as td:
        owner = _owner(Path(td), dependency_paths=["does/not/exist"])
        try:
            assert owner.sync_frameworks() == []   # skipped, no exception
        finally:
            owner.close()


def test_shared_mode_ignores_declared_dependencies():
    """Framework corpora are a per-project-collection concept."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vendor = _odoo_tree(tmp / "vendor")
        owner = _owner(tmp, dependency_paths=[str(vendor)], strategy="shared")
        try:
            assert owner.sync_frameworks() == []
        finally:
            owner.close()
