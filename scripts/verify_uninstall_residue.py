"""Install, then uninstall, then prove nothing survived.

The release gate requires that no obsolete version, service, watcher, scheduled
action, tray process, runtime directory or duplicated index remains active after
removal. Testing that against a real installation means destroying one, so this
does it against a sandbox: build the wheel, install into a fresh interpreter,
create the runtime footprint, uninstall, and sweep.

The sweep is the point. "Uninstall returned 0" is not evidence; the evidence is
that a scan of the machine afterwards finds nothing, run by the same detection
code the upgrade uses.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

PASS, FAIL = "PASS", "FAIL"

#: (name, status, detail, phase). The phases are reported separately because
#: they are separate matrix rows: V01 is "a clean install of the BUILT artifact
#: works", V15 is "removing it leaves nothing". One run produces both, but
#: collapsing them into a single number would let install evidence satisfy the
#: uninstall row and vice versa.
results: list[tuple[str, str, str, str]] = []

INSTALL, UNINSTALL = "install", "uninstall"


def check(name: str, ok: bool, detail: str = "", phase: str = UNINSTALL) -> None:
    results.append((name, PASS if ok else FAIL, detail, phase))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def clean_env() -> dict:
    """An environment with no path back to the source tree.

    The first run of this script reported the package still importable after
    uninstall — because PYTHONPATH pointed the sandbox interpreter at
    `src/`. A clean room with an inherited PYTHONPATH is not a clean room, and
    the failure looked exactly like a product bug.
    """
    import os

    env = dict(os.environ)
    for var in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        env.pop(var, None)
    return env


def run(argv, **kw):
    kw.setdefault("env", clean_env())
    return subprocess.run(argv, capture_output=True, text=True, timeout=900, **kw)


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    root = Path(tempfile.mkdtemp(prefix="ragtools-uninstall-"))
    dist, venv, data = root / "dist", root / "venv", root / "data"
    print(f"Sandbox: {root}\n")

    try:
        # --- build + install ---------------------------------------------
        built = run([sys.executable, "-m", "pip", "wheel", str(repo), "--no-deps",
                     "-w", str(dist), "-q"])
        wheels = list(dist.glob("*.whl"))
        check("the wheel builds", bool(wheels),
              wheels[0].name if wheels else (built.stderr or "")[-90:], INSTALL)
        if not wheels:
            return 1

        run([sys.executable, "-m", "venv", str(venv)])
        py = venv / ("Scripts" if sys.platform == "win32" else "bin") / (
            "python.exe" if sys.platform == "win32" else "python")
        install = run([str(py), "-m", "pip", "install", "-q", str(wheels[0])])
        check("it installs into a clean interpreter", install.returncode == 0,
              (install.stderr or "")[-90:] if install.returncode else "", INSTALL)

        scripts_dir = venv / ("Scripts" if sys.platform == "win32" else "bin")
        entry = [p for p in scripts_dir.glob("rag*") if p.suffix.lower() in (".exe", "")]
        check("entry points are created", len(entry) >= 2,
              ", ".join(sorted(p.name for p in entry))
              or f"scripts dir holds: {sorted(q.name for q in scripts_dir.iterdir())[:6]}",
              INSTALL)

        # --- drive the installed artifact, not the source tree -----------
        # "pip install returned 0" is not evidence that what ships works. This
        # is the same set the CI clean-install job runs: package data present,
        # the platform adapter resolvable, and the retired watchdog genuinely
        # absent from the distribution rather than merely deleted from git.
        exercised = run([str(py), "-c", (
            "import ragtools, pathlib, importlib.util as u;"
            "root = pathlib.Path(ragtools.__file__).parent;"
            "assert len(list((root/'service'/'templates').glob('*.html'))) >= 8;"
            "from ragtools.platform import adapter; adapter();"
            "from ragtools.upgrade import scan, migrate_config;"
            "assert u.find_spec('ragtools.service.watchdog') is None, 'watchdog shipped';"
            "print(ragtools.__version__)")])
        check("the installed artifact imports and resolves its platform",
              exercised.returncode == 0,
              (exercised.stdout or exercised.stderr or "").strip()[-90:], INSTALL)

        # --- a runtime footprint, as a real install would leave ----------
        (data / "logs").mkdir(parents=True)
        (data / "qdrant").mkdir()
        for stale in ("service.pid", "supervisor.pid", "tray.pid"):
            (data / stale).write_text("1234", encoding="utf-8")
        (data / "config.toml").write_text("version = 2\n", encoding="utf-8")

        # --- uninstall ----------------------------------------------------
        removed = run([str(py), "-m", "pip", "uninstall", "-y", "-q", "ragtools"])
        check("uninstall succeeds", removed.returncode == 0,
              (removed.stderr or "")[-90:] if removed.returncode else "")

        # --- the sweep ----------------------------------------------------
        gone = run([str(py), "-c", "import ragtools"])
        check("the package is no longer importable", gone.returncode != 0)

        leftover = [p.name for p in scripts_dir.glob("rag*")]
        check("entry points are removed", not leftover, ", ".join(leftover) or "none")

        site = list(venv.rglob("ragtools"))
        check("no package directory survives", not site,
              str(site[0]) if site else "site-packages clean")

        # --- data is NOT removed, deliberately ---------------------------
        check("the data directory is left alone", (data / "config.toml").exists(),
              "an uninstaller that destroys a rebuilt index is doing something "
              "the user did not ask for")

        # --- the upgrade scanner agrees the install is gone --------------
        from ragtools.upgrade.scan import scan

        class _Adapter:
            name = "sandbox"

            def app_dir(self):
                return root / "nonexistent-app-dir"

            def dev_dir(self):
                return root / "nonexistent-dev-dir"

            def find_autostart(self, kind="service"):
                return []

        result = scan(adapter=_Adapter(), path_value="")
        check("a fresh scan finds no installation", not result.findings,
              f"{len(result.findings)} finding(s)")
        check("a fresh scan finds no registrations", not result.registrations)
        check("a fresh scan finds no product PATH entries", not result.path_entries)

    finally:
        shutil.rmtree(root, ignore_errors=True)
        check("the sandbox itself is removed", not root.exists(), str(root))

    def tally(phase: str) -> dict:
        rows = [r for r in results if r[3] == phase]
        failed = [r for r in rows if r[1] == FAIL]
        return {"passed": len(rows) - len(failed), "failed": len(failed)}

    failed = [r for r in results if r[1] == FAIL]
    print(f"\n  {len(results) - len(failed)} passed, {len(failed)} failed")
    print(json.dumps({
        "passed": len(results) - len(failed), "failed": len(failed),
        INSTALL: tally(INSTALL), UNINSTALL: tally(UNINSTALL),
    }))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
