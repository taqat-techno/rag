"""Release metadata: reproducible, verifiable, and honest about what it is.

Signing is not tested here because it cannot be — it needs credentials this
repository must never hold. What IS testable is everything the signing step
consumes and the release gate checks: a build stamp that records a dirty tree
instead of hiding it, checksums that actually catch a modified artifact, and an
SBOM built from what is installed rather than what was declared.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.release_artifacts import (
    BUILD_INFO_FILE,
    CHECKSUM_FILE,
    SBOM_FILE,
    BuildInfo,
    Component,
    build_info,
    git_commit,
    sbom,
    sha256,
    verify_checksums,
    write_checksums,
    write_release_metadata,
)


class FakeRun:
    def __init__(self, sha="abc1234", status="", code=0):
        self._sha, self._status, self._code = sha, status, code

    def __call__(self, argv):
        class _R:
            pass

        r = _R()
        r.returncode = self._code
        r.stdout = self._sha if "rev-parse" in argv else self._status
        return r


# --- build stamp ----------------------------------------------------------


def test_a_dirty_tree_is_recorded_not_hidden(tmp_path):
    """An artifact built from uncommitted state is the most confusing thing to
    debug in the field: nothing on disk explains what is running."""
    info = build_info("3.0.0", tmp_path, runner=FakeRun(status=" M src/x.py"))
    assert info.dirty is True
    assert info.commit == "abc1234"


def test_a_clean_tree_is_not_marked_dirty(tmp_path):
    assert build_info("3.0.0", tmp_path, runner=FakeRun()).dirty is False


def test_a_source_tarball_without_git_still_builds(tmp_path):
    """Distributions are built from tarballs that have no `.git`; refusing there
    would make the source distribution unbuildable."""
    def _boom(argv):
        raise FileNotFoundError("git")

    info = build_info("3.0.0", tmp_path, runner=_boom)
    assert info.commit == ""
    assert info.version == "3.0.0"


def test_the_build_stamp_is_reproducible_from_source_date_epoch(tmp_path):
    """Two builds of one commit must produce the same manifest, or
    "reproducible build metadata" is a claim nobody can check."""
    first = build_info("3.0.0", tmp_path, source_date_epoch=1_700_000_000,
                       runner=FakeRun())
    second = build_info("3.0.0", tmp_path, source_date_epoch=1_700_000_000,
                        runner=FakeRun())
    assert first.built_at == second.built_at
    assert first.to_json() == second.to_json()


def test_the_stamp_records_the_pinned_qdrant_version(tmp_path):
    """Which storage engine a build shipped against is a support question that
    comes up on every incident."""
    info = build_info("3.0.0", tmp_path, qdrant_version="1.15.5", runner=FakeRun())
    assert json.loads(info.to_json())["qdrant_version"] == "1.15.5"


# --- checksums ------------------------------------------------------------


def test_checksums_verify_unmodified_artifacts(tmp_path):
    art = tmp_path / "RAGTools-Setup-3.0.0-x64.exe"
    art.write_bytes(b"installer bytes")

    manifest = write_checksums([art], tmp_path)

    assert verify_checksums(manifest, tmp_path) == []


def test_a_modified_artifact_is_caught(tmp_path):
    art = tmp_path / "ragtools-3.0.0-linux-x86_64.tar.gz"
    art.write_bytes(b"original")
    manifest = write_checksums([art], tmp_path)

    art.write_bytes(b"tampered")

    assert verify_checksums(manifest, tmp_path) == [art.name]


def test_a_missing_artifact_is_caught(tmp_path):
    art = tmp_path / "RAGTools-3.0.0.dmg"
    art.write_bytes(b"x")
    manifest = write_checksums([art], tmp_path)
    art.unlink()

    assert verify_checksums(manifest, tmp_path) == [art.name]


def test_the_manifest_is_sorted_so_it_is_reproducible(tmp_path):
    for name in ("z.tar.gz", "a.exe", "m.dmg"):
        (tmp_path / name).write_bytes(name.encode())

    manifest = write_checksums(
        [tmp_path / "z.tar.gz", tmp_path / "a.exe", tmp_path / "m.dmg"], tmp_path)
    names = [line.split("  ")[1] for line in
             manifest.read_text(encoding="utf-8").strip().splitlines()]

    assert names == sorted(names)


def test_the_manifest_format_is_sha256sum_compatible(tmp_path):
    """`sha256sum -c SHA256SUMS` has to work — two spaces, hash first."""
    art = tmp_path / "a.bin"
    art.write_bytes(b"x")
    line = write_checksums([art], tmp_path).read_text(encoding="utf-8").strip()

    digest, sep, name = line.partition("  ")
    assert sep == "  "
    assert len(digest) == 64
    assert name == "a.bin"
    assert digest == sha256(art)


# --- SBOM -----------------------------------------------------------------


def test_the_sbom_is_valid_cyclonedx_with_sorted_components():
    document = sbom([
        Component("qdrant-client", "1.15.5", "Apache-2.0"),
        Component("fastapi", "0.111.0", "MIT"),
    ], version="3.0.0")

    assert document["bomFormat"] == "CycloneDX"
    assert document["specVersion"] == "1.5"
    assert document["metadata"]["component"]["version"] == "3.0.0"
    assert [c["name"] for c in document["components"]] == ["fastapi", "qdrant-client"]


def test_sbom_components_carry_a_purl_and_licence():
    document = sbom([Component("fastapi", "0.111.0", "MIT")], version="3.0.0")
    component = document["components"][0]
    assert component["purl"] == "pkg:pypi/fastapi@0.111.0"
    assert component["licenses"][0]["license"]["id"] == "MIT"


def test_a_component_without_a_declared_licence_omits_the_field():
    """Recording an unknown licence as some default is how a compliance review
    ends up trusting a value nobody checked."""
    document = sbom([Component("mystery", "1.0")], version="3.0.0")
    assert "licenses" not in document["components"][0]


def test_the_sbom_describes_what_is_installed_not_what_was_declared(tmp_path):
    """What ships is what was resolved into the environment; pyproject lists
    ranges, and a range is not a bill of materials."""
    from scripts.release_artifacts import installed_components

    components = installed_components()
    names = {c.name.lower() for c in components}
    assert "fastapi" in names or "qdrant-client" in names
    assert all(c.version for c in components if c.name.lower() == "fastapi")


# --- the bundle -----------------------------------------------------------


def test_release_metadata_writes_every_required_file(tmp_path):
    artifact = tmp_path / "RAGTools-Setup-3.0.0-x64.exe"
    artifact.write_bytes(b"installer")
    out = tmp_path / "out"

    written = write_release_metadata(
        out,
        info=BuildInfo(version="3.0.0", commit="abc1234", platform="windows", arch="x86_64"),
        artifacts=[artifact],
        components=[Component("fastapi", "0.111.0", "MIT")],
    )

    assert (out / BUILD_INFO_FILE).exists()
    assert (out / SBOM_FILE).exists()
    assert (out / CHECKSUM_FILE).exists()
    assert set(written) == {"build_info", "sbom", "checksums"}
    assert verify_checksums(out / CHECKSUM_FILE, tmp_path) == []


def test_metadata_for_a_platform_can_be_produced_from_any_host(tmp_path):
    """A Linux artifact's manifest must be generatable from a Windows build
    host — otherwise the release needs three machines available at once just to
    assemble it."""
    out = tmp_path / "out"
    written = write_release_metadata(
        out,
        info=BuildInfo(version="3.0.0", platform="linux", arch="aarch64"),
        components=[Component("fastapi", "0.111.0", "MIT")],
    )
    stamp = json.loads((out / BUILD_INFO_FILE).read_text(encoding="utf-8"))

    assert stamp["platform"] == "linux"
    assert stamp["arch"] == "aarch64"
    assert "checksums" not in written      # nothing to checksum yet


def test_no_signing_material_is_produced_or_expected(tmp_path):
    """Signing needs credentials this repository must never hold. The pipeline
    emits what a signing step CONSUMES and nothing more."""
    out = tmp_path / "out"
    write_release_metadata(out, info=BuildInfo(version="3.0.0"), components=[])

    produced = {p.name for p in out.iterdir()}
    assert not any(p.endswith((".p12", ".pfx", ".key", ".pem")) for p in produced)
