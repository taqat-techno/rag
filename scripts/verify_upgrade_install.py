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




def browser_processes() -> list[str]:
    """Browsers started since setup began.

    The mandatory service start must not drag a browser window onto the screen:
    that was the `startnow` checkbox's job and it stays optional. A silent
    install that opens a browser is a silent install in name only.
    """
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -in "
              "'msedge.exe','chrome.exe','firefox.exe','iexplore.exe' } | "
              "ForEach-Object { \"$($_.Name):$($_.ProcessId)\" }")
    out = run(["powershell", "-NoProfile", "-Command", script], timeout=120).stdout
    return [line.strip() for line in (out or "").splitlines() if line.strip()]


def api_status(port: int = 21420) -> dict | None:
    """`/api/status`, for the collection layout the machine actually ended on."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=10) as r:
            return json.load(r)
    except Exception:  # noqa: BLE001
        return None


#: How long one silent install may take before we call it hung.
#:
#: The previous release installs in ~90 s on a hosted runner. 2400 s was chosen
#: as "surely enough" and had the opposite effect: a hang burned forty minutes
#: and then reported only `TimeoutExpired`, which says the installer did not
#: finish and nothing about where it stopped. A tighter bound fails fast, and
#: the log below says where.
INSTALL_TIMEOUT_SECONDS = 900


def _dump_inno_log(log_path: Path, label: str, *, tail: int = 120) -> None:
    """Print the tail of Inno's own log.

    This is the difference between "the installer hung" and "the installer hung
    at line N". Inno logs every step with a timestamp — the graceful-stop Exec,
    each [InstallDelete] entry, every file it copies, each [Run] entry — so the
    last line before the silence names the operation that never returned.
    """
    if not log_path.is_file():
        print(f"    (no Inno log at {log_path})", flush=True)
        return
    try:
        raw = log_path.read_bytes()
    except OSError as exc:
        print(f"    (could not read the Inno log: {exc})", flush=True)
        return

    # Inno writes UTF-16LE with a BOM. Decoding it as UTF-8 yields a string
    # whose first character is U+FEFF, and printing that to a cp1252 console —
    # which is what a Windows runner gives you — raises UnicodeEncodeError. The
    # first version of this helper did exactly that and crashed the script it
    # was added to diagnose, destroying the evidence instead of reporting it.
    for encoding in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = raw.decode("latin-1")

    lines = text.replace("﻿", "").splitlines()
    print(f"\n--- Inno log for {label} (last {tail} of {len(lines)} lines) ---",
          flush=True)
    # Re-encode through the console's own codec so an unexpected character
    # degrades to '?' rather than taking the whole run down with it.
    codec = sys.stdout.encoding or "utf-8"
    for line in lines[-tail:]:
        print("    " + line.encode(codec, "replace").decode(codec), flush=True)
    print("--- end Inno log ---\n", flush=True)


def install_silently(installer: Path, label: str, *, log_dir: Path | None = None) -> int:
    """Inno silent install. `startnow` is omitted so nothing opens a browser;
    the service is started explicitly instead, which is the state that matters.

    `/LOG=` is not optional instrumentation. A silent installer that stalls
    produces no console output at all, so without the log a failure is
    indistinguishable from a hung runner — and the log is written incrementally,
    which means it survives the timeout that kills the process.
    """
    log_path = (log_dir or installer.parent) / f"inno-{label.split()[0]}.log"
    log_path.unlink(missing_ok=True)
    print(f"\n>>> installing {label}: {installer.name}", flush=True)
    print(f"    log: {log_path}", flush=True)
    argv = [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/TASKS=addtopath,startup", f"/LOG={log_path}"]
    try:
        proc = run(argv, timeout=INSTALL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"    TIMED OUT after {INSTALL_TIMEOUT_SECONDS}s", flush=True)
        _dump_inno_log(log_path, label)
        raise
    print(f"    exit={proc.returncode}", flush=True)
    if proc.returncode != 0:
        _dump_inno_log(log_path, label)
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
    install_silently(previous, f"v{args.from_version}", log_dir=work)

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

    # An AUTHENTIC legacy configuration: projects, and no storage keys at all.
    #
    # That absence is the whole point. A v2 config has no `storage_backend` and
    # no `collection_strategy` because those keys did not exist when it was
    # written, and the release must treat that as a legacy default to migrate —
    # not as a user who chose shared+embedded. Seeding it here is what makes
    # this an upgrade test rather than a fresh-install test wearing its clothes.
    legacy_projects = [work / "legacy_alpha", work / "legacy_beta"]
    for project in legacy_projects:
        project.mkdir(parents=True, exist_ok=True)
        (project / "notes.md").write_text(
            f"# {project.name}\n\nLegacy content that must survive the migration.",
            encoding="utf-8")
    config.write_text(
        "version = 2\n\n"
        + "".join(
            f'[[projects]]\nid = "{p.name}"\npath = '
            f'"{str(p).replace(chr(92), chr(92) * 2)}"\nmode = "docs"\n\n'
            for p in legacy_projects),
        encoding="utf-8")
    print(f"    seeded a legacy v2 config with {len(legacy_projects)} projects "
          f"and NO storage keys", flush=True)

    # --- 2. the upgrade, over a live installation ------------------------
    rc = install_silently(new_installer, f"v{args.version} (over v{args.from_version})",
                          log_dir=work)
    # Name the two failures that are NOT the same failure. Inno's exit 5 means
    # Setup aborted mid-write and the installation is now mixed; exit 7 means
    # `PrepareToInstall` refused BEFORE the first destructive write and the
    # previous version is intact. A leg that reports only "exit 7" sends the
    # next person looking for a half-installed tree that does not exist.
    detail = f"exit {rc}"
    if rc == 7:
        quiesce_log = (LOCALAPPDATA / "RAGTools" / "logs" / "quiesce-blockers.txt")
        blockers = (quiesce_log.read_text(encoding="utf-8", errors="replace").strip()
                    if quiesce_log.is_file() else "(no blocker summary was written)")
        detail = (f"exit 7 — quiescence REFUSED the upgrade before writing anything; "
                  f"the previous version is intact. Blockers:\n{blockers}")
    elif rc == 5:
        detail = ("exit 5 — Setup aborted DURING the write; the installation may be "
                  "mixed. This is the failure quiescence exists to convert into a 7.")
    check("the upgrade installer exited 0", rc == 0, detail)

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
    #
    # WAIT for the rebuild first. A legacy config migrates to a new collection
    # layout, and the index for it does not exist until the service has rebuilt
    # every project — so `selfcheck` correctly FAILS in the meantime, reporting
    # `reindex state: 0/2 rebuilt`. Asserting readiness before that finishes
    # tests the wrong instant: it turns the machinery working as designed into a
    # failure, and would push someone to weaken the check that is telling the
    # truth.
    #
    # Waiting also makes this the stronger assertion. It proves the whole
    # lifecycle — migrate, rebuild, become ready — instead of only the first step.
    # "Only after completion" is half the claim, so prove the other half: while
    # the rebuild is outstanding, the product's own verdict must be NEGATIVE.
    # NOTHING HERE STARTS THE SERVICE. That is the assertion.
    #
    # This test installs WITHOUT `startnow` and fires no logon. If a service is
    # answering below, the installer started it — which is the property under
    # test: a finished installation must leave a running service, not one
    # waiting for the user's next Windows session.
    #
    # An earlier version of this script started the service itself and called
    # the resulting green a pass. It was measuring its own workaround: the
    # product left the service stopped unless the user ticked a checkbox whose
    # real subject was "open my browser".
    check("no browser was opened by the mandatory service start",
          not browser_processes(),
          "; ".join(browser_processes()) or "none")

    # WAIT FOR THE SERVICE TO ANSWER AT ALL before asking what it is doing.
    #
    # `health()` returns None when nothing is listening, and reading that as
    # "no migration is pending" is the same "could not determine == nothing to
    # do" collapse this project keeps having to fix. It passed vacuously here:
    # the service was still starting, the check reported "no migration was
    # pending", and ten seconds later /health said `migrating, 0/2`.
    current = None
    service_deadline = time.monotonic() + 300
    while time.monotonic() < service_deadline:
        current = health(tries=1)
        if current:
            break
        time.sleep(5)
    # Report what was FOUND, not the failure text, when it passes. A PASS whose
    # detail reads "nothing responded within 300s" is worse than no detail: the
    # log then contradicts its own verdict and a reader has to guess which half
    # to believe.
    if not check("the service answers after the upgrade", bool(current),
                 f"status={current.get('status')} version={current.get('version')}"
                 if current else
                 "nothing responded within 300s — the state below cannot be read"):
        current = {}
    current = current or {}
    was_migrating = current.get("status") == "migrating"
    if was_migrating:
        early = run([str(install / "rag.exe"), "selfcheck",
                     "--expect-version", args.version], timeout=600)
        check("selfcheck REFUSES to pass while the rebuild is outstanding",
              early.returncode != 0,
              "exit 0 — it claimed readiness mid-rebuild")

    # Bounded, and explicit about which way it ran out. A wait that simply
    # expires tells you nothing about whether the rebuild was progressing
    # slowly or not progressing at all.
    deadline = time.monotonic() + 900
    last, stalled_at, progressed = current, None, False
    while time.monotonic() < deadline:
        last = health(tries=3)
        # A service that stops answering mid-rebuild is a finding, not a
        # completion: treating silence as "finished" is how the check above
        # passed while the rebuild had not started.
        if last is None:
            print("    service stopped answering during the rebuild", flush=True)
            time.sleep(5)
            continue
        if last.get("status") != "migrating":
            break
        state = last.get("migration") or {}
        done, total = state.get("done"), state.get("total")
        if stalled_at is not None and done != stalled_at:
            progressed = True
        stalled_at = done
        print(f"    rebuilding after migration: {done}/{total}", flush=True)
        time.sleep(10)

    migration = (last or {}).get("migration") or {}
    # Explicitly requires an ANSWER saying it is done — not the absence of one.
    completed = bool(last) and last.get("status") != "migrating"
    check("the post-migration rebuild completed",
          completed,
          json.dumps(migration) if migration else "no migration was pending")
    if not completed:
        check("the rebuild was making progress when the wait expired",
              progressed, f"stalled at done={stalled_at} for the full 900s")

    # Every captured unit, not merely "the plan finished".
    final = health() or {}
    residual = final.get("migration")
    check("no unit was left pending or failed", residual is None,
          json.dumps(residual) if residual else "the plan completed")

    # UNCONDITIONAL. These were gated on `was_migrating`, i.e. on happening to
    # poll while the rebuild was still running — and two tiny seeded projects
    # rebuild in seconds, so the gate usually lost the race and every assertion
    # below was silently skipped. The seeded config guarantees a migration
    # occurred; whether the test observed it mid-flight is luck, and luck is not
    # a precondition for checking the result.
    status = api_status()
    if status:
        check("the final layout is per-project",
              status.get("collection_strategy") == "per_project",
              str(status.get("collection_strategy")))
        collections = {c.get("name"): c.get("points", 0)
                       for c in (status.get("collections") or [])}
        check("every legacy project has its own populated collection",
              len(collections) >= len(legacy_projects)
              and all(v > 0 for v in collections.values()),
              json.dumps(collections))
        check("the old shared collection was retired",
              "markdown_kb" not in collections, json.dumps(sorted(collections)))

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

    # --- 6. the LEGACY CONFIG was migrated, not merely preserved ----------
    #
    # The installer proving the machine moved is only half the release. The
    # other half is that the machine moved to the intended architecture: a
    # legacy config with no storage keys must come out of this at schema v3,
    # on the recommended engine and layout, with every project intact.
    try:
        import tomllib
    except ModuleNotFoundError:                       # pragma: no cover
        import tomli as tomllib                       # type: ignore[no-redef]

    migrated = {}
    if config.is_file():
        try:
            migrated = tomllib.loads(config.read_text(encoding="utf-8"))
        except Exception as exc:                      # noqa: BLE001
            check("the migrated config parses", False, str(exc))

    check("the legacy config was migrated to schema v3",
          migrated.get("version") == 3, f"version={migrated.get('version')}")
    check("the migration adopted an explicit engine",
          bool(migrated.get("storage_backend")),
          str(migrated.get("storage_backend")))
    check("the migration adopted the per-project layout",
          migrated.get("collection_strategy") == "per_project",
          str(migrated.get("collection_strategy")))
    check("the adoption was recorded as a legacy default, not a user choice",
          "storage_backend" in ((migrated.get("migration") or {}).get("adopted") or []),
          json.dumps(migrated.get("migration")))
    surviving = [p.get("id") for p in (migrated.get("projects") or [])]
    check("every legacy project survived the migration",
          sorted(surviving) == sorted(p.name for p in legacy_projects),
          str(surviving))

    # `/health` must not claim readiness while the rebuild is outstanding.
    state = (after_health or {}).get("status")
    migration_block = (after_health or {}).get("migration")
    check("the service does not claim readiness while rebuilding",
          state in ("ready", "migrating"),
          f"status={state}")
    if state == "migrating":
        check("the rebuild reports progress and a retry path",
              bool(migration_block) and "retry" in (migration_block or {}),
              json.dumps(migration_block))

    check("user sentinel file survived", sentinel.is_file(), str(sentinel))
    check("the configuration still exists after migration", config.is_file(),
          str(config))

    failed = [r for r in results if not r[1]]
    print(f"\n  {len(results) - len(failed)} passed, {len(failed)} failed", flush=True)
    print(json.dumps({"passed": len(results) - len(failed), "failed": len(failed)}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
