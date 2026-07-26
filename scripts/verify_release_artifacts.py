"""Validate a published release's assets against the platform contract.

`/release-ship` Phase D describes this check in prose and nothing implements it,
which is how two artifact-naming defects reached a published release in one
afternoon:

* the macOS bundle shipped as ``macos-arm64.tar.gz`` while the gate required
  ``macOS-arm64.zip`` — a release whose bundle built perfectly would have been
  failed for a missing artifact;
* the Windows installer was uploaded under a name derived from the tag while
  Inno emitted one derived from a hardcoded version, so v3.0.0-rc.2 published
  with **no installer at all** and every step reported success.

Both were invisible because the upload step defaulted to
``fail_on_unmatched_files: false``. That is now true in the workflow, and this
script is the second line: it checks what is actually ON the release, not what
the build believed it uploaded.

Usage:
    python scripts/verify_release_artifacts.py --tag v3.0.0
    python scripts/verify_release_artifacts.py --tag v3.0.0-rc.4 --allow-prerelease
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

#: name pattern -> minimum plausible size in MB.
#: The floor matters as much as the name: a 2 KB file called
#: `RAGTools-Setup-3.0.0.exe` satisfies a regex and installs nothing. These
#: bundles carry a sentence-transformer model, so anything small is a broken
#: build that uploaded successfully.
EXPECTED = {
    "windows-installer": (r"^RAGTools-Setup-{v}\.exe$", 50),
    "windows-portable": (r"^RAGTools-{v}-portable\.zip$", 50),
    "macos-bundle": (r"^RAGTools-{v}-macOS-arm64\.(zip|dmg)$", 10),
    "linux-bundle": (r"^RAGTools-{v}-linux-(x86_64|aarch64)\.(AppImage|tar\.gz|deb|rpm)$", 10),
}

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def gh_json(args: list[str]):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:200])
    return json.loads(proc.stdout)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repo", default="taqat-techno/rag")
    parser.add_argument("--allow-prerelease", action="store_true",
                        help="an rc is expected to be flagged prerelease")
    args = parser.parse_args(argv)

    version = args.tag.lstrip("v")
    print(f"Release {args.tag} on {args.repo}\n")

    try:
        release = gh_json(["gh", "release", "view", args.tag, "--repo", args.repo,
                           "--json", "assets,isPrerelease,isDraft,tagName"])
    except Exception as exc:  # noqa: BLE001
        check("the release exists", False, str(exc))
        return 1

    check("the release exists", True, release.get("tagName", ""))
    check("it is not a draft", not release.get("isDraft"),
          "a draft is invisible to everyone but its author")

    prerelease = bool(release.get("isPrerelease"))
    if args.allow_prerelease:
        check("flagged as prerelease", prerelease,
              "a tag with a hyphen must not be presented as stable")
    else:
        check("NOT flagged as prerelease", not prerelease,
              "a final release marked prerelease is invisible to `latest`")

    assets = {a["name"]: a["size"] for a in release.get("assets") or []}
    print(f"\n  {len(assets)} asset(s) published:")
    for name, size in sorted(assets.items()):
        print(f"    {name}  ({size / 1048576:.0f} MB)")
    print()

    # --- the contract ----------------------------------------------------
    for label, (pattern, min_mb) in EXPECTED.items():
        rx = re.compile(pattern.format(v=re.escape(version)))
        matched = [(n, s) for n, s in assets.items() if rx.match(n)]
        if not matched:
            check(f"{label}: present", False,
                  f"nothing matches {rx.pattern}")
            continue
        name, size = matched[0]
        check(f"{label}: present", True, name)
        check(f"{label}: plausible size", size >= min_mb * 1048576,
              f"{size / 1048576:.0f} MB (floor {min_mb} MB)")

    # --- nothing unexpected ----------------------------------------------
    known = [re.compile(p.format(v=re.escape(version))) for p, _ in EXPECTED.values()]
    strays = [n for n in assets if not any(rx.match(n) for rx in known)]
    check("no unrecognised assets", not strays, ", ".join(strays) or "none")

    failed = [r for r in results if r[1] == FAIL]
    print(f"\n  {len(results) - len(failed)} passed, {len(failed)} failed")
    print(json.dumps({"passed": len(results) - len(failed), "failed": len(failed),
                      "assets": len(assets)}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
