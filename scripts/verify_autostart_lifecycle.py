"""Register autostart for real, trigger it, and prove it is removed cleanly.

A reboot cannot be forced on someone's working machine, so V03 ("reboot ->
service autostarts") is usually declared manual and left unrun forever. But a
reboot only supplies one thing the rest of the row does not: the OS firing the
trigger. Everything else — that the registration is created, that it is bound to
*this* user's logon, that the command it records actually starts, that removal
leaves nothing — can be verified now by registering a task and running it.

So this does the real thing against the real Task Scheduler, under a
deliberately distinct name that could never collide with the product's own
registrations, and removes it in a `finally` so an interruption cannot leave a
stray task behind. The user's `RAGTools Watchdog` task and Startup-folder
entries are never touched.

Both concerns are exercised, because they are separate registrations with
separate lifetimes: the service (V03) and the tray (V04).

What remains unproven afterwards, honestly: that Windows fires an at-logon
trigger at logon. That is Windows' behaviour, not this product's.

This is also the probe that found the defect it now guards. Registration used to
go through `schtasks /sc onlogon`, which builds a logon trigger with no
`<UserId>` — "at logon of ANY user" — and the scheduler accepts that only from
an administrator. On a standard account the product could not register its own
autostart at all; the failure was read as "this machine won't let us test it"
rather than "this build cannot start itself for a normal user".
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ragtools.platform import KIND_SERVICE, KIND_TRAY, AutostartSpec, current_platform
from ragtools.platform.base import default_runner

#: Deliberately unlike anything the product registers, so a failure here can
#: never be confused with — or damage — a real registration.
SANDBOX_PREFIX = r"\RAGToolsVerify"

#: (kind, leaf, matrix row) — the two autostart concerns, verified separately.
CONCERNS = ((KIND_SERVICE, "Service", "V03"), (KIND_TRAY, "Tray", "V04"))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    print(f"  [{status:^4}] {name}" + (f" — {detail}" if detail else ""))


def query(task: str):
    return default_runner(["schtasks", "/query", "/tn", task])


def root_folders() -> set[str]:
    """Folder names directly under the Task Scheduler library root."""
    result = default_runner([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "$s = New-Object -ComObject Schedule.Service; $s.Connect(); "
        "$s.GetFolder('\\').GetFolders(0) | ForEach-Object { $_.Name }",
    ])
    return {line.strip() for line in (result.stdout or "").splitlines() if line.strip()}


def exercise(adapter, kind: str, leaf: str, row: str) -> bool:
    """Register one concern, prove it works, and report. Returns registered."""
    task = SANDBOX_PREFIX + "\\" + leaf
    marker = Path(tempfile.gettempdir()) / f"ragtools-autostart-{leaf.lower()}.txt"
    marker.unlink(missing_ok=True)
    label = f"{row} {leaf.lower()}"

    print(f"\n{row} — {leaf} autostart")

    # A task whose action is observable: it writes a marker file. That proves
    # the SCHEDULER ran the command we registered, which is the part a reboot
    # would otherwise be needed to observe.
    spec = AutostartSpec(
        name=task, kind=kind,
        argv=["cmd.exe", "/c", f"echo started > {marker}"],
        description="ragtools release verification probe",
        delay_seconds=10 if kind == KIND_TRAY else 0,
    )

    try:
        registration = adapter.install_autostart(spec)
    except Exception as exc:  # noqa: BLE001
        check(f"{label}: registration is created", FAIL, str(exc)[:90])
        return False
    check(f"{label}: registration is created", PASS, registration.mechanism)
    check(f"{label}: the scheduler reports the task",
          PASS if query(task).ok else FAIL)

    detail = default_runner(["schtasks", "/query", "/tn", task, "/v", "/fo", "list"])
    text = (detail.stdout or "").lower()
    check(f"{label}: the trigger is at-logon", PASS if "logon" in text else FAIL,
          "At logon time" if "logon" in text else "trigger not recognised as logon")
    check(f"{label}: it runs without elevation",
          PASS if "highest" not in text else FAIL,
          "a per-user product must not require admin")
    check(f"{label}: no interpreted shim is involved",
          PASS if not any(s in text for s in (".vbs", "wscript")) else FAIL,
          "command registered directly")

    # --- RUN IT: the part a reboot would otherwise be needed for ---------
    run = default_runner(["schtasks", "/run", "/tn", task])
    check(f"{label}: the scheduler accepts a run request", PASS if run.ok else FAIL,
          (run.stderr or run.stdout).strip()[:70])

    appeared = False
    for _ in range(20):
        if marker.exists():
            appeared = True
            break
        time.sleep(0.5)
    check(f"{label}: the registered command actually executed",
          PASS if appeared else FAIL,
          "marker written by the scheduled action" if appeared
          else "the task ran but the recorded command did not")
    marker.unlink(missing_ok=True)
    return True


def main() -> int:
    if current_platform() != "windows":
        print("autostart lifecycle probe is Windows-specific")
        return 0

    from ragtools.platform.windows import WindowsAdapter

    # Isolation comes from the PREFIX, not from a name the adapter would
    # discard. With this, the probe cannot reach the real registrations.
    adapter = WindowsAdapter(task_prefix=SANDBOX_PREFIX)

    print(f"Registering under {SANDBOX_PREFIX} (sandbox; removed at the end)")
    check("V04: this host has a desktop session",
          PASS if adapter.has_desktop_session() else SKIP,
          "a tray registration is only meaningful where a tray can exist")

    registered: list[str] = []
    try:
        for kind, leaf, row in CONCERNS:
            if exercise(adapter, kind, leaf, row):
                registered.append(SANDBOX_PREFIX + "\\" + leaf)
    finally:
        # --- removal, always ---------------------------------------------
        print("\nremoval")
        for task in registered:
            removed = adapter.remove_autostart(task)
            gone = not query(task).ok
            check(f"{task}: removal deletes the registration",
                  PASS if gone else FAIL, f"{len(removed)} removed")
            check(f"{task}: removing again is a no-op",
                  PASS if adapter.remove_autostart(task) == [] else FAIL)
        if registered:
            # The folder outlives the tasks inside it, so "the task is gone" is
            # not the same claim as "nothing was left behind".
            check("the task FOLDER is pruned once its last task goes",
                  PASS if SANDBOX_PREFIX.strip("\\") not in root_folders() else FAIL,
                  "an empty RAGTools node in the scheduler tree is still residue")

    failed = [r for r in results if r[1] == FAIL]
    passed = [r for r in results if r[1] == PASS]
    print(f"\n  {len(passed)} passed, {len(failed)} failed")
    print(json.dumps({"passed": len(passed), "failed": len(failed)}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
