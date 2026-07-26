"""Register autostart for real, trigger it, and prove it is removed cleanly.

A reboot cannot be forced on someone's working machine, so V03 ("reboot ->
service autostarts") is usually declared manual and left unrun forever. But a
reboot only supplies one thing the rest of the row does not: the OS firing the
trigger. Everything else — that the registration is created, that the command it
records actually starts the service, that removal leaves nothing — can be
verified now by registering a task and running it.

So this does the real thing against the real Task Scheduler, under a
deliberately distinct name that could never collide with the product's own
registrations, and removes it in a `finally` so an interruption cannot leave a
stray task behind. The user's `RAGTools Service`, `RAGTools Watchdog` and
Startup-folder entries are never touched.

What remains unproven afterwards, honestly: that Windows fires an at-logon
trigger at logon. That is Windows' behaviour, not this product's.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ragtools.platform import KIND_SERVICE, AutostartSpec, current_platform
from ragtools.platform.base import default_runner

#: Deliberately unlike anything the product registers, so a failure here can
#: never be confused with — or damage — a real registration.
SANDBOX_PREFIX = r"\RAGToolsVerify"
SANDBOX_TASK = SANDBOX_PREFIX + r"\Service"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    print(f"  [{status:^4}] {name}" + (f" — {detail}" if detail else ""))


def query(task: str):
    return default_runner(["schtasks", "/query", "/tn", task])


def main() -> int:
    if current_platform() != "windows":
        print("autostart lifecycle probe is Windows-specific")
        return 0

    from ragtools.platform.windows import WindowsAdapter

    marker = Path(tempfile.gettempdir()) / "ragtools-autostart-probe.txt"
    marker.unlink(missing_ok=True)

    # Isolation comes from the PREFIX, not from a name the adapter would
    # discard. With this, the probe cannot reach the real registrations.
    adapter = WindowsAdapter(task_prefix=SANDBOX_PREFIX)
    # A task whose action is observable: it writes a marker file. That proves
    # the SCHEDULER ran the command we registered, which is the part a reboot
    # would otherwise be needed to observe.
    spec = AutostartSpec(
        name=SANDBOX_TASK, kind=KIND_SERVICE,
        argv=["cmd.exe", "/c", f"echo started > {marker}"],
        description="ragtools release verification probe",
    )

    registered = False
    try:
        print(f"Registering {SANDBOX_TASK} (sandbox; removed at the end)\n")

        # --- create -------------------------------------------------------
        try:
            registration = adapter.install_autostart(
                AutostartSpec(**{**spec.__dict__, "kind": KIND_SERVICE}))
            registered = True
            check("autostart registration is created", PASS, registration.mechanism)
        except Exception as exc:  # noqa: BLE001
            check("autostart registration is created", FAIL, str(exc)[:90])
            return 1

        # --- the scheduler can see it ------------------------------------
        check("the scheduler reports the task", PASS if query(SANDBOX_TASK).ok else FAIL)

        # --- it is registered at LOGON, not on a timer -------------------
        detail = default_runner(["schtasks", "/query", "/tn", SANDBOX_TASK, "/v", "/fo", "list"])
        text = detail.stdout or ""
        at_logon = "logon" in text.lower()
        check("the trigger is at-logon", PASS if at_logon else FAIL,
              "At logon time" if at_logon else "trigger not recognised as logon")
        check("it runs without elevation", PASS if "highest" not in text.lower() else FAIL,
              "a per-user product must not require admin")

        # --- RUN IT: the part a reboot would otherwise be needed for ------
        run = default_runner(["schtasks", "/run", "/tn", SANDBOX_TASK])
        check("the scheduler accepts a run request", PASS if run.ok else FAIL,
              (run.stderr or run.stdout).strip()[:70])

        appeared = False
        for _ in range(20):
            if marker.exists():
                appeared = True
                break
            time.sleep(0.5)
        check("the registered command actually executed", PASS if appeared else FAIL,
              "marker written by the scheduled action" if appeared
              else "the task ran but the recorded command did not")

        # --- no console window -------------------------------------------
        check("no interpreted shim is involved", PASS
              if not any(s in text.lower() for s in (".vbs", "wscript")) else FAIL,
              "command registered directly")

    finally:
        # --- removal, always ---------------------------------------------
        if registered:
            removed = adapter.remove_autostart(SANDBOX_TASK)
            gone = not query(SANDBOX_TASK).ok
            check("removal deletes the registration", PASS if gone else FAIL,
                  f"{len(removed)} removed")
            check("removing again is a no-op",
                  PASS if adapter.remove_autostart(SANDBOX_TASK) == [] else FAIL)
            check("zero residue", PASS if gone else FAIL,
                  "the scheduler no longer knows this task")
        marker.unlink(missing_ok=True)

    failed = [r for r in results if r[1] == FAIL]
    print(f"\n  {len(results) - len(failed)} passed, {len(failed)} failed")
    print(json.dumps({"passed": len(results) - len(failed), "failed": len(failed)}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
