"""Fetch the pinned Qdrant engine into the bundle, verifying what arrives.

`managed` storage supervises a native Qdrant. `find_qdrant_binary` has always
looked for one "alongside the packaged application (the installer ships it
here)" — and no release has ever shipped it. Not in `rag.spec`, not in
`installer.iss`, not in `build.py`, not in `release.yml`. So `managed` was a
mode the product could describe, refuse to guess about, and never actually
enter: every attempt fell back to embedded, correctly reporting a reason nobody
was looking for.

That is the dependency behind making `managed` the migration target. A recommended
architecture that silently degrades on every machine is not a recommendation.

**Digests are pinned in this file.** Qdrant publishes no checksum file, but the
GitHub release API exposes a `digest` per asset, and these were read from it.
Verifying the download against a value committed to the repository is what makes
this a supply-chain step rather than "run whatever the network returned" — a
build must fail on a mismatch, not ship it.
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ragtools.storage_managed import PINNED_QDRANT_VERSION  # noqa: E402

BASE = "https://github.com/qdrant/qdrant/releases/download"

#: (asset, sha256) per (system, machine). Read from the GitHub release API for
#: v1.15.5; update deliberately when PINNED_QDRANT_VERSION moves.
ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("windows", "amd64"): (
        "qdrant-x86_64-pc-windows-msvc.zip",
        "feaeb25c1d2a17fb787f9053de0fa9732bab74ff7e580135fd49e796a889ab91"),
    ("darwin", "arm64"): (
        "qdrant-aarch64-apple-darwin.tar.gz",
        "e712f362e1e9aff18bb062f15c05fa384282ededc7d49f1935792cdc64e25222"),
    ("darwin", "x86_64"): (
        "qdrant-x86_64-apple-darwin.tar.gz",
        "b25f5512f2b696bae84752ff50752a77eec2ce6955e00847816e05c6344d6af9"),
    ("linux", "x86_64"): (
        "qdrant-x86_64-unknown-linux-gnu.tar.gz",
        "56b41911cc0f891ef47d1a4c5cb1f62a423db654ab3694f83cfd37643fea082d"),
}

_MACHINE_ALIASES = {
    "amd64": "amd64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64",
}


def platform_key() -> tuple[str, str]:
    system = platform.system().lower()
    machine = _MACHINE_ALIASES.get(platform.machine().lower(), platform.machine().lower())
    if system == "windows":
        machine = "amd64"
    return system, machine


def binary_name() -> str:
    return "qdrant.exe" if platform.system().lower() == "windows" else "qdrant"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract(archive: Path, into: Path) -> Path:
    """Pull just the engine executable out, wherever the archive keeps it."""
    into.mkdir(parents=True, exist_ok=True)
    wanted = binary_name()

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            member = next((n for n in bundle.namelist()
                           if Path(n).name.lower() == wanted.lower()), None)
            if member is None:
                raise RuntimeError(f"{wanted} is not in {archive.name}: "
                                   f"{bundle.namelist()[:10]}")
            with bundle.open(member) as src, open(into / wanted, "wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive) as bundle:
            member = next((m for m in bundle.getmembers()
                           if Path(m.name).name == wanted), None)
            if member is None:
                raise RuntimeError(f"{wanted} is not in {archive.name}: "
                                   f"{[m.name for m in bundle.getmembers()][:10]}")
            src = bundle.extractfile(member)
            if src is None:
                raise RuntimeError(f"could not read {member.name}")
            with src, open(into / wanted, "wb") as dst:
                shutil.copyfileobj(src, dst)

    target = into / wanted
    if platform.system().lower() != "windows":
        target.chmod(0o755)
    return target


def fetch(dest: Path, *, force: bool = False) -> Path:
    key = platform_key()
    if key not in ASSETS:
        raise SystemExit(
            f"no pinned Qdrant asset for {key}. Supported: {sorted(ASSETS)}.\n"
            "Managed storage cannot be packaged for this platform; the product "
            "falls back to embedded and says so."
        )
    asset, expected = ASSETS[key]
    target = dest / binary_name()
    if target.is_file() and not force:
        print(f"already present: {target}")
        return target

    url = f"{BASE}/v{PINNED_QDRANT_VERSION}/{asset}"
    print(f"fetching {url}")
    with tempfile.TemporaryDirectory() as raw:
        archive = Path(raw) / asset
        urllib.request.urlretrieve(url, archive)  # noqa: S310 — pinned vendor URL
        actual = sha256(archive)
        if actual != expected:
            raise SystemExit(
                f"checksum mismatch for {asset}\n  expected {expected}\n"
                f"  actual   {actual}\n"
                "Refusing to package an artifact that is not the pinned one."
            )
        print(f"sha256 verified: {actual}")
        binary = extract(archive, dest)

    print(f"installed: {binary} ({binary.stat().st_size / 1048576:.1f} MB)")
    return binary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", default="dist/rag/bin",
                        help="where the engine is placed (default: dist/rag/bin)")
    parser.add_argument("--force", action="store_true", help="re-download")
    args = parser.parse_args(argv)

    fetch(Path(args.dest).resolve(), force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
