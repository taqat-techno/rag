"""Run the uninstaller the user actually runs, then prove nothing survived.

Every uninstall check that existed before this one tested something else.
`verify_uninstall_residue.py` builds a wheel, installs it into a throwaway venv
and `pip uninstall`s it — which exercises pip, not `unins000.exe`.
`rehearse_upgrade.py` goes further and writes a FAKE uninstaller
(``unins000.exe`` containing the two bytes ``MZ``) so the layout looks right.

So the Inno uninstaller — the [UninstallRun] steps that stop the service, the
[UninstallDelete] entries, the data-wipe prompt, the config backup that guards
it — had never been executed anywhere. The release gate said uninstall was
validated; what was validated was a different program.

This runs the real one, silently, against a real installation, and then sweeps
with the same detection code an upgrade uses.

Usage (after an installer has been run):

    python scripts/verify_real_uninstall.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
DATA_DIR = LOCALAPPDATA / "RAGTools"
UNINSTALL_KEY = (r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
                 r"\{7E4B2A3C-F1D8-4A5E-B9C0-1234567890AB}_is1")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


def registry_value(name: str) -> str | None:
    import winreg

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, UNINSTALL_KEY) as key:
                return winreg.QueryValueEx(key, name)[0]
        except OSError:
            continue
    return None


def main() -> int:
    if sys.platform != "win32":
        print("Windows only.")
        return 0

    uninstaller = registry_value("UninstallString")
    install_dir = registry_value("InstallLocation")
    if not check("an installation is registered to uninstall",
                 bool(uninstaller), str(uninstaller)):
        return 1

    # The registry value is quoted and may carry its own switches.
    exe = str(uninstaller).strip().strip('"')
    if not Path(exe).is_file():
        return 1 if not check("the uninstaller exists on disk", False, exe) else 0
    check("the uninstaller exists on disk", True, exe)

    # Data the user did not ask to lose. The silent uninstall must not take it:
    # the prompt that offers to is defaulted to No and suppressed switches
    # answer with the default, so "keep" is the correct silent behaviour.
    config = DATA_DIR / "config.toml"
    config_before = config.read_bytes() if config.is_file() else None

    print(f"\n>>> running the real uninstaller: {exe}", flush=True)
    proc = subprocess.run([exe, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                          capture_output=True, text=True, timeout=900)
    print(f"    exit={proc.returncode}", flush=True)
    # Inno's uninstaller spawns a copy of itself and returns immediately; the
    # work happens in the child. Poll for the registry entry to disappear
    # rather than assuming the exit code means "finished".
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and registry_value("DisplayVersion"):
        time.sleep(2)

    check("the uninstall entry is gone", registry_value("DisplayVersion") is None,
          str(registry_value("DisplayVersion")))

    if install_dir:
        leftovers = []
        root = Path(install_dir)
        if root.is_dir():
            leftovers = [p.name for p in root.iterdir()]
        check("no program files survive", not leftovers,
              f"{len(leftovers)} entries: {leftovers[:8]}")

    # The sweep, run by the same detection code the upgrade uses.
    #
    # Filter on the REAL layout constants, imported rather than typed. The first
    # version of this check asked for `by_layout("install")` — a layout that
    # does not exist, so it returned an empty list and passed on every machine
    # including one where nothing had been uninstalled at all.
    from ragtools.upgrade.scan import (
        L_INSTALL_MACHINE,
        L_INSTALL_PIP,
        L_INSTALL_USER,
        L_LEGACY_ARTIFACT,
        L_DATA,
    )
    from ragtools.upgrade import scan

    result = scan()
    # `install-pip` is the CI checkout's own editable install and `data` is
    # preserved by policy; neither is residue of the packaged product.
    assert L_INSTALL_PIP and L_DATA  # named so the exclusions are explicit
    residue = [f for f in result.findings
               if f.layout in (L_INSTALL_USER, L_INSTALL_MACHINE, L_LEGACY_ARTIFACT)]
    check("a fresh scan finds no packaged installation", not residue,
          "; ".join(f"{f.layout}: {f.path}" for f in residue) or "clean")
    check("a fresh scan finds no registrations", not result.registrations,
          f"{len(result.registrations)} registration(s)")
    check("no product PATH entries survive", not result.path_entries,
          f"{len(result.path_entries)} entry/entries")

    # Nothing owned may still be running.
    from ragtools.platform import adapter

    running = []
    try:
        processes = adapter().owned_processes()
        running = [f"{image} pid={pid}" for pid, image, _p in (processes or [])]
    except Exception as exc:  # noqa: BLE001
        print(f"    (could not enumerate processes: {exc})", flush=True)
    check("no owned process is still running", not running, "; ".join(running))

    # Policy: an uninstaller that destroys a rebuilt index is doing something
    # the user did not ask for, so a SILENT uninstall keeps user data.
    if config_before is not None:
        check("the user's configuration survives a silent uninstall",
              config.is_file() and config.read_bytes() == config_before,
              str(config))

    failed = [r for r in results if not r[1]]
    print(f"\n  {len(results) - len(failed)} passed, {len(failed)} failed", flush=True)
    print(json.dumps({"passed": len(results) - len(failed), "failed": len(failed)}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
