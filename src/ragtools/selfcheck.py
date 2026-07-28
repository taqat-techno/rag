"""Does the installation on this machine actually belong to this release?

An installer that copies files and reports success has proven that it copied
files. It has not proven that the machine now runs the new version — and the
two come apart in ordinary, undramatic ways:

* a process from the old install is still running and holding its own binaries,
  so the copy silently skipped them;
* a scheduled task still names an executable the new release does not ship;
* the uninstall registry entry still reads the old version, so Add/Remove
  Programs, winget and every upgrade check disagree with the files on disk;
* the whole run was blocked before it started and nothing changed at all.

Every one of those looks identical from inside the installer: no error. So this
module asks the machine, after the fact, and reports what it finds rather than
what was intended.

It ships inside the product deliberately. A verifier written in a script the
target machine cannot run is a verifier that only runs in CI, and CI is not
where installations go wrong.

**Every platform-specific answer comes from the platform adapter**, not from a
branch here. Registry lookup, process enumeration and "does this build ship a
windowless binary" are all facts about an OS, and this module exists to compare
them — not to know them. See `ragtools.platform.base`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    #: A check that could not run (wrong platform, service not started) is not a
    #: failure — but it must not be counted as a pass either. Collapsing the two
    #: would let a machine where nothing is inspectable report a clean bill of
    #: health, which is the false success this module exists to prevent.
    skipped: bool = False

    @property
    def status(self) -> str:
        return "SKIP" if self.skipped else ("PASS" if self.ok else "FAIL")


def _install_dir() -> Path:
    """The directory the running executable lives in."""
    return Path(sys.executable).parent


def _is_packaged() -> bool:
    from ragtools.config import is_packaged

    return bool(is_packaged())


def _adapter():
    """The platform adapter, or None when this platform has none."""
    try:
        from ragtools.platform import adapter

        return adapter()
    except Exception:  # noqa: BLE001 — no adapter is an answer, not a crash
        return None


def check_own_version(expect: str) -> Check:
    """The most basic question, and the one a failed replacement answers wrong."""
    from ragtools import __version__

    return Check("installed version", __version__ == expect,
                 f"running {__version__}, expected {expect}")


def check_windowed_executable() -> Check:
    """The windowless sibling, where the platform ships one.

    Its absence after an upgrade to a release that ships it is the signature of
    a replacement that did not happen: the old bundle had no such file, so a
    tree still missing it is the old tree.

    Whether one is expected is the adapter's answer, not a flag passed in — a
    caller that has to know would have to branch on the OS to decide.
    """
    impl = _adapter()
    name = getattr(impl, "windowed_executable_name", None) if impl else None
    if name is None:
        return Check("windowed executable", True,
                     "this platform ships no windowless variant", skipped=True)
    if not _is_packaged():
        return Check("windowed executable", True, "source install", skipped=True)
    candidate = _install_dir() / name
    return Check("windowed executable", candidate.is_file(), str(candidate))


def check_recorded_version(expect: str) -> Check:
    """What the OS's own package database says is installed.

    A stale entry is not cosmetic: winget, upgrade detection and the user's own
    "what is installed?" all read it, and Inno itself uses it to decide whether
    the next run is an upgrade.
    """
    impl = _adapter()
    if impl is None or not getattr(impl, "records_installed_version", False):
        return Check("recorded install version", True,
                     "this platform records no installed version", skipped=True)

    # A source checkout has no business appearing in the OS package database,
    # so its absence there is not a finding. Every sibling check already asks
    # this; this one did not, which is why it reported a FAIL on any Windows
    # machine that had never installed the packaged product — including every
    # CI runner.
    if not _is_packaged():
        return Check("recorded install version", True, "source install", skipped=True)

    recorded = impl.recorded_version()
    if recorded is None:
        return Check("recorded install version", False,
                     "the system records no installation of this product")
    return Check("recorded install version", recorded == expect,
                 f"the system records {recorded}, expected {expect}")


def check_running_processes() -> Check:
    """Nothing owned may still be running from outside this installation.

    This is the check that catches the failure the others miss. Files can be
    correct on disk while the service the user is actually talking to is the
    previous build, still resident because nothing stopped it — and a running
    old binary is also what prevents its own replacement in the first place.
    """
    if not _is_packaged():
        return Check("running processes", True, "source install", skipped=True)
    impl = _adapter()
    processes = impl.owned_processes() if impl else None
    if processes is None:
        return Check("running processes", True,
                     "processes could not be enumerated on this platform",
                     skipped=True)

    install = _install_dir().resolve()
    strays = []
    for pid, image, path in processes:
        if not path:
            continue
        # Storage lives under the data dir, not the program dir: owned, but not
        # part of the tree an upgrade replaces.
        if Path(image).stem.lower() == "qdrant":
            continue
        try:
            resolved = Path(path).resolve()
        except OSError:
            continue
        if install not in resolved.parents:
            strays.append(f"{image} pid={pid} at {resolved}")
    return Check("running processes", not strays,
                 "; ".join(strays) or f"all owned processes run from {install}")


def check_autostart_targets() -> Check:
    """Every registration must name a binary inside this installation.

    A task still pointing at the previous directory starts the previous build at
    the next login — so the machine reverts on reboot while every file-level
    check says the upgrade worked.
    """
    if not _is_packaged():
        return Check("autostart targets", True, "source install", skipped=True)
    impl = _adapter()
    if impl is None:
        return Check("autostart targets", True, "no platform adapter", skipped=True)

    from ragtools.platform import KIND_SERVICE, KIND_TRAY

    install = _install_dir().resolve()
    wrong, seen = [], 0
    try:
        for kind in (KIND_SERVICE, KIND_TRAY):
            for registration in impl.find_autostart(kind):
                if registration.legacy:
                    wrong.append(f"legacy registration survives: {registration.name}")
                    continue
                target = (registration.target or "").strip('" ')
                if not target:
                    continue
                seen += 1
                if str(install).lower() not in target.lower():
                    wrong.append(f"{registration.name} -> {target}")
    except Exception as exc:  # noqa: BLE001
        return Check("autostart targets", True, f"could not enumerate: {exc}",
                     skipped=True)

    if not seen and not wrong:
        return Check("autostart targets", True, "no autostart registered", skipped=True)
    return Check("autostart targets", not wrong,
                 "; ".join(wrong) or f"{seen} registration(s) point into {install}")


def check_service_health(expect: str, port: int | None = None) -> Check:
    """If a service answers, it must be this version.

    Skipped when nothing is listening: a post-install check runs before the
    service has necessarily started, and "not running yet" is not "wrong
    version". But a service that answers with the OLD version is the single
    most direct proof that the machine did not actually move.
    """
    try:
        import httpx

        from ragtools.config import Settings

        resolved = port or Settings().service_port
        payload = httpx.get(f"http://127.0.0.1:{resolved}/health", timeout=5.0).json()
    except Exception:  # noqa: BLE001 — not listening is the normal case here
        return Check("service health version", True, "no service responding", skipped=True)

    running = payload.get("version")
    return Check("service health version", running == expect,
                 f"/health reports {running}, expected {expect}")


def run_selfcheck(expect_version: str, *, port: int | None = None) -> list[Check]:
    """Every check, in the order a reader should think about them."""
    return [
        check_own_version(expect_version),
        check_windowed_executable(),
        check_recorded_version(expect_version),
        check_running_processes(),
        check_autostart_targets(),
        check_service_health(expect_version, port),
    ]


def format_report(checks: list[Check]) -> str:
    lines = [f"  [{c.status}] {c.name}" + (f" — {c.detail}" if c.detail else "")
             for c in checks]
    failed = [c for c in checks if not c.ok and not c.skipped]
    skipped = [c for c in checks if c.skipped]
    lines.append("")
    lines.append(f"  {len(checks) - len(failed) - len(skipped)} passed, "
                 f"{len(failed)} failed, {len(skipped)} skipped")
    return "\n".join(lines)


def failures(checks: list[Check]) -> list[Check]:
    return [c for c in checks if not c.ok and not c.skipped]
