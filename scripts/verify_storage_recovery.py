"""Kill the vector store, prove health says so, prove the service recovers.

The failure this exists for is specific: a service that answers `/health` with
`ready` while Qdrant is unreachable. Everything downstream then reports success
against a store that is not there — which is how an index divergence goes
unnoticed for weeks.

It is destructive by nature: it kills a running storage engine. So it refuses to
touch the installed profile's ports and demands an explicit target, and it
restores the service before returning.

Usage:
    python scripts/verify_storage_recovery.py --url http://127.0.0.1:21455 \\
        --storage-port 21510 --restart "path\\to\\start.ps1"
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []

#: Ports belonging to an INSTALLED profile. Refused outright — a verification
#: script that can kill the user's working service is not a verification script.
PROTECTED_PORTS = {21420, 21422}


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def health(url: str, timeout: float = 25.0) -> dict:
    try:
        with urllib.request.urlopen(url + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        return {"_unreachable": str(exc)}


def listener_pid(port: int) -> int | None:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-NetTCPConnection -State Listen -LocalPort {port} "
         f"-ErrorAction SilentlyContinue).OwningProcess"],
        capture_output=True, text=True, timeout=30)
    raw = (proc.stdout or "").strip().splitlines()
    return int(raw[0]) if raw and raw[0].strip().isdigit() else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--storage-port", type=int, required=True)
    parser.add_argument("--restart", default="",
                        help="script that restarts the service after the kill")
    args = parser.parse_args(argv)

    service_port = int(args.url.rsplit(":", 1)[-1].split("/")[0])
    if service_port in PROTECTED_PORTS or args.storage_port in PROTECTED_PORTS:
        print(f"refusing: {service_port}/{args.storage_port} belongs to an "
              "installed profile. This test kills the storage engine.")
        return 2

    # --- baseline ---------------------------------------------------------
    before = health(args.url)
    check("the service is healthy to begin with",
          before.get("status") == "ready" and before.get("storage_reachable") is True,
          f"backend={before.get('storage_backend')}")
    points_before = 0
    try:
        with urllib.request.urlopen(args.url + "/api/status", timeout=60) as r:
            points_before = json.loads(r.read().decode()).get("points_count", 0)
    except Exception:  # noqa: BLE001
        pass

    pid = listener_pid(args.storage_port)
    check("the storage engine is running", pid is not None, f"pid={pid}")
    if pid is None:
        return 1

    # --- kill -------------------------------------------------------------
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Stop-Process -Id {pid} -Force"],
                   capture_output=True, timeout=30)
    time.sleep(8)

    # --- the whole point --------------------------------------------------
    during = health(args.url)
    honest = (during.get("storage_reachable") is False
              and during.get("degraded") is True
              and "storage_unreachable" in (during.get("issues") or []))
    check("health reports the store as unreachable, not green", honest,
          f"reachable={during.get('storage_reachable')} "
          f"degraded={during.get('degraded')} issues={during.get('issues')}")
    check("the failure names a cause", bool(during.get("storage_error")),
          (during.get("storage_error") or "")[:70])

    # --- recover ----------------------------------------------------------
    if args.restart:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Get-NetTCPConnection -State Listen -LocalPort {service_port} "
                        "-ErrorAction SilentlyContinue | ForEach-Object "
                        "{ Stop-Process -Id $_.OwningProcess -Force }"],
                       capture_output=True, timeout=30)
        time.sleep(4)
        # No pipes. The launcher spawns a DETACHED service, and a captured
        # pipe stays open as long as that child holds the handle — so
        # `capture_output=True` here waits for the service to exit, which is
        # the one thing it must not do.
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", args.restart],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=120)

        recovered = {}
        for _ in range(30):
            recovered = health(args.url, timeout=5)
            if recovered.get("storage_reachable"):
                break
            time.sleep(4)
        check("the service re-supervises the store on restart",
              recovered.get("storage_reachable") is True,
              f"backend={recovered.get('storage_backend')}")

        new_pid = listener_pid(args.storage_port)
        check("a NEW storage process is listening",
              new_pid is not None and new_pid != pid, f"{pid} -> {new_pid}")

        points_after = 0
        try:
            with urllib.request.urlopen(args.url + "/api/status", timeout=120) as r:
                points_after = json.loads(r.read().decode()).get("points_count", 0)
        except Exception:  # noqa: BLE001
            pass
        check("the index survived the kill", points_after >= points_before > 0,
              f"{points_before:,} -> {points_after:,} points")
    else:
        print("  (no --restart given; recovery not exercised)")

    failed = [r for r in results if r[1] == FAIL]
    print(f"\n  {len(results) - len(failed)} passed, {len(failed)} failed")
    print(json.dumps({"passed": len(results) - len(failed), "failed": len(failed)}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
