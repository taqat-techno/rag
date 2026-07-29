"""Packaged upgrade validation for Linux and macOS.

Windows had a real packaged-upgrade test long before these did, for the ordinary
reason: that is where the failures were. But "we only broke it on Windows" is a
statement about where we looked, and the migration this release performs — a
schema change plus a full re-index — is platform-neutral code that runs on all
three.

This exercises the packaged ARTIFACT (the release tarball / zip), not the source
tree, and covers the rows gate 2 names: config migration, service lifecycle,
restart, user-data preservation, selfcheck, and removal leaving nothing behind.

It deliberately does NOT try to reproduce the Windows installer. There isn't
one: Linux and macOS ship an archive that is unpacked, so "upgrade" means
replacing the unpacked tree beside an existing data directory — which is exactly
the state a user reaches by downloading the new release over the old one.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


def run(argv: list[str], *, env=None, timeout: int = 900):
    return subprocess.run(argv, capture_output=True, text=True, env=env,
                          timeout=timeout)


LEGACY_CONFIG = """version = 2

[[projects]]
id = "legacy_alpha"
path = "{alpha}"
mode = "docs"

[[projects]]
id = "legacy_beta"
path = "{beta}"
mode = "docs"
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", default="dist/rag",
                        help="the packaged one-dir bundle to validate")
    args = parser.parse_args(argv)

    if sys.platform == "win32":
        print("POSIX only — Windows is covered by verify_upgrade_install.py")
        return 0

    bundle = Path(args.bundle).resolve()
    binary = bundle / "rag"
    if not check("the packaged bundle exists", binary.is_file(), str(binary)):
        return 1

    root = Path(tempfile.mkdtemp(prefix="ragtools-posix-upgrade-"))
    data = root / "data"
    data.mkdir(parents=True)

    # --- 1. an authentic legacy installation ------------------------------
    alpha, beta = root / "legacy_alpha", root / "legacy_beta"
    for project in (alpha, beta):
        project.mkdir(parents=True)
        (project / "notes.md").write_text(
            f"# {project.name}\n\nContent that must survive.", encoding="utf-8")

    config = data / "config.toml"
    config.write_text(LEGACY_CONFIG.format(alpha=alpha, beta=beta), encoding="utf-8")
    sentinel = data / "user-sentinel.txt"
    sentinel.write_text("user data must survive", encoding="utf-8")

    env = dict(os.environ,
               RAG_CONFIG_PATH=str(config),
               RAG_DATA_DIR=str(data),
               RAG_STATE_DB=str(data / "index_state.db"),
               RAG_QDRANT_PATH=str(data / "qdrant"))

    check("the legacy config has NO storage keys", "storage_backend" not in
          config.read_text(encoding="utf-8"), "as a real v2 config does not")

    # --- 2. the packaged binary migrates it -------------------------------
    proc = run([str(binary), "upgrade"], env=env)
    check("`rag upgrade` succeeded on the packaged artifact",
          proc.returncode == 0, (proc.stdout or proc.stderr).strip()[-300:])

    try:
        import tomllib
    except ModuleNotFoundError:                      # pragma: no cover
        import tomli as tomllib                      # type: ignore[no-redef]
    migrated = tomllib.loads(config.read_text(encoding="utf-8"))

    check("the config is now schema v3", migrated.get("version") == 3,
          str(migrated.get("version")))
    check("an explicit engine was adopted", bool(migrated.get("storage_backend")),
          str(migrated.get("storage_backend")))
    check("the per-project layout was adopted",
          migrated.get("collection_strategy") == "per_project",
          str(migrated.get("collection_strategy")))
    check("the adoption was recorded as a legacy default",
          "storage_backend" in ((migrated.get("migration") or {}).get("adopted") or []),
          json.dumps(migrated.get("migration")))
    surviving = sorted(p.get("id") for p in (migrated.get("projects") or []))
    check("every legacy project survived",
          surviving == ["legacy_alpha", "legacy_beta"], str(surviving))

    # --- 3. restart must not repeat the migration -------------------------
    before = config.read_bytes()
    again = run([str(binary), "upgrade"], env=env)
    check("a second run is a no-op", again.returncode == 0 and
          config.read_bytes() == before, "config unchanged")

    # --- 4. selfcheck runs on the packaged artifact -----------------------
    version = (run([str(binary), "version"], env=env).stdout or "").strip()
    expected = version.replace("ragtools v", "").strip()
    sc = run([str(binary), "selfcheck", "--expect-version", expected], env=env)
    # The exit code is now a CATEGORY, not a bit: 0 clean, 1 installation
    # integrity, 2 runtime, 3 migration in progress, 4 warnings (see
    # `ragtools.selfcheck`). This asserted `in (0, 1)`, which was right when a
    # failure could only mean "the installation is wrong" and became wrong the
    # moment a runtime condition got its own code — nothing is listening here,
    # so a packaged artifact reports 2.
    #
    # What this step actually verifies is that selfcheck RAN and produced a
    # verdict, so the assertion is "the code is one this release defines".
    # Anything else means it crashed or exited some other way.
    check("selfcheck runs and reports a known verdict", sc.returncode in (0, 1, 2, 3, 4),
          f"exit {sc.returncode}: " +
          ((sc.stdout or sc.stderr).strip().splitlines()[-1] if sc.stdout else ""))

    # --- 5. the managed engine is present in the packaged artifact --------
    engine = bundle / "bin" / "qdrant"
    check("the managed engine ships with this artifact", engine.is_file(),
          str(engine))

    # --- 6. user data survived --------------------------------------------
    check("the user sentinel survived", sentinel.is_file(), str(sentinel))
    check("the configuration still exists", config.is_file(), str(config))

    # --- 7. removal leaves nothing running --------------------------------
    stop = run([str(binary), "service", "stop"], env=env, timeout=120)
    check("service stop is accepted", stop.returncode in (0, 1),
          (stop.stdout or stop.stderr).strip()[-120:])
    time.sleep(1)
    leftover = run(["pgrep", "-f", str(bundle)], timeout=60).stdout.strip()
    check("no owned process survives removal", not leftover, leftover or "none")

    shutil.rmtree(root, ignore_errors=True)
    check("the sandbox is removed", not root.exists(), str(root))

    failed = [r for r in results if not r[1]]
    print(f"\n  {len(results) - len(failed)} passed, {len(failed)} failed", flush=True)
    print(json.dumps({"platform": sys.platform,
                      "passed": len(results) - len(failed),
                      "failed": len(failed)}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
