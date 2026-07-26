"""Release-artifact metadata: build stamp, checksums, SBOM, licence inventory.

Everything here is deterministic and platform-agnostic, so a Linux artifact's
metadata can be produced and verified from any build host. That property is what
lets the release be assembled without three machines being simultaneously
available — the artifacts still have to be *built* and *validated* per platform,
but their manifests do not have to be.

Signing is deliberately absent: it needs credentials this repository must never
contain (Apple Developer ID, Windows Authenticode). The pipeline emits the
checksum file that a signing step consumes, and the release gate refuses an
unsigned macOS artifact.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

CHECKSUM_FILE = "SHA256SUMS"
SBOM_FILE = "sbom.cyclonedx.json"
BUILD_INFO_FILE = "build-info.json"


@dataclass
class BuildInfo:
    """What was built, from what, for what. Surfaced by ``/identity``.

    ``source_date_epoch`` is honoured so two builds of the same commit produce
    the same manifest — without it "reproducible build metadata" is a claim
    nobody can check.
    """

    version: str
    commit: str = ""
    dirty: bool = False
    built_at: str = ""
    platform: str = ""
    arch: str = ""
    python: str = ""
    qdrant_version: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def git_commit(repo: Path, *, runner=None) -> tuple[str, bool]:
    """``(short-sha, dirty)``. A dirty build is recorded, never hidden.

    An artifact built from uncommitted state is the single most confusing thing
    to debug in the field: nothing on disk explains what is running.
    """
    run = runner or (lambda argv: subprocess.run(
        argv, cwd=str(repo), capture_output=True, text=True, timeout=15))
    try:
        sha = run(["git", "rev-parse", "--short", "HEAD"])
        status = run(["git", "status", "--porcelain"])
    except Exception:  # noqa: BLE001 — a tarball has no git; that is not fatal
        return "", False
    if sha.returncode != 0:
        return "", False
    return sha.stdout.strip(), bool(status.stdout.strip())


def build_info(
    version: str,
    repo: Path,
    *,
    platform_name: str = "",
    arch: str = "",
    python: str = "",
    qdrant_version: str = "",
    source_date_epoch: Optional[int] = None,
    runner=None,
) -> BuildInfo:
    from datetime import datetime, timezone

    commit, dirty = git_commit(repo, runner=runner)
    stamp = (
        datetime.fromtimestamp(source_date_epoch, tz=timezone.utc)
        if source_date_epoch is not None
        else datetime.now(timezone.utc)
    )
    return BuildInfo(
        version=version, commit=commit, dirty=dirty,
        built_at=stamp.isoformat(timespec="seconds"),
        platform=platform_name, arch=arch, python=python,
        qdrant_version=qdrant_version,
    )


def sha256(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def write_checksums(artifacts: Iterable[Path], out_dir: Path) -> Path:
    """A `sha256sum -c`-compatible manifest, sorted for reproducibility."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Sorted by NAME, not by the rendered line — sorting by line orders on the
    # hash, which is effectively random and defeats the reproducibility this
    # manifest exists to provide.
    ordered = sorted((Path(a) for a in artifacts), key=lambda p: p.name)
    lines = [f"{sha256(a)}  {a.name}" for a in ordered]
    target = out_dir / CHECKSUM_FILE
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def verify_checksums(manifest: Path, artifact_dir: Path) -> list[str]:
    """Return the names that do NOT match. Empty means everything verified."""
    bad: list[str] = []
    for line in Path(manifest).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, _, name = line.partition("  ")
        candidate = Path(artifact_dir) / name.strip()
        if not candidate.exists() or sha256(candidate) != expected.strip():
            bad.append(name.strip())
    return bad


@dataclass
class Component:
    name: str
    version: str
    licence: str = ""
    purl: str = ""


def sbom(components: Iterable[Component], *, version: str) -> dict:
    """A minimal CycloneDX 1.5 document.

    Minimal on purpose: a hand-rolled SBOM that claims more structure than it
    verifies is worse than a small honest one. Components come from the
    resolved environment, not from the dependency declarations, because what
    ships is what was installed.
    """
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": "ragtools",
                                   "version": version}},
        "components": [
            {
                "type": "library",
                "name": c.name,
                "version": c.version,
                "purl": c.purl or f"pkg:pypi/{c.name}@{c.version}",
                **({"licenses": [{"license": {"id": c.licence}}]} if c.licence else {}),
            }
            for c in sorted(components, key=lambda c: c.name.lower())
        ],
    }


def installed_components(*, distributions=None) -> list[Component]:
    """Resolve what is actually installed in this environment."""
    from importlib import metadata

    out: list[Component] = []
    for dist in (distributions or metadata.distributions()):
        try:
            name = dist.metadata["Name"]
        except Exception:  # noqa: BLE001
            continue
        if not name:
            continue
        out.append(Component(
            name=name,
            version=dist.version or "",
            licence=(dist.metadata.get("License") or "").split("\n")[0][:64],
        ))
    return out


def write_release_metadata(
    out_dir: Path,
    *,
    info: BuildInfo,
    artifacts: Iterable[Path] = (),
    components: Optional[Iterable[Component]] = None,
) -> dict:
    """Emit build-info, SBOM and checksums together. Returns the paths written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    build_path = out_dir / BUILD_INFO_FILE
    build_path.write_text(info.to_json(), encoding="utf-8")

    sbom_path = out_dir / SBOM_FILE
    sbom_path.write_text(
        json.dumps(sbom(components if components is not None else installed_components(),
                        version=info.version), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    written = {"build_info": build_path, "sbom": sbom_path}
    artifacts = [Path(a) for a in artifacts]
    if artifacts:
        written["checksums"] = write_checksums(artifacts, out_dir)
    return written
