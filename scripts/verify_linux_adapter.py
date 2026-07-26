"""Execute the Linux adapter on real Linux. Standard library only.

The suite exercises this adapter against temp directories on any host, which
proves the logic and proves nothing about systemd. This script closes that gap
where a real Linux kernel is available: it renders a unit and hands it to
``systemd-analyze verify``, which parses it exactly as systemd would, and it
exercises the process primitives against real PIDs.

Everything is confined to a temporary ``XDG_CONFIG_HOME``. Nothing is written to
the user's real unit directory, nothing is enabled in their session, and no
service is started.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ragtools.platform import KIND_SERVICE, KIND_TRAY, AutostartSpec, current_platform
from ragtools.platform.linux import LinuxAdapter

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    print(f"  [{status:^4}] {name}" + (f" — {detail}" if detail else ""))


def have(tool: str) -> bool:
    return subprocess.run(["sh", "-c", f"command -v {tool}"],
                          capture_output=True).returncode == 0


def main() -> int:
    print(f"Linux adapter verification on {os.uname().sysname} "
          f"{os.uname().release}\n")

    # --- the seam resolves to the right adapter here ---------------------
    check("platform resolves to linux", PASS if current_platform() == "linux" else FAIL,
          current_platform())

    home = Path(tempfile.mkdtemp(prefix="ragtools-verify-"))
    config_home = home / "config"
    data_home = home / "data"
    adapter = LinuxAdapter(home=home, xdg_config_home=config_home,
                           xdg_data_home=data_home)

    # --- XDG paths --------------------------------------------------------
    check("app_dir honours XDG_DATA_HOME",
          PASS if adapter.app_dir() == data_home / "RAGTools" else FAIL,
          str(adapter.app_dir()))
    check("dev_dir never collides with app_dir",
          PASS if adapter.dev_dir() != adapter.app_dir() else FAIL,
          str(adapter.dev_dir()))

    real = LinuxAdapter(home=Path.home())
    expected = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share"))
    check("app_dir matches the real XDG environment",
          PASS if real.app_dir() == expected / "RAGTools" else FAIL,
          str(real.app_dir()))

    # --- unit rendering, validated BY SYSTEMD -----------------------------
    spec = AutostartSpec(
        name="RAGTools Service", kind=KIND_SERVICE,
        argv=[str(Path.home() / ".local/bin/rag"), "service", "run",
              "--host", "127.0.0.1", "--port", "21420"],
        description="RAG Tools — local knowledge-base service",
        environment={"RAG_PROFILE": "installed"},
    )
    unit_text = adapter.render_unit(spec)
    unit_dir = adapter.unit_dir
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "ragtools.service"
    unit_path.write_text(unit_text, encoding="utf-8")

    if have("systemd-analyze"):
        proc = subprocess.run(["systemd-analyze", "verify", str(unit_path)],
                              capture_output=True, text=True)
        # `verify` reports unresolved ExecStart paths as warnings on a machine
        # where the binary is not installed; a PARSE failure is what matters.
        # An absent ExecStart binary is an environment fact, not a parse
        # failure — systemd reports it whenever the product is not installed on
        # the verifying host, which is the normal case in CI.
        noise = ("does not exist", "not found", "No such file or directory",
                 "is not executable", "Failed to prepare", "cannot be resolved",
                 "Unit not found")
        fatal = [
            line for line in (proc.stderr or "").splitlines()
            if line.strip() and not any(n in line for n in noise)
        ]
        check("systemd parses the generated unit", PASS if not fatal else FAIL,
              "; ".join(fatal[:2]) if fatal else "systemd-analyze verify clean")
    else:
        check("systemd parses the generated unit", SKIP, "systemd-analyze absent")

    for key in ("Restart=on-failure", "RestartSec=5", "StartLimitBurst=5",
                "WantedBy=default.target", 'Environment="RAG_PROFILE=installed"'):
        check(f"unit carries {key}", PASS if key in unit_text else FAIL)

    # --- enumeration ------------------------------------------------------
    found = adapter.find_autostart(KIND_SERVICE)
    check("find_autostart sees the unit on disk",
          PASS if any(r.name == "ragtools.service" for r in found) else FAIL,
          f"{len(found)} registration(s)")
    check("ExecStart is read back from the file",
          PASS if found and "service run" in found[0].target else FAIL,
          found[0].target if found else "")

    desktop = adapter.autostart_dir
    desktop.mkdir(parents=True, exist_ok=True)
    (desktop / "ragtools-tray.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
    tray = adapter.find_autostart(KIND_TRAY)
    check("XDG autostart entry is enumerated for the tray",
          PASS if any(r.mechanism == "xdg-autostart" for r in tray) else FAIL,
          f"{len(tray)} registration(s)")

    # --- headless / desktop detection -------------------------------------
    has_session = adapter.has_desktop_session()
    check("desktop-session detection runs",
          PASS, f"{'session' if has_session else 'headless'} "
                f"(DISPLAY={os.environ.get('DISPLAY','')!r} "
                f"WAYLAND={os.environ.get('WAYLAND_DISPLAY','')!r})")

    # --- lingering (the headless correctness detail) ----------------------
    if have("loginctl"):
        check("linger state is readable", PASS,
              f"linger_enabled={adapter.linger_enabled()}")
    else:
        check("linger state is readable", SKIP, "loginctl absent")

    check("systemctl present for autostart support",
          PASS if adapter.supports_autostart() else FAIL,
          "systemctl found" if adapter.supports_autostart() else "systemctl missing")

    # --- process primitives, against real PIDs ----------------------------
    pid = adapter.spawn_detached(["sleep", "30"])
    check("spawn_detached returns a live pid",
          PASS if pid > 0 and adapter.pid_alive(pid) else FAIL, f"pid={pid}")
    check("pid_alive is False for an impossible pid",
          PASS if not adapter.pid_alive(4_000_000) else FAIL)
    check("terminate stops the process",
          PASS if adapter.terminate(pid, force=True) else FAIL)
    import time

    time.sleep(0.3)
    check("the terminated process is gone",
          PASS if not adapter.pid_alive(pid) else FAIL)

    # --- removal is idempotent -------------------------------------------
    removed = adapter.remove_autostart("ragtools.service")
    check("remove_autostart deletes the unit",
          PASS if removed and not unit_path.exists() else FAIL,
          f"{len(removed)} removed")
    check("removing again is a no-op",
          PASS if adapter.remove_autostart("ragtools.service") == [] else FAIL)

    import shutil

    shutil.rmtree(home, ignore_errors=True)

    failed = [r for r in results if r[1] == FAIL]
    skipped = [r for r in results if r[1] == SKIP]
    print(f"\n  {len(results) - len(failed) - len(skipped)} passed, "
          f"{len(failed)} failed, {len(skipped)} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
