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


#: What KIND of problem a failing check represents. The installer displays a
#: fixed sentence per verdict, so a single pass/fail bit made it blame stale
#: processes and prescribe a reboot for a storage outage — a wrong diagnosis
#: with a wrong remedy, on a machine whose files were byte-perfect.
CATEGORY_INTEGRITY = "integrity"    # the files/registrations are wrong
CATEGORY_RUNTIME = "runtime"        # installed fine; something at run time is stuck
CATEGORY_MIGRATING = "migrating"    # installed fine; a rebuild is legitimately running
CATEGORY_WARNING = "warning"        # worth saying, not worth failing over

#: Exit codes `rag selfcheck` returns, and the installer branches on. Ordered by
#: severity so a caller that only compares can still behave sensibly.
EXIT_CLEAN = 0
EXIT_INTEGRITY = 1
EXIT_RUNTIME = 2
EXIT_MIGRATING = 3
EXIT_WARNING = 4


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
    #: Which remedy this check's failure calls for. Only meaningful when failing.
    category: str = CATEGORY_INTEGRITY

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
                seen += 1
                target = (registration.target or "").strip('" ')
                if not target:
                    # FOUND but UNVERIFIABLE — a finding, not a non-event.
                    #
                    # This used to `continue` without counting, so a registration
                    # whose target could not be read vanished from the tally and
                    # the check reported "no autostart registered". On Windows
                    # that was every registration on every machine, because the
                    # query never asked for the command. The check could neither
                    # pass nor fail, which is the worst of the three.
                    wrong.append(
                        f"{registration.name}: registered, but its target could "
                        f"not be read — cannot verify what starts at logon")
                    continue
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
    except Exception:  # noqa: BLE001
        # On a PACKAGED installation, silence is a finding.
        #
        # Installation now starts the service and waits for it, so a finished
        # install that answers nothing has not finished. This used to skip
        # unconditionally, on the reasonable-at-the-time basis that setup ran
        # before anything was started — which stopped being true when service
        # startup became mandatory rather than gated on the browser checkbox.
        #
        # From a source checkout there is still nothing to expect, so that stays
        # a skip.
        if _is_packaged():
            return Check("service health version", False,
                         "nothing is listening — a completed installation must "
                         "leave the service running", category=CATEGORY_RUNTIME)
        return Check("service health version", True, "no service responding",
                     skipped=True)

    running = payload.get("version")
    return Check("service health version", running == expect,
                 f"/health reports {running}, expected {expect}")


def check_config_schema() -> Check:
    """The configuration must be at the schema this release reads.

    A machine can be perfectly installed and still be running the previous
    product: v3.0.0 and v3.0.1 both shipped a migration nothing invoked, so
    every upgraded installation kept a v2 configuration, fell back to v2 code
    defaults, and left the v3 architecture unreachable behind a file nobody
    rewrote. Nothing reported that — which is why it survived two releases.
    """
    try:
        from ragtools.config import CONFIG_VERSION, Settings, _find_config_path

        existing = _find_config_path()
        found = int(getattr(Settings(), "config_version", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        return Check("config schema", True, f"could not read the config: {exc}",
                     skipped=True)

    # "No configuration file" is not "a configuration at the wrong schema".
    # Collapsing the two reports a stale config on every machine that has never
    # written one — a source checkout, a fresh container, a CI runner — which is
    # the same three-state mistake `recorded_version` made, in a new place.
    if not existing or not Path(existing).is_file():
        return Check("config schema", True,
                     "no configuration file yet", skipped=True)

    if found == CONFIG_VERSION:
        return Check("config schema", True, f"version {found}")
    return Check("config schema", False,
                 f"config is version {found}, this release reads {CONFIG_VERSION} "
                 f"— run `rag upgrade`", category=CATEGORY_RUNTIME)


def check_migration_state() -> Check:
    """Whether this process migrated the configuration, and whether it worked."""
    try:
        from ragtools.bootstrap import last_result

        result = last_result()
    except Exception as exc:  # noqa: BLE001
        return Check("migration state", True, f"unavailable: {exc}", skipped=True)

    if result is None:
        return Check("migration state", True,
                     "no migration ran in this process", skipped=True)
    return Check("migration state", not result.degraded, result.describe(),
                 category=CATEGORY_RUNTIME)


def check_storage_contract() -> Check:
    """The engine and layout actually in force, and whether they are supported.

    Reports them even when correct. "Which engine am I really on?" was
    unanswerable without reading the source, and an operator who cannot see the
    answer cannot notice it is wrong.
    """
    try:
        from ragtools.config import (
            _SUPPORTED_BACKENDS,
            _SUPPORTED_STRATEGIES,
            Settings,
        )

        settings = Settings()
        backend = (settings.storage_backend or "").strip().lower()
        strategy = (settings.collection_strategy or "").strip().lower()
    except Exception as exc:  # noqa: BLE001
        # A Settings() that raises IS the finding — the contract validator
        # refuses an unsupported configuration at load.
        return Check("storage contract", False, f"configuration refused: {exc}",
                     category=CATEGORY_RUNTIME)

    detail = f"{backend} + {strategy}"
    ok = backend in _SUPPORTED_BACKENDS and strategy in _SUPPORTED_STRATEGIES
    return Check("storage contract", ok, detail, category=CATEGORY_RUNTIME)


def check_index_identity() -> Check:
    """Does the state DB describe the store this release will write to?

    When it does not, incremental indexing skips files whose hashes match
    against a store that does not hold them — observed once as
    "indexed 0, skipped 38286" with ~28k chunks silently missing. Not a failure:
    a pending re-index is the correct response to a layout change, and saying so
    is the point.
    """
    try:
        from ragtools.config import Settings
        from ragtools.index_identity import current_identity, reconcile
        from ragtools.indexing.state import IndexState

        settings = Settings()
        if not Path(settings.state_db).exists():
            return Check("index identity", True, "no index yet", skipped=True)

        state = IndexState(settings.state_db)
        try:
            from ragtools.embedding.encoder import Encoder

            # No ``registry=`` on purpose. Opening one here would CREATE
            # registry.db and sync it from config — a write, from a read-only
            # health check, against a file the running service holds. The
            # fingerprint is therefore "unknown", which `registry_changed`
            # treats as unknown rather than different, so doctor never
            # contradicts the service: it reports every other field, and the
            # registry's integrity is the service's to judge while it holds the
            # registry open (see `registry_integrity`, surfaced on /health).
            identity = current_identity(settings, Encoder(settings.embedding_model).dimension)
            trustworthy, changed = reconcile(state, identity)
        finally:
            state.close()
    except Exception as exc:  # noqa: BLE001
        return Check("index identity", True, f"could not be checked: {exc}",
                     skipped=True)

    if trustworthy:
        return Check("index identity", True, "state describes the current store")
    return Check("index identity", True,
                 f"re-index pending ({', '.join(changed)}) — run `rag index`",
                 skipped=True)


def check_reindex_state() -> Check:
    """A migration that stopped half-way is a FAILURE, not a status line.

    The machine is then running a v3 configuration over an index that was never
    rebuilt for it: some projects present, some absent, and every query against
    the missing ones answering "no matches" in the ordinary, reassuring shape.
    Left as an informational note it would be read as progress and ignored — so
    it fails, names the units, and names the retry.
    """
    try:
        from ragtools.config import Settings
        from ragtools.upgrade import relayout

        settings = Settings()
        plan = relayout.active_plan(settings)
        if plan is None:
            return Check("reindex state", True, "no migration pending")
        report = relayout.progress(settings, plan)
    except Exception as exc:  # noqa: BLE001
        return Check("reindex state", True, f"could not be checked: {exc}",
                     skipped=True)

    if report is None or report.complete:
        return Check("reindex state", True, "no migration pending")

    detail = report.describe()
    if report.failures:
        named = "; ".join(f"{k} {i}: {e}" for k, i, e in report.failures[:3])
        detail += f" — {named}"

    # INCOMPLETE IS NEVER READY — that invariant does not move. An index that
    # was never rebuilt answers "no matches" for every missing project, in the
    # ordinary reassuring shape, so this stays a failure in both branches.
    #
    # What differs is the REMEDY, and therefore the category. A rebuild that is
    # merely in flight needs no action; one that has stopped needs a person. The
    # installer used to receive a single bit for both and told a user whose
    # index was quietly rebuilding to reboot Windows and reinstall.
    if not getattr(report, "stalled", False):
        return Check("reindex state", False,
                     f"{detail}. The service resumes this on its own; "
                     f"`rag upgrade --resume` runs it now.",
                     category=CATEGORY_MIGRATING)
    return Check("reindex state", False,
                 f"{detail}. Retry with `rag upgrade --resume`",
                 category=CATEGORY_RUNTIME)


def run_selfcheck(expect_version: str, *, port: int | None = None) -> list[Check]:
    """Every check, in the order a reader should think about them.

    Installation first — is this machine running this release at all — then the
    configuration and storage that decide what the release actually does. An
    installation can be byte-perfect and still be the previous product.
    """
    return [
        check_own_version(expect_version),
        check_windowed_executable(),
        check_recorded_version(expect_version),
        check_running_processes(),
        check_autostart_targets(),
        check_service_health(expect_version, port),
        check_config_schema(),
        check_migration_state(),
        check_storage_contract(),
        check_index_identity(),
        check_reindex_state(),
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


def classify(checks: list[Check]) -> str:
    """The single worst category present. Integrity outranks everything.

    Ordered by what the operator must DO, not by how alarming it sounds. A
    machine whose files are wrong needs the installer run again; one whose
    rebuild is merely in flight needs nothing at all. Reporting the second as
    the first is the defect this exists to end.
    """
    present = {c.category for c in failures(checks)}
    for category in (CATEGORY_INTEGRITY, CATEGORY_RUNTIME, CATEGORY_MIGRATING,
                     CATEGORY_WARNING):
        if category in present:
            return category
    return ""


def exit_code(checks: list[Check]) -> int:
    """The process exit code for a set of results — what the installer branches on."""
    return {
        "": EXIT_CLEAN,
        CATEGORY_INTEGRITY: EXIT_INTEGRITY,
        CATEGORY_RUNTIME: EXIT_RUNTIME,
        CATEGORY_MIGRATING: EXIT_MIGRATING,
        CATEGORY_WARNING: EXIT_WARNING,
    }[classify(checks)]


def as_dict(checks: list[Check], *, expected: str) -> dict:
    """Machine-readable results, for the installer and any other caller.

    The installer cannot import this module — it is Pascal — and classification
    does not belong in an installer script anyway. So the CLI does the deciding
    and hands over a verdict, rather than a bit the caller has to interpret.
    """
    verdict = classify(checks)
    return {
        "expected_version": expected,
        "category": verdict or "clean",
        "exit_code": exit_code(checks),
        "ok": not verdict,
        "checks": [
            {"name": c.name, "status": c.status, "detail": c.detail,
             "category": c.category}
            for c in checks
        ],
    }
