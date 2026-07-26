"""Find every previous installation — including the ones nothing could see.

An upgrade cannot remove what it cannot enumerate. That is not a theoretical
concern: the machine this was written on carries, simultaneously,

* an installed build under two spellings of one directory
  (``Programs\\RAGTools`` and ``Programs\\ragtools``),
* a ``RAGTools Watchdog`` scheduled task,
* ``RAGTools.vbs`` **and** ``RAGTools-Tray.vbs`` in the Startup folder,
* and the install directory repeated **sixteen times** on ``PATH``, because the
  installer's "is it already there?" check failed on every upgrade and appended
  again.

None of that was visible to any code, so every upgrade added rather than
replaced. This module's only job is to see it.

**Nothing here mutates anything.** A scan produces a plan the user can read
before a single process is stopped. Removal lives in :mod:`ragtools.upgrade.apply`.

The hard rule is the exclusion list: development data directories, worktrees and
non-``installed`` profiles are never upgrade candidates. A release that deletes a
developer's isolated environment because it pattern-matched on the name is worse
than one that leaves a stale file behind.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

#: Directory names that mark a NON-installed environment. Matching any of these
#: anywhere in a path disqualifies it from every cleanup action.
DEV_MARKERS = (
    "ragtools-dev", "rag-v3-dev", "rag-v3-e2e", "ragtools-e2e",
    ".git", "site-packages", "node_modules",
)

#: Layout identifiers, mirroring the release plan's L1–L5.
L_INSTALL_USER = "install-user"        # per-user Inno install
L_INSTALL_MACHINE = "install-machine"  # Program Files / /opt / /Applications
L_INSTALL_PIP = "install-pip"          # a venv/pip `rag` on PATH
L_DATA = "data"                        # the data directory
L_LEGACY_ARTIFACT = "legacy-artifact"  # VBS launchers, watchdog scripts


@dataclass(frozen=True)
class Finding:
    """One thing the upgrade found. ``removable`` is a recommendation, not an act."""

    layout: str
    path: Path
    detail: str = ""
    removable: bool = False
    #: Why it must NOT be touched, when it must not be.
    protected_reason: str = ""

    @property
    def protected(self) -> bool:
        return bool(self.protected_reason)


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    #: Duplicate PATH entries pointing at the product, in the order found.
    path_entries: list[str] = field(default_factory=list)
    #: Autostart/scheduler registrations, from the platform adapter.
    registrations: list = field(default_factory=list)

    def by_layout(self, layout: str) -> list[Finding]:
        return [f for f in self.findings if f.layout == layout]

    @property
    def removable(self) -> list[Finding]:
        return [f for f in self.findings if f.removable and not f.protected]

    @property
    def protected(self) -> list[Finding]:
        return [f for f in self.findings if f.protected]

    @property
    def duplicate_path_entries(self) -> list[str]:
        """PATH entries beyond the first for the same resolved directory.

        Sixteen copies of one directory is not a cosmetic problem: it decides
        which ``rag`` runs, and two of the entries on this machine differ only
        by case.
        """
        seen: set[str] = set()
        dupes: list[str] = []
        for entry in self.path_entries:
            key = _path_key(entry)
            if key in seen:
                dupes.append(entry)
            else:
                seen.add(key)
        return dupes


def _path_key(value: str) -> str:
    """Identity of a PATH entry: resolved, normalised, case-folded.

    ``Programs\\RAGTools`` and ``Programs\\ragtools`` are one directory on
    Windows and must not be counted as two installs.
    """
    try:
        resolved = str(Path(value).expanduser().resolve())
    except OSError:
        resolved = value
    return os.path.normcase(resolved.rstrip("\\/"))


def is_development_path(path: Path | str) -> bool:
    """Whether a path belongs to a development environment.

    Checked before every removal. An installer that deletes an isolated dev
    store because the name looked close enough has done more damage than the
    stale files it was cleaning up.
    """
    text = os.path.normcase(str(path))
    return any(marker in text for marker in (os.path.normcase(m) for m in DEV_MARKERS))


def scan(
    *,
    adapter=None,
    path_value: Optional[str] = None,
    extra_roots: Iterable[Path] = (),
) -> ScanResult:
    """Enumerate previous installations, data, legacy artifacts and PATH state.

    Pure inspection. ``adapter`` and ``path_value`` are injectable so the suite
    can build a machine that looks exactly like the one in the field without
    touching this one.
    """
    from ragtools.platform import KIND_SERVICE, KIND_TRAY, PlatformUnsupported

    if adapter is None:
        from ragtools.platform import adapter as _adapter

        try:
            adapter = _adapter()
        except PlatformUnsupported:
            adapter = None

    result = ScanResult()

    # --- installation directories --------------------------------------
    candidates: list[tuple[str, Path]] = []
    if adapter is not None:
        app = adapter.app_dir()
        candidates.append((L_DATA, app))
        # The per-user program directory sits beside the data directory on
        # Windows and is a sibling convention elsewhere.
        candidates.append((L_INSTALL_USER, app.parent / "Programs" / "RAGTools"))
    for root in extra_roots:
        candidates.append((L_INSTALL_MACHINE, Path(root)))

    for layout, path in candidates:
        if not path.exists():
            continue
        if is_development_path(path):
            result.findings.append(Finding(
                layout, path, "development environment",
                removable=False,
                protected_reason="development path — never touched by an upgrade",
            ))
            continue
        result.findings.append(Finding(
            layout, path,
            "installed program files" if layout != L_DATA else "data directory",
            # The data directory is RENAMED, not deleted, so rollback stays
            # real past the migration boundary.
            removable=(layout != L_DATA),
        ))

    # --- legacy artifacts ----------------------------------------------
    for finding in list(result.findings):
        if finding.layout != L_DATA or finding.protected:
            continue
        for stale in ("RAGTools-Watchdog.vbs", "service.pid", "supervisor.pid", "tray.pid"):
            candidate = finding.path / "data" / stale
            if candidate.exists():
                result.findings.append(Finding(
                    L_LEGACY_ARTIFACT, candidate,
                    "superseded runtime artifact", removable=True,
                ))

    # --- PATH ------------------------------------------------------------
    raw = path_value if path_value is not None else os.environ.get("PATH", "")
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        if "ragtools" in os.path.normcase(entry) and not is_development_path(entry):
            result.path_entries.append(entry)

    # --- autostart registrations ----------------------------------------
    if adapter is not None:
        for kind in (KIND_SERVICE, KIND_TRAY):
            try:
                result.registrations.extend(adapter.find_autostart(kind))
            except Exception:  # noqa: BLE001 — a scan never fails the upgrade
                pass

    return result


def summarize(result: ScanResult) -> str:
    """Human-readable plan preview. Shown before anything is stopped."""
    lines: list[str] = []
    if result.findings:
        lines.append("Found:")
        for finding in result.findings:
            mark = "protected" if finding.protected else ("remove" if finding.removable else "keep")
            lines.append(f"  [{mark}] {finding.layout}: {finding.path} — {finding.detail}"
                         + (f" ({finding.protected_reason})" if finding.protected else ""))
    dupes = result.duplicate_path_entries
    if dupes:
        lines.append(f"PATH: {len(result.path_entries)} product entries, "
                     f"{len(dupes)} duplicate(s) to remove")
    legacy = [r for r in result.registrations if getattr(r, "legacy", False)]
    if legacy:
        lines.append("Superseded autostart registrations:")
        lines.extend(f"  {r.describe()}" for r in legacy)
    return "\n".join(lines) or "Nothing found from a previous installation."
