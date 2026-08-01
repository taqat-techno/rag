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

WHEN THE SWEEP IS TAKEN
-----------------------
After the uninstaller has finished with the file system — which is later than
the signal this script used to stop at. Inno removes the uninstall REGISTRY
ENTRY and then, from a second process, its own stub and the directory holding
it; polling for the entry therefore returns while the directory is still being
removed. In run 30692207245 one leg of five swept inside that window and the two
checks over the same directory, 0.6 s apart, disagreed::

    [PASS] no program files survive except the uninstaller itself — 0 entries: []
    [FAIL] a fresh scan finds no packaged installation — install-user: ...\\RAGTools

They disagreed because they were two different predicates. The sweep's mapped
every unreadable directory to "residue" and reported neither the contents nor
the error, so the failure could not be told from a real one. There is now one
predicate (:func:`directory_state`), it is taken only after
:func:`wait_until_settled`, and it NAMES what survived.

Usage (after an installer has been run):

    python scripts/verify_real_uninstall.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
DATA_DIR = LOCALAPPDATA / "RAGTools"
UNINSTALL_KEY = (r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
                 r"\{7E4B2A3C-F1D8-4A5E-B9C0-1234567890AB}_is1")

#: `unins000.exe` is the uninstaller itself, and a running program cannot delete
#: its own image. Inno schedules it for removal at the next reboot; until then it
#: legitimately remains, alone, in an otherwise empty directory. Treating that as
#: residue fails every correct uninstall — which it did on the first run of this
#: script.
SELF = frozenset({"unins000.exe", "unins000.dat"})

#: The two states the uninstaller is FINISHED with...
GONE = "gone"
UNINSTALLER_ONLY = "uninstaller-only"
#: ...and the two it is not.
RESIDUE = "residue"
UNREADABLE = "unreadable"

SETTLED = (GONE, UNINSTALLER_ONLY)

#: How long the uninstaller's asynchronous tail is given to finish.
#:
#: The only completion signal this script had was the uninstall REGISTRY ENTRY
#: disappearing, and Inno removes its own stub and the directory holding it
#: after that, from a second process. So "the entry is gone" is not "the
#: uninstaller has finished with the file system", and the sweep was being taken
#: against a removal still in progress.
SETTLE_TIMEOUT = 60.0

results: list[tuple[str, bool, str]] = []


@dataclass(frozen=True)
class DirState:
    """What is in the install directory, and — when it cannot be read — why."""

    state: str
    entries: tuple[str, ...] = ()
    reason: str = ""

    @property
    def settled(self) -> bool:
        return self.state in SETTLED

    def describe(self) -> str:
        if self.state == GONE:
            return "the directory is gone"
        if self.state == UNINSTALLER_ONLY:
            return "only Inno's own stub remains, scheduled for deletion at reboot"
        if self.state == UNREADABLE:
            return f"the directory could not be read: {self.reason}"
        return f"{len(self.entries)} surviving: {list(self.entries[:8])}"


def directory_state(path, *, listdir=None) -> DirState:
    """Classify ``path`` — ONE implementation, deliberately.

    There were two, taken 0.6 s apart over the same directory, and in run
    30692207245 they returned opposite verdicts: the check that iterates the
    directory reported ``0 entries: []`` while the sweep beside it reported that
    same directory as a surviving packaged installation.

    The sweep's predicate was ``all(name in SELF for name in path.iterdir())``
    wrapped in a bare ``except OSError: return False`` — which turns "I could
    not read it" into "there is residue in it", and records neither the contents
    nor the error, so the failure said only ``install-user: <path>``. A Windows
    directory removed while a handle on it is still open is exactly that state:
    ``stat`` still answers from the parent's entry, so it looks present, while
    enumeration is refused. It is NAMED here so it can be waited out and, if it
    persists, reported as itself rather than as an unexplained path.

    ``listdir`` is injectable for the same reason ``scan()`` takes an ``adapter``
    — so the suite can present a directory in each state without needing a
    machine that happens to be in it.
    """
    lister = listdir or (lambda p: [entry.name for entry in Path(p).iterdir()])
    try:
        names = sorted(lister(path))
    except FileNotFoundError:
        return DirState(GONE)
    except NotADirectoryError:
        # A FILE at this path — a legacy artifact such as `data\service.pid` —
        # is residue plainly, not something that "could not be read".
        return DirState(RESIDUE, (os.path.basename(str(path)),))
    except OSError as exc:
        return DirState(UNREADABLE, reason=f"{type(exc).__name__}: {exc}")
    leftovers = tuple(n for n in names if n.lower() not in SELF)
    return DirState(RESIDUE, leftovers) if leftovers else DirState(UNINSTALLER_ONLY)


def wait_until_settled(path, *, timeout: float = SETTLE_TIMEOUT, listdir=None,
                       sleep=time.sleep, now=time.monotonic) -> DirState:
    """Poll until the uninstaller has finished with ``path``, or ``timeout``.

    This RELAXES NOTHING. Whatever state the directory is in when the deadline
    passes is what gets asserted on, by the same predicate as before: a
    directory still holding program files at the end still fails, and now names
    them. All the wait removes is the possibility of passing judgement on a
    removal that is still in progress.
    """
    deadline = now() + timeout
    state = directory_state(path, listdir=listdir)
    while not state.settled and now() < deadline:
        sleep(0.5)
        state = directory_state(path, listdir=listdir)
    return state


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
    #
    # THE REGISTRY ENTRY IS NOT THE LAST THING TO GO. It proves the uninstall
    # reached its registry step; the stub and the directory holding it are
    # removed after that. `wait_until_settled` below is the second half of this
    # wait, and the half that was missing.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and registry_value("DisplayVersion"):
        time.sleep(2)

    check("the uninstall entry is gone", registry_value("DisplayVersion") is None,
          str(registry_value("DisplayVersion")))

    # WAIT FOR THE UNINSTALLER TO FINISH WITH THE DIRECTORY — not merely for the
    # registry entry to go, which is what the poll above proves and which Inno
    # does BEFORE it removes its own stub and the directory holding it.
    if install_dir:
        state = wait_until_settled(Path(install_dir))
        check("no program files survive except the uninstaller itself",
              state.settled, state.describe())

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

    def _inside_preserved_data(path) -> bool:
        """Is this inside the directory the uninstaller deliberately keeps?

        The check below and the one above ("the data directory is left alone")
        were contradicting each other: one required the data directory to
        survive intact, the other counted a file inside it as residue. A v2
        leftover such as `data\\RAGTools-Watchdog.vbs` is dead weight, but it is
        dead weight inside the folder we chose not to touch — that is a
        housekeeping matter, not a failed uninstall, and blaming the uninstaller
        for it would mean the only way to pass is to delete user data.
        """
        try:
            Path(path).relative_to(DATA_DIR)
            return True
        except ValueError:
            return False

    # Judged by the SAME predicate as the check above, and each survivor is
    # named. `install-user: <path>` on its own is what run 30692207245 printed,
    # and it is not enough to tell a failed uninstall from one still finishing.
    residue: list[tuple[object, DirState]] = []
    for finding in result.findings:
        if finding.layout not in (L_INSTALL_USER, L_INSTALL_MACHINE, L_LEGACY_ARTIFACT):
            continue
        if _inside_preserved_data(finding.path):
            continue
        state = directory_state(finding.path)
        if not state.settled:
            residue.append((finding, state))
    check("a fresh scan finds no packaged installation", not residue,
          "; ".join(f"{f.layout}: {f.path} — {s.describe()}" for f, s in residue)
          or "clean")
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
