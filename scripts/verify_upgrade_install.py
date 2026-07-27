"""Install a real packaged v2.7.0, then upgrade it with the newly built installer.

Every upgrade check this project had before ran against a *synthesised* layout:
a config file written by the test, directories created by the test, and
`migrate_config()` called directly. All of it passed while the one thing that
actually breaks on a user's machine went untested — running one packaged
installer over another, with the previous version's processes alive and holding
their own binaries.

That gap is not theoretical. `ForceKillRagProcesses()` killed only `rag.exe`
until v3.0.2, and v3.0.1 had already moved both scheduled tasks to `ragw.exe` —
so from 3.0.1 onward the images an upgrade most needed to stop were the ones it
did not touch. No test could see it, because no test ever had a real previous
installation running.

So this does the real thing, in order:

  1. download and silently install the published previous release
  2. start it and prove it is running, from its own directory
  3. run the NEW installer over it, with those processes still alive
  4. assert the machine now belongs entirely to the new release
  5. assert user-owned data survived

Usage:
    python scripts/verify_upgrade_install.py --installer dist/RAGTools-Setup-3.0.2.exe \
        --version 3.0.2 --from-version 2.7.0
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = "taqat-techno/rag"
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
INSTALL_DIR = LOCALAPPDATA / "Programs" / "RAGTools"
DATA_DIR = LOCALAPPDATA / "RAGTools"
UNINSTALL_KEY = (r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
                 r"\{7E4B2A3C-F1D8-4A5E-B9C0-1234567890AB}_is1")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


def run(argv: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _registry_value(name: str) -> str | None:
    import winreg

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, UNINSTALL_KEY) as key:
                return winreg.QueryValueEx(key, name)[0]
        except OSError:
            continue
    return None


def registry_version() -> str | None:
    return _registry_value("DisplayVersion")


def installed_dir() -> Path:
    """Where the product actually went, per its own uninstall entry.

    Asked rather than assumed: `{autopf}` resolves differently depending on
    whether Inno chose administrative install mode, and a CI runner is an
    administrator account. Hardcoding the path would make this test pass or fail
    for a reason that has nothing to do with the upgrade.
    """
    recorded = _registry_value("InstallLocation")
    if recorded:
        return Path(recorded.rstrip("\\/"))
    return INSTALL_DIR


def owned_processes() -> list[tuple[int, str, str]]:
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -in 'rag.exe','ragw.exe' } | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.Name)|$($_.ExecutablePath)\" }"
    )
    out = run(["powershell", "-NoProfile", "-Command", script], timeout=120).stdout
    found = []
    for line in (out or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[0].isdigit():
            found.append((int(parts[0]), parts[1], parts[2]))
    return found


def task_targets() -> dict[str, str]:
    """Scheduled-task -> the command it will run at logon."""
    out = run(["powershell", "-NoProfile", "-Command",
               "Get-ScheduledTask -TaskPath '\\RAGTools\\*' -ErrorAction SilentlyContinue | "
               "ForEach-Object { \"$($_.TaskName)|$($_.Actions.Execute)\" }"],
              timeout=120).stdout
    targets = {}
    for line in (out or "").splitlines():
        if "|" in line:
            name, _, execute = line.strip().partition("|")
            targets[name] = execute
    return targets


def binary_version(exe: Path) -> str:
    if not exe.is_file():
        return "<missing>"
    proc = run([str(exe), "version"], timeout=300)
    return (proc.stdout or proc.stderr or "").strip().splitlines()[0] if proc.stdout or proc.stderr else ""


def health(port: int = 21420, tries: int = 45) -> dict | None:
    for _ in range(tries):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=4) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            time.sleep(4)
    return None


def install_silently(installer: Path, label: str) -> int:
    """Inno silent install. `startnow` is omitted so nothing opens a browser;
    the service is started explicitly instead, which is the state that matters."""
    print(f"\n>>> installing {label}: {installer.name}", flush=True)
    proc = run([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                "/TASKS=addtopath,startup"], timeout=2400)
    print(f"    exit={proc.returncode}", flush=True)
    return proc.returncode


def download_previous(version: str, dest: Path) -> Path:
    target = dest / f"RAGTools-Setup-{version}.exe"
    if target.is_file():
        return target
    print(f">>> downloading the published v{version} installer", flush=True)
    proc = run(["gh", "release", "download", f"v{version}", "--repo", REPO,
                "--pattern", target.name, "--dir", str(dest), "--clobber"], timeout=2400)
    if proc.returncode != 0:
        raise RuntimeError(f"download failed: {(proc.stderr or proc.stdout)[:300]}")
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", required=True, help="the newly built installer")
    parser.add_argument("--version", required=True, help="version it should install")
    parser.add_argument("--from-version", default="2.7.0", help="published release to upgrade FROM")
    parser.add_argument("--work", default="upgrade-work", help="scratch directory")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("Windows only.")
        return 0

    new_installer = Path(args.installer).resolve()
    if not new_installer.is_file():
        check("the new installer exists", False, str(new_installer))
        return 1

    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)

    # --- 1. a genuine previous installation ------------------------------
    previous = download_previous(args.from_version, work)
    install_silently(previous, f"v{args.from_version}")

    print("\n--- the machine before the upgrade ---", flush=True)
    install = installed_dir()
    print(f"    install directory (per registry): {install}", flush=True)
    before_version = binary_version(install / "rag.exe")
    check(f"previous release installed ({args.from_version})",
          args.from_version in before_version, before_version)
    check("previous uninstall registry entry",
          registry_version() == args.from_version, str(registry_version()))

    # Start it so its binaries are LOCKED during the upgrade — the condition
    # under which a real upgrade actually has to work.
    run([str(install / "rag.exe"), "service", "start"], timeout=600)
    before_health = health()
    check("previous release is serving", bool(before_health),
          json.dumps(before_health) if before_health else "no /health")
    running_before = owned_processes()
    check("previous release has live processes holding its binaries",
          bool(running_before), f"{len(running_before)} process(es)")

    # A user-owned file that must survive the upgrade untouched.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sentinel = DATA_DIR / "upgrade-sentinel.txt"
    sentinel.write_text("user data must survive", encoding="utf-8")
    config = DATA_DIR / "config.toml"
    config_before = config.read_bytes() if config.is_file() else None

    # --- 2. the upgrade, over a live installation ------------------------
    rc = install_silently(new_installer, f"v{args.version} (over v{args.from_version})")
    check("the upgrade installer exited 0", rc == 0, f"exit {rc}")

    # --- 3. the machine must now belong to the new release ---------------
    print("\n--- the machine after the upgrade ---", flush=True)
    install = installed_dir()          # may legitimately change on upgrade
    after_version = binary_version(install / "rag.exe")
    check(f"rag.exe reports {args.version}", args.version in after_version, after_version)
    check("ragw.exe was installed", (install / "ragw.exe").is_file(),
          str(install / "ragw.exe"))
    check("uninstall registry entry updated",
          registry_version() == args.version,
          f"registry reads {registry_version()}")

    strays = [f"{n} pid={p} at {e}" for p, n, e in owned_processes()
              if e and install.resolve() not in Path(e).resolve().parents]
    check("no owned process runs from outside the install directory",
          not strays, "; ".join(strays) or f"all inside {install}")

    targets = task_targets()
    wrong = {n: t for n, t in targets.items()
             if t and str(install).lower() not in t.lower()}
    check("scheduled tasks point into the install directory",
          not wrong and bool(targets), json.dumps(wrong) if wrong else f"{targets}")

    # --- 4. the product's own verdict ------------------------------------
    selfcheck = run([str(install / "rag.exe"), "selfcheck",
                     "--expect-version", args.version], timeout=600)
    check("rag selfcheck passes", selfcheck.returncode == 0,
          (selfcheck.stdout or selfcheck.stderr or "").strip()[-400:])

    # --- 5. the service the user talks to --------------------------------
    run([str(install / "rag.exe"), "service", "start"], timeout=600)
    after_health = health()
    check("/health reports the new version",
          bool(after_health) and after_health.get("version") == args.version,
          json.dumps(after_health) if after_health else "no /health")

    # --- 6. user data preserved ------------------------------------------
    check("user sentinel file survived", sentinel.is_file(), str(sentinel))
    if config_before is not None:
        check("config.toml was not modified",
              config.is_file() and config.read_bytes() == config_before,
              "unchanged" if config.is_file() else "MISSING")
    else:
        check("config.toml", True, "none existed before", )

    failed = [r for r in results if not r[1]]
    print(f"\n  {len(results) - len(failed)} passed, {len(failed)} failed", flush=True)
    print(json.dumps({"passed": len(results) - len(failed), "failed": len(failed)}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
