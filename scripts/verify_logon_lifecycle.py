"""Fire the registered at-logon tasks the way Windows does, and watch what starts.

Gates 3 and 4. Two rows have been MANUAL since the beginning, for opposite
reasons:

**The logon trigger.** A hosted runner never logs a user in, so an `onlogon`
task is registered and never fires. `schtasks /run` is the closest honest
substitute: it starts the task through Task Scheduler, as the registered user,
with the registered action — which is the part that decides whether a console
window appears. What it does NOT reproduce is the trigger itself firing, and
that limitation is printed rather than glossed.

**The non-admin account.** Every hosted runner is an administrator, and that is
precisely what hid the original `/sc onlogon` defect: the form that omits the
trigger's UserId succeeds for an admin and is refused for everyone else. So this
reports the privilege it ran under instead of implying coverage it does not
have — a green tick from an admin account is not evidence about a standard one.

The console-window check is the one this exists for. `ragw.exe` is GUI-subsystem
so Windows allocates no console for it; `rag.exe` is not. A task pointing at the
wrong one gives every user a terminal window at every login, and no unit test
can see it because the subsystem is decided by PyInstaller and the window by the
OS.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


def ps(script: str, timeout: int = 120) -> str:
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                           "-Command", script],
                          capture_output=True, text=True, timeout=timeout)
    return (proc.stdout or "").strip()


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def console_windows_for(image: str) -> int:
    """How many console host processes belong to this image's tree.

    `conhost.exe` is what Windows attaches to a console-subsystem process. Its
    presence as a child is the observable form of "a terminal window appeared".
    """
    return int(ps(
        "$n = 0; "
        f"Get-CimInstance Win32_Process -Filter \"Name='{image}'\" | "
        "ForEach-Object { $pid0 = $_.ProcessId; "
        "$n += @(Get-CimInstance Win32_Process -Filter "
        "\"Name='conhost.exe' AND ParentProcessId=$pid0\").Count }; $n") or 0)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-path", default="\\RAGTools",
                        help="the scheduled-task folder this product owns")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("Windows only.")
        return 0

    admin = is_admin()
    print(f"privilege context: {'ADMINISTRATOR' if admin else 'standard user'}")
    if admin:
        print("  NOTE: a hosted runner is always an administrator. `/sc onlogon`\n"
              "  without a UserId succeeds here and is REFUSED for a standard\n"
              "  account — which is exactly what hid the original defect. This\n"
              "  run is therefore NOT evidence about a non-admin machine.\n")

    tasks = ps("Get-ScheduledTask -TaskPath '" + args.task_path +
               "\\*' -ErrorAction SilentlyContinue | "
               "ForEach-Object { \"$($_.TaskName)|$($_.Actions.Execute)\" }")
    registered = dict(line.split("|", 1) for line in tasks.splitlines() if "|" in line)
    if not check("both autostart tasks are registered",
                 len(registered) >= 2, json.dumps(registered)):
        return 1

    for name, execute in registered.items():
        check(f"{name} targets the windowless executable",
              execute.lower().endswith("ragw.exe"), execute)

    before = console_windows_for("ragw.exe") + console_windows_for("rag.exe")

    for name in registered:
        ps(f"Start-ScheduledTask -TaskPath '{args.task_path}\\' -TaskName '{name}'")
        print(f"    started {name} through Task Scheduler", flush=True)
    time.sleep(12)

    running = ps("Get-CimInstance Win32_Process -Filter \"Name='ragw.exe'\" | "
                 "Measure-Object | ForEach-Object { $_.Count }")
    check("the scheduler actually started the product", int(running or 0) > 0,
          f"{running} ragw.exe process(es)")

    after = console_windows_for("ragw.exe") + console_windows_for("rag.exe")
    check("no console window was created at logon", after <= before,
          f"conhost children before={before} after={after}")

    # Stop what we started, so the runner is left as we found it.
    for name in registered:
        ps(f"Stop-ScheduledTask -TaskPath '{args.task_path}\\' -TaskName '{name}'")

    failed = [r for r in results if not r[1]]
    print(f"\n  {len(results) - len(failed)} passed, {len(failed)} failed")
    print("  STILL NOT COVERED: the OS firing an at-logon trigger at a real "
          "logon, and registration under a standard (non-admin) account.")
    print(json.dumps({"admin": admin, "passed": len(results) - len(failed),
                      "failed": len(failed)}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
