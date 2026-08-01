"""The Winget manifest must describe the artifact it points at.

`winget/RAGTools.RAGTools.installer.yaml` shipped for v3.5.0 carrying

    PackageVersion:   3.5.0
    InstallerUrl:     .../v3.5.0/RAGTools-Setup-3.5.0.exe
    InstallerSha256:  20D81E8E9BE7A77BD4F65155DB930967F83E8041496B61F7647C97B1F3D347FF

and that digest is the **v3.4.0** installer's, byte for byte. The published
v3.5.0 installer hashes to ``1942dba9...a1b3``. Winget verifies the hash before
running anything, so the manifest as written fails every install with a hash
mismatch — the one error mode where the tool is right and the package is wrong.

Nothing caught it because nothing compared the two. The version and URL were
edited by the release process; the digest was not, and a stale digest looks
exactly like a fresh one. So this check exists to make the comparison
mechanical: the manifest's hash is checked against the artifact actually
attached to the release named in its own URL.

It does NOT download the installer by default. GitHub records a digest for every
release asset, so the comparison costs one API call instead of 626 MB. Pass
``--download`` to hash the bytes locally when the API does not report a digest,
or when you want the stronger check that the served bytes match.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = "taqat-techno/rag"
MANIFEST = Path("winget/RAGTools.RAGTools.installer.yaml")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


def read_manifest(path: Path) -> dict[str, str]:
    """Pull the three fields we compare, without requiring a YAML dependency.

    Deliberately regex-based and deliberately strict: a field this script cannot
    find is reported as missing rather than defaulted, because a default here
    would let the check pass on a manifest it never actually read.
    """
    text = path.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for key, pattern in (
        ("version", r"^PackageVersion:\s*(\S+)\s*$"),
        ("url", r"^\s*(?:-\s*)?InstallerUrl:\s*(\S+)\s*$"),
        ("sha256", r"^\s*(?:-\s*)?InstallerSha256:\s*([0-9A-Fa-f]{64})\s*$"),
    ):
        m = re.search(pattern, text, re.MULTILINE)
        if m:
            out[key] = m.group(1)
    return out


def tag_and_asset_from_url(url: str) -> tuple[str | None, str | None]:
    m = re.search(r"/releases/download/([^/]+)/([^/?#]+)$", url)
    return (m.group(1), m.group(2)) if m else (None, None)


def api_digest(tag: str, asset_name: str) -> tuple[str | None, int | None, str]:
    """Ask GitHub for the asset's recorded digest.

    Returns (sha256_hex_or_None, size_or_None, note). An asset GitHub has not
    digested returns None — NOT an empty string and NOT a zero — so the caller
    can tell "no digest available" from "digest did not match". Reporting a
    missing measurement as a value is how a check starts lying.
    """
    proc = subprocess.run(
        ["gh", "api", f"repos/{REPO}/releases/tags/{tag}"],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        return None, None, f"gh api failed: {proc.stderr.strip()[:200]}"
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return None, None, f"unparseable release payload: {exc}"

    for asset in payload.get("assets", []):
        if asset.get("name") == asset_name:
            digest = asset.get("digest") or ""
            size = asset.get("size")
            if digest.startswith("sha256:"):
                return digest.split(":", 1)[1].lower(), size, "from the GitHub API"
            return None, size, "the API records no sha256 digest for this asset"
    return None, None, f"{asset_name} is not attached to {tag}"


def resolve_published_digest(
    url: str, tag: str, asset_name: str, *, download: bool, wait_seconds: float,
) -> tuple[str | None, int | None, str]:
    """The digest of the artifact the manifest points at, waiting if asked.

    ``--wait-for-asset`` exists because of WHEN this check can run. Wired into
    release-validation, it fires on the tag push — the same event that starts
    ``release.yml`` building the installer. So at the moment this job starts,
    the release it is asked to validate does not exist yet: it is being built
    on another runner, and will be for the best part of an hour.

    The alternatives were both worse. Failing immediately makes the check
    useless on the only event it needs to work on. Passing when the asset is
    absent is a check that reports success for something it never looked at,
    which is the failure mode this entire work package is about. So it waits,
    with a stated budget, and fails when the budget runs out — a tag that
    produced no installer inside the budget is a failed release either way.
    """
    deadline = time.time() + max(0.0, wait_seconds)
    while True:
        if download:
            actual, size, note = downloaded_digest(url)
        else:
            actual, size, note = api_digest(tag, asset_name)
            # Fall back to hashing the bytes ONLY when the asset demonstrably
            # exists and merely has no recorded digest (`size` is not None).
            # If the release or the asset is absent, a 626 MB download cannot
            # succeed either — it would just take much longer to say so.
            if actual is None and size is not None:
                print(f"    (API digest unavailable: {note}; falling back to "
                      f"download)", flush=True)
                actual, size, note = downloaded_digest(url)

        if actual is not None or time.time() >= deadline:
            return actual, size, note

        remaining = int(deadline - time.time())
        print(f"    (not published yet: {note}; retrying in 60s, ~{remaining}s "
              f"of budget left)", flush=True)
        time.sleep(min(60.0, max(1.0, deadline - time.time())))


def downloaded_digest(url: str) -> tuple[str | None, int | None, str]:
    h = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(url, timeout=1800) as resp:
            while chunk := resp.read(1024 * 1024):
                h.update(chunk)
                total += len(chunk)
    except Exception as exc:                       # noqa: BLE001 - reported, not swallowed
        return None, None, f"download failed: {exc}"
    return h.hexdigest(), total, "hashed from the served bytes"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, default=MANIFEST)
    ap.add_argument("--expect-version",
                    help="fail unless the manifest declares this version")
    ap.add_argument("--download", action="store_true",
                    help="hash the served bytes instead of trusting the API digest")
    ap.add_argument("--wait-for-asset", type=float, default=0.0, metavar="SECONDS",
                    help="poll for up to SECONDS while the release asset is "
                         "still being built and uploaded (0 = do not wait)")
    args = ap.parse_args()

    print(f"Winget manifest: {args.manifest}", flush=True)
    if not args.manifest.exists():
        check("the manifest exists", False, str(args.manifest))
        return 1

    fields = read_manifest(args.manifest)
    for key in ("version", "url", "sha256"):
        if key not in fields:
            check(f"the manifest declares {key}", False, "field not found")
            return 1

    version, url, declared = fields["version"], fields["url"], fields["sha256"].lower()
    print(f"  version={version}\n  url={url}\n  declared={declared}", flush=True)

    ok = True
    if args.expect_version:
        ok &= check("the manifest version is the release under test",
                    version == args.expect_version,
                    f"manifest {version}, expected {args.expect_version}")

    tag, asset_name = tag_and_asset_from_url(url)
    ok &= check("the installer URL is a release-asset URL", bool(tag and asset_name), url)
    if not (tag and asset_name):
        return 1

    # The URL and the version must agree with each other before either is
    # compared with the artifact. A manifest that points a 3.5.0 version at a
    # 3.4.0 URL is broken even if the hash matches that URL.
    ok &= check("the URL's tag matches the declared version",
                tag.lstrip("v") == version, f"tag {tag}, version {version}")
    ok &= check("the asset name matches the declared version",
                version in asset_name, f"asset {asset_name}, version {version}")

    # Waiting is only ever worth it when everything above agreed. If the
    # manifest already declares the wrong version, or points at a tag that is
    # not the one under test, an hour of polling ends in the same answer — so
    # say it now, in seconds, rather than at the end of the budget.
    wait = args.wait_for_asset
    if wait and not ok:
        print("    (not waiting for the asset: a check above already failed, "
              "and the answer will not change)", flush=True)
        wait = 0.0

    actual, size, note = resolve_published_digest(
        url, tag, asset_name, download=args.download, wait_seconds=wait)

    if actual is None:
        check("the published artifact's digest could be determined", False, note)
        return 1

    ok &= check("the manifest hash equals the published artifact's digest",
                actual == declared,
                f"published {actual} ({note}, {size} bytes), manifest {declared}")

    print("", flush=True)
    passed = sum(1 for _, o, _ in results if o)
    failed = len(results) - passed
    print(f"  {passed} passed, {failed} failed", flush=True)
    print(json.dumps({"passed": passed, "failed": failed, "version": version,
                      "declared": declared, "published": actual}), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
