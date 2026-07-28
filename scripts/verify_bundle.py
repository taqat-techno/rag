"""Assert properties of the BUILT bundle that no source test can reach.

Two of the four defects in the 3.0.0 incident were properties of the artifact,
not of the source tree:

* `rag.exe` is a console-subsystem image, so Windows gave it a console every
  time Task Scheduler started it — two terminal windows on the desktop at every
  login. The fix ships a GUI-subsystem sibling, and whether that sibling is
  *actually* GUI-subsystem is decided by PyInstaller at build time. A `console=`
  keyword silently dropped in a spec refactor produces a bundle that passes
  every unit test and reintroduces the defect.
* the installer overlaid a new `_internal` on the old one, so package manifests
  accumulated until `importlib.metadata` reported a version the bundle had not
  shipped in months. The invariant that protects against it — one `.dist-info`
  per distribution — is a property of a directory that only exists after a build.

v3.0.0 shipped four artifacts that passed every name and size check and were
broken. Names and sizes were never the question.

Usage:
    python scripts/verify_bundle.py --bundle dist/rag
    python scripts/verify_bundle.py --bundle dist/rag --require-windowed
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from collections import defaultdict
from pathlib import Path

#: winnt.h. 2 = does not get a console; 3 = does.
IMAGE_SUBSYSTEM_WINDOWS_GUI = 2
IMAGE_SUBSYSTEM_WINDOWS_CUI = 3
SUBSYSTEM_NAMES = {IMAGE_SUBSYSTEM_WINDOWS_GUI: "WINDOWS_GUI",
                   IMAGE_SUBSYSTEM_WINDOWS_CUI: "WINDOWS_CUI"}

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def pe_subsystem(path: Path) -> int:
    """Read the PE Optional Header's Subsystem field.

    Walked by hand rather than with a dependency: this runs in CI on a machine
    that has just built the thing it is checking, and adding an import to verify
    a build is how a verifier stops running.
    """
    # Header only. These images are 70 MB+ and the answer is two bytes near the
    # front; `read_bytes()` would pull the whole bundle through memory in CI.
    with path.open("rb") as handle:
        head = handle.read(0x40)
        if head[:2] != b"MZ":
            raise ValueError(f"{path.name} is not a PE image")
        pe_offset = struct.unpack_from("<I", head, 0x3C)[0]
        handle.seek(pe_offset)
        # PE signature (4) + COFF file header (20) -> optional header.
        # Subsystem sits at offset 68 in both PE32 and PE32+.
        block = handle.read(24 + 70)
    if len(block) < 24 + 70 or block[:4] != b"PE\0\0":
        raise ValueError(f"{path.name} has no PE signature")
    return struct.unpack_from("<H", block, 24 + 68)[0]


def check_windowed_executable(bundle: Path) -> None:
    """`ragw.exe` must exist and must genuinely be GUI-subsystem."""
    windowed = bundle / "ragw.exe"
    console = bundle / "rag.exe"

    if not check("ragw.exe is in the bundle", windowed.is_file(),
                 "the login path falls back to rag.exe without it"):
        return

    try:
        subsystem = pe_subsystem(windowed)
    except ValueError as exc:
        check("ragw.exe is a readable PE image", False, str(exc))
        return

    check("ragw.exe is GUI-subsystem", subsystem == IMAGE_SUBSYSTEM_WINDOWS_GUI,
          f"subsystem {subsystem} ({SUBSYSTEM_NAMES.get(subsystem, 'unknown')}), "
          f"want {IMAGE_SUBSYSTEM_WINDOWS_GUI} (WINDOWS_GUI)")

    # The console binary must stay console: `rag search` piped into a file is
    # the normal case, and a GUI-subsystem CLI writes nowhere.
    if console.is_file():
        try:
            cli_subsystem = pe_subsystem(console)
        except ValueError:
            return
        check("rag.exe is still console-subsystem",
              cli_subsystem == IMAGE_SUBSYSTEM_WINDOWS_CUI,
              f"subsystem {cli_subsystem} — a windowed CLI has no stdout")


def distribution_versions(internal: Path) -> dict[str, set[str]]:
    """Map distribution name -> versions present, from `.dist-info` names.

    Normalised the way `importlib.metadata` normalises, because that is what
    decides which of two manifests wins when both are present.
    """
    found: dict[str, set[str]] = defaultdict(set)
    for entry in internal.glob("*.dist-info"):
        match = re.match(r"^(?P<name>.+?)-(?P<version>[^-]+)\.dist-info$", entry.name)
        if not match:
            continue
        name = re.sub(r"[-_.]+", "-", match.group("name")).lower()
        found[name].add(match.group("version"))
    return found


def check_no_layered_manifests(bundle: Path) -> None:
    """One version per distribution — the invariant the crash loop violated."""
    internal = bundle / "_internal"
    if not check("the bundle has an _internal payload", internal.is_dir(),
                 f"looked in {internal}"):
        return

    versions = distribution_versions(internal)
    check("_internal carries package manifests", bool(versions),
          f"{len(versions)} distributions")

    duplicated = {n: sorted(v) for n, v in versions.items() if len(v) > 1}
    check("no distribution ships two versions", not duplicated,
          "; ".join(f"{n}: {', '.join(v)}" for n, v in sorted(duplicated.items()))
          or "none")

    # The specific pair that took the service down, named so a regression is
    # recognisable rather than merely red.
    safetensors = sorted(versions.get("safetensors", []))
    if len(safetensors) > 1:
        check("safetensors is not layered", False,
              f"{safetensors} — this is the 3.0.0 crash-loop signature")


def check_managed_engine(bundle: Path) -> None:
    """The managed engine must be IN the bundle, not merely supported by it.

    `find_qdrant_binary` looks for one "alongside the packaged application (the
    installer ships it here)" — a comment that was true of the intent and false
    of every release. Nothing shipped it, so `managed` fell back to embedded on
    every machine, correctly reporting a reason nobody read.

    That failure is invisible from inside the product: falling back IS the
    designed behaviour. The only place it can be caught is here, against a real
    bundle, before it becomes an installer.
    """
    engine = bundle / "bin" / ("qdrant.exe" if (bundle / "rag.exe").is_file()
                               else "qdrant")
    if not engine.is_file():
        check("the managed engine is packaged", False,
              f"{engine} is missing — `managed` would silently fall back to "
              "embedded on every installation")
        return

    size_mb = engine.stat().st_size / 1048576
    check("the managed engine is packaged", True, f"{engine.name} ({size_mb:.1f} MB)")
    # A truncated or placeholder file passes an existence check and fails at the
    # one moment it matters, so assert it is plausibly an executable.
    check("the packaged engine is a real binary", size_mb > 5,
          f"{size_mb:.1f} MB")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", default="dist/rag",
                        help="the PyInstaller one-dir output")
    parser.add_argument("--require-windowed", action="store_true",
                        help="fail if ragw.exe is absent (Windows builds)")
    parser.add_argument("--require-engine", action="store_true",
                        help="fail if the managed Qdrant engine is not bundled")
    args = parser.parse_args(argv)

    bundle = Path(args.bundle)
    print(f"Bundle {bundle.resolve()}\n")

    if not bundle.is_dir():
        check("the bundle exists", False, str(bundle))
        return 1
    check("the bundle exists", True, str(bundle))

    check_no_layered_manifests(bundle)

    windows_build = (bundle / "rag.exe").is_file() or args.require_windowed
    if windows_build:
        check_windowed_executable(bundle)
    else:
        print("  [skip] windowed-executable checks — not a Windows bundle")

    if args.require_engine:
        check_managed_engine(bundle)
    else:
        print("  [skip] managed-engine check — not requested (--require-engine)")

    failed = [r for r in results if r[1] == FAIL]
    print(f"\n  {len(results) - len(failed)} passed, {len(failed)} failed")
    print(json.dumps({"passed": len(results) - len(failed), "failed": len(failed)}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
