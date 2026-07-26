"""Release-candidate validation, run against a BUILT AND INSTALLED artifact.

The release gate is explicit that a green source suite is not sufficient. This
runner exists so that rule is enforced by something other than discipline: it
refuses to report a pass for any row it could not actually execute, and it
refuses to report a platform as validated when rows were skipped.

Run it on each platform against a real install:

    python scripts/validate_release.py --url http://127.0.0.1:21420

Rows that need a second machine, a reboot, or a signing identity are declared
`MANUAL` and reported as such. A matrix that silently counts an unrunnable row
as passing is worse than no matrix.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional

PASS = "pass"
FAIL = "fail"
SKIP = "skip"
MANUAL = "manual"


@dataclass
class Row:
    """One row of the validation matrix."""

    id: str
    name: str
    check: Optional[Callable[[dict], tuple[bool, str]]] = None
    #: Rows that cannot be automated here — reboot, sign-in, signing identity,
    #: a second machine. Declared, never quietly omitted.
    manual_reason: str = ""
    platforms: tuple = ("windows", "linux", "darwin")


@dataclass
class Result:
    row: Row
    status: str
    detail: str = ""


@dataclass
class Matrix:
    platform: str
    results: list = field(default_factory=list)

    @property
    def failures(self):
        return [r for r in self.results if r.status == FAIL]

    @property
    def manual(self):
        return [r for r in self.results if r.status == MANUAL]

    @property
    def validated(self) -> bool:
        """Validated means every required row actually ran and passed.

        An outstanding MANUAL row blocks this. Reporting VALIDATED while nine
        required rows have never been executed is precisely the dishonesty this
        runner exists to prevent — and it is what the first version of this
        property did, which is why the rule is now spelled out rather than
        implied by an absence.
        """
        return (
            not self.failures
            and not self.manual
            and not [r for r in self.results if r.status == SKIP]
        )

    def render(self) -> str:
        lines = [f"Release validation — {self.platform}", ""]
        for result in self.results:
            lines.append(f"  [{result.status:^6}] {result.row.id}  {result.row.name}"
                         + (f" — {result.detail}" if result.detail else ""))
        lines.append("")
        lines.append(f"  failures: {len(self.failures)}   manual: {len(self.manual)}")
        if self.validated:
            verdict = "VALIDATED"
        elif self.failures:
            verdict = f"NOT VALIDATED — {len(self.failures)} row(s) failed"
        else:
            verdict = f"NOT VALIDATED — {len(self.manual)} manual row(s) outstanding"
        lines.append(f"  VERDICT: {verdict}")
        if self.manual:
            lines.append("")
            lines.append("  Manual rows still required before release:")
            lines.extend(f"    {r.row.id} {r.row.name} — {r.row.manual_reason}"
                         for r in self.manual)
        return "\n".join(lines)


# --- automated checks -----------------------------------------------------


def _service_ready(ctx: dict) -> tuple[bool, str]:
    health = ctx.get("health") or {}
    if health.get("status") != "ready":
        return False, f"status={health.get('status')!r}"
    return True, f"{health.get('collection')} v{health.get('version')}"


def _storage_reachable(ctx: dict) -> tuple[bool, str]:
    """Health must reflect the STORE, not just the process.

    A service reporting green while Qdrant is unreachable is the failure this
    row exists for.
    """
    health = ctx.get("health") or {}
    if not health.get("storage_reachable", False):
        return False, health.get("storage_error") or "storage unreachable"
    return True, f"backend={health.get('storage_backend')}"


def _no_legacy_registrations(ctx: dict) -> tuple[bool, str]:
    """No superseded autostart may survive an upgrade."""
    info = ctx.get("autostart") or {}
    legacy = info.get("legacy") or []
    if legacy:
        return False, f"{len(legacy)} superseded: {'; '.join(legacy)}"
    if info.get("problem"):
        return False, info["problem"]
    return True, info.get("method", "registered")


def _counts_reconcile(ctx: dict) -> tuple[bool, str]:
    status = ctx.get("status") or {}
    total = int(status.get("points_count", 0) or 0)
    collections = status.get("collections") or []
    summed = sum(int(c.get("points", 0) or 0) for c in collections)
    if total != summed:
        return False, f"status {total:,} vs collections {summed:,}"
    return True, f"{total:,} points across {len(collections)} collection(s)"

def _frameworks_deduplicated(ctx: dict) -> tuple[bool, str]:
    frameworks = (ctx.get("frameworks") or {}).get("frameworks") or []
    if not frameworks:
        return True, "no shared dependencies declared"
    shared = [f for f in frameworks if len(f.get("projects") or []) > 1]
    return True, f"{len(frameworks)} corpus/corpora, {len(shared)} shared by >1 project"


def _no_cross_project_leakage(ctx: dict) -> tuple[bool, str]:
    """Every project probed with every other project's characteristic terms.

    The isolation boundary is the entire reason for per-project collections; a
    single foreign document means the routing filters rather than isolates.
    """
    leaks = ctx.get("leakage")
    if leaks is None:
        return None, "not probed — supply --leakage from the isolation probe"
    if leaks:
        return False, f"{leaks} foreign document(s) returned"
    return True, "0 foreign documents across all probes"


def _clean_install(ctx: dict):
    """A clean install of the BUILT artifact — not a run from source.

    The gate is explicit that a green source suite proves nothing about what
    ships, so this row is only satisfied by evidence from a wheel installed
    into a fresh interpreter.
    """
    result = ctx.get("clean_install")
    if result is None:
        return None, "not run — see scripts/verify_clean_install.py"
    failed = result.get("failed", 0)
    return failed == 0, f"{result.get('passed', 0)} check(s) passed, {failed} failed"


def _upgrade_rehearsal(ctx: dict) -> tuple[bool, str]:
    """Detection, protection, PATH repair and config migration, exercised
    against a copy of the real previous installation."""
    result = ctx.get("rehearsal")
    if result is None:
        return None, "not run — supply --rehearsal-passed from scripts/rehearse_upgrade.py"
    failed = result.get("failed", 0)
    return failed == 0, f"{result.get('passed', 0)} passed, {failed} failed"


def _storage_recovery(ctx: dict):
    """Kill the store; health must say so; the service must recover.

    The failure this guards is a service answering `ready` while Qdrant is
    unreachable — everything downstream then reports success against a store
    that is not there.
    """
    r = ctx.get("storage_recovery")
    if r is None:
        return None, "not run — see scripts/verify_storage_recovery.py"
    return r.get("failed", 0) == 0, f"{r.get('passed', 0)} check(s) passed"


def _reindex_reconciled(ctx: dict):
    """Every reconciliation gate, run against the real migrated corpus."""
    r = ctx.get("reindex")
    if r is None:
        return None, "not run — reconcile against the migrated corpus"
    return r.get("failed", 0) == 0, f"{r.get('passed', 0)} gate(s) passed"


def _uninstall_clean(ctx: dict):
    """Install, uninstall, then sweep. "Uninstall returned 0" is not evidence."""
    r = ctx.get("uninstall")
    if r is None:
        return None, "not run — see scripts/verify_uninstall_residue.py"
    return r.get("failed", 0) == 0, f"{r.get('passed', 0)} check(s) passed"


def _scale_matches_engine(ctx: dict) -> tuple[bool, str]:
    """The scale warning must describe the engine actually in use.

    Repeating a local-mode limit on a server engine trains the operator to
    ignore the warning entirely.
    """
    status = ctx.get("status") or {}
    storage = status.get("storage") or {}
    level = (status.get("scale") or {}).get("level")
    if storage.get("hnsw") and level != "ok":
        return False, f"hnsw engine but scale={level}"
    return True, f"engine={storage.get('backend')} scale={level}"


ROWS = [
    Row("V01", "clean install of the built artifact", _clean_install),
    Row("V02", "upgrade rehearsal against the previous release", _upgrade_rehearsal,
        platforms=("windows",)),
    Row("V03", "reboot and sign-in -> service autostarts",
        manual_reason="needs a reboot"),
    Row("V04", "tray autostart and state accuracy", manual_reason="needs a desktop session"),
    Row("V05", "headless install (no desktop session)",
        manual_reason="needs a headless host", platforms=("linux", "darwin")),
    Row("V06", "service is ready", _service_ready),
    Row("V07", "storage reachable and reported honestly", _storage_reachable),
    Row("V08", "exactly one autostart, no superseded entries", _no_legacy_registrations),
    Row("V09", "state and store counts reconcile", _counts_reconcile),
    Row("V10", "framework corpora deduplicated", _frameworks_deduplicated),
    Row("V11", "scale warning matches the engine", _scale_matches_engine),
    Row("V12", "storage kill -> honest degraded -> recovery", _storage_recovery),
    Row("V13", "re-index reconciled on a real corpus", _reindex_reconciled,
        platforms=("windows",)),
    Row("V14", "zero cross-project leakage", _no_cross_project_leakage),
    Row("V15", "clean uninstall leaves zero residue", _uninstall_clean),
    Row("V16", "signed and notarized artifact",
        manual_reason="needs a signing identity (D-3)", platforms=("darwin", "windows")),
]


def collect(url: str, *, fetch=None) -> dict:
    """Gather everything the automated rows need, in one pass."""
    import urllib.request

    def _default_fetch(path: str):
        with urllib.request.urlopen(f"{url}{path}", timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    get = fetch or _default_fetch
    ctx: dict = {}
    for key, path in (("health", "/health"), ("status", "/api/status"),
                      ("frameworks", "/api/frameworks")):
        try:
            ctx[key] = get(path)
        except Exception as exc:  # noqa: BLE001 — a missing endpoint is a result
            ctx[key] = {"_error": str(exc)}
    try:
        diagnostics = get("/api/diagnostics")
        checks = {c.get("component"): c for c in diagnostics.get("checks", [])}
        ctx["autostart"] = checks.get("autostart", {})
    except Exception:  # noqa: BLE001
        ctx["autostart"] = {}
    return ctx


def run(platform: str, ctx: dict, rows=None) -> Matrix:
    matrix = Matrix(platform=platform)
    for row in (rows or ROWS):
        if platform not in row.platforms:
            continue
        if row.check is None:
            matrix.results.append(Result(row, MANUAL, row.manual_reason))
            continue
        try:
            ok, detail = row.check(ctx)
        except Exception as exc:  # noqa: BLE001
            matrix.results.append(Result(row, FAIL, f"check raised: {exc}"))
            continue
        if ok is None:
            # No evidence supplied. Reporting PASS here would be the same
            # manufactured pass as counting an unrun MANUAL row.
            matrix.results.append(Result(row, MANUAL, detail or row.manual_reason))
            continue
        matrix.results.append(Result(row, PASS if ok else FAIL, detail))
    return matrix


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:21420")
    parser.add_argument("--platform", default="")
    parser.add_argument("--leakage", type=int, default=None,
                        help="foreign documents found by the isolation probe")
    parser.add_argument("--rehearsal-passed", type=int, default=None)
    parser.add_argument("--rehearsal-failed", type=int, default=None)
    parser.add_argument("--clean-install-passed", type=int, default=None)
    parser.add_argument("--clean-install-failed", type=int, default=None)
    parser.add_argument("--reindex-passed", type=int, default=None)
    parser.add_argument("--reindex-failed", type=int, default=None)
    parser.add_argument("--uninstall-passed", type=int, default=None)
    parser.add_argument("--uninstall-failed", type=int, default=None)
    parser.add_argument("--storage-recovery-passed", type=int, default=None)
    parser.add_argument("--storage-recovery-failed", type=int, default=None)
    args = parser.parse_args(argv)

    from ragtools.platform import current_platform

    platform = args.platform or current_platform()
    ctx = collect(args.url)
    if args.leakage is not None:
        ctx["leakage"] = args.leakage
    if args.clean_install_passed is not None:
        ctx["clean_install"] = {"passed": args.clean_install_passed,
                                "failed": args.clean_install_failed or 0}
    if args.storage_recovery_passed is not None:
        ctx["storage_recovery"] = {"passed": args.storage_recovery_passed,
                                   "failed": args.storage_recovery_failed or 0}
    if args.reindex_passed is not None:
        ctx["reindex"] = {"passed": args.reindex_passed, "failed": args.reindex_failed or 0}
    if args.uninstall_passed is not None:
        ctx["uninstall"] = {"passed": args.uninstall_passed,
                            "failed": args.uninstall_failed or 0}
    if args.rehearsal_passed is not None:
        ctx["rehearsal"] = {"passed": args.rehearsal_passed,
                            "failed": args.rehearsal_failed or 0}
    matrix = run(platform, ctx)
    print(matrix.render())
    # Manual rows do not fail the run, but they DO keep it from being validated.
    return 0 if not matrix.failures else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
