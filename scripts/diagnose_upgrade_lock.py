"""Prove who is holding an installed file when a packaged upgrade is cancelled.

WP-R01. On GitHub-hosted `windows-latest`, job 91273392455, the 3.3.0 -> 3.5.0
leg of the packaged upgrade check failed like this:

    >>> installing v3.3.0: RAGTools-Setup-3.3.0.exe
        exit=0
      [PASS] previous release is serving - managed engine pid 1264, per_project
      [PASS] previous release has live processes holding its binaries - 3
    >>> installing v3.5.0 (over v3.3.0): RAGTools-Setup-3.5.0.exe
        exit=5
    --- Inno log for v3.5.0 (over v3.3.0) (last 120 of 1 lines) ---
    --- end Inno log ---
      [FAIL] the upgrade installer exited 0 - exit 5
      [FAIL] rag.exe reports 3.5.0 - [PYI-2072:ERROR] Failed to load Python DLL
             '...\\RAGTools\\_internal\\python312.dll'.
      [FAIL] uninstall registry entry updated - registry reads 3.3.0
      [FAIL] the service answers after the upgrade - nothing responded in 300s

The 2.7.0 and 3.0.1 legs of the SAME run passed. Inno Setup exit code 5 means
"the user clicked Cancel during installation, or chose Abort at an
Abort-Retry-Ignore box", and `/SUPPRESSMSGBOXES` answers Abort-Retry-Ignore with
Abort. So the installer did not crash: something asked it a question it was
configured to answer with "give up", after it had already begun replacing files.
The one-line Inno log is not noise either - a log Setup writes incrementally,
that contains a single line, says Setup stopped writing it, and WHEN it stopped
is evidence.

Everything above is a symptom. The question underneath is ownership: at the
instant the installer tried to replace `_internal\\python312.dll`, which process
still had it open, what was that process, and had anything restarted it after
the installer's own shutdown phase ran. `verify_upgrade_install.py` proves the
upgrade broke; it cannot say who was holding the file, because a process name is
not an answer - the Restart Manager is.

So this harness collects, in order:

  A. a genuine previous installation (the published installer, silently
     installed and started - never a synthesised layout)
  B. the full pre-upgrade truth: every process whose image OR loaded modules lie
     under the install directory, the Restart Manager's per-file owner list,
     the scheduled tasks pointing into it, `/health` verbatim, a SHA-256 of
     every installed file, the uninstall registry entry, and digests of the
     user's own data
  C. the candidate installer run under a 250 ms sampler and a 2 s Restart
     Manager probe, so the first destructive write and any mid-install process
     appearance are timestamped against the installer's own start
  D. the same capture again, plus the diff: what changed, what is missing, what
     still runs, whether a rollback left a byte-consistent previous install, and
     whether the user's data survived

and ends with a machine-readable verdict answering ten named questions, each
carrying the evidence that decided it. A question the collected data cannot
settle is reported as `undetermined` WITH THE REASON. That is not a hedge: this
bundle exists to be quoted, and a harness that guesses gets quoted too.

WHY IT REFUSES TO RUN ON A DEVELOPER MACHINE
--------------------------------------------
This is not a simulation. It installs a real published release at the product's
fixed `AppId`, into `%LOCALAPPDATA%\\Programs\\RAGTools`, and then runs a second
installer over it - which force-kills every owned process, deletes `_internal`,
and rewrites the uninstall registry entry. On a developer machine those are not
scratch paths: they are the live installation, its running service, and the
user's indexed data. There is no `--dry-run` that would still measure anything,
because the measurement IS the destruction.

So it refuses unless BOTH hold: `--i-am-a-disposable-runner` was passed
explicitly, and `CI=true` is in the environment. The flag alone is a typo away
from being real; `CI=true` alone is set by shells and editors that are not
disposable at all. Both together are hard to arrive at by accident, and the
refusal happens before anything is downloaded or installed.

Usage:
    python scripts/diagnose_upgrade_lock.py \\
        --installer dist/RAGTools-Setup-3.5.0.exe --version 3.5.0 \\
        --from-version 3.3.0 --out upgrade-work/lock-diagnosis-3.3.0.json \\
        --i-am-a-disposable-runner

Exit codes (deliberately NOT the house "1 if any check failed"):
    0  the diagnosis ran to completion and the bundle was written - including
       when the upgrade itself failed, which is the outcome it was built for.
       A red job here would be read as "the harness is broken" and someone would
       weaken it.
    2  refused: this does not look like a disposable runner. Nothing was touched.
    3  the diagnosis could not be performed at all (no installer, wrong platform,
       previous release would not install).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- reuse, not redefinition -------------------------------------------------
#
# REPO / INSTALL_DIR / DATA_DIR / UNINSTALL_KEY are IMPORTED from the harness
# that already owns them. A second definition that drifts by one character
# produces a forensic bundle describing a directory the product does not use,
# and nothing in the output would look wrong. The installer invocation
# (`install_silently`) is imported for the same reason: "run it exactly as the
# existing harness does" is only true if it is literally the same call.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_upgrade_install import (  # noqa: E402
    DATA_DIR,
    INSTALL_DIR,
    REPO,
    UNINSTALL_KEY,
    binary_version,
    download_previous,
    health,
    install_silently,
    installed_dir,
    registry_version,
    run,
)

_WINDOWS = sys.platform == "win32"

# psutil is a declared dependency as of 3.3.0 - and the reason it is declared is
# that through 3.2.0 it was not, so whether the engine-ownership proofs existed
# depended on whether the build venv happened to contain it. The same trap
# applies here, so its absence is RECORDED rather than silently degrading the
# process capture to nothing.
try:
    import psutil
except ImportError as _exc:                                  # pragma: no cover
    psutil = None                                            # type: ignore[assignment]
    PSUTIL_UNAVAILABLE: str | None = f"psutil import failed: {_exc}"
else:
    PSUTIL_UNAVAILABLE = None

# ctypes.wintypes does not import on Linux (its DWORD/FILETIME definitions are
# Windows-only), and this file has to at least IMPORT on all three platforms
# because CI lints on all three.
if _WINDOWS:
    import ctypes
    import winreg
    from ctypes import wintypes
else:                                                        # pragma: no cover
    ctypes = None                                            # type: ignore[assignment]
    winreg = None                                            # type: ignore[assignment]
    wintypes = None                                          # type: ignore[assignment]


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """House-style observation line. Mirrors `verify_upgrade_install.check`.

    These are OBSERVATIONS, not gates - see the exit codes in the module
    docstring. A `[FAIL]` here is frequently the finding.
    """
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""),
          flush=True)
    return ok


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _normcase(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _under(path: str | None, root_normcase: str) -> bool:
    """Is `path` inside `root_normcase` (or the root itself)?"""
    if not path:
        return False
    try:
        candidate = _normcase(path)
    except (OSError, ValueError):
        return False
    return candidate == root_normcase or candidate.startswith(root_normcase + os.sep)


# =============================================================================
# Windows Restart Manager - the authoritative answer to "who holds this file"
# =============================================================================
#
# Every other way of answering this question is a guess. A process named
# `ragw.exe` is not evidence that `ragw.exe` holds `python312.dll`; `handle.exe`
# is not on a hosted runner and would have to be downloaded and hash-verified;
# and "the installer said DeleteFile failed" names the victim, not the owner.
#
# RM is the mechanism Inno itself uses (`CloseApplications`), it is already on
# the machine, and it answers per FILE. Nothing is downloaded here, so nothing
# needs hash-verifying.

CCH_RM_SESSION_KEY = 32          # sizeof(GUID) * 2, per restartmanager.h
CCH_RM_MAX_APP_NAME = 255
CCH_RM_MAX_SVC_NAME = 63
ERROR_MORE_DATA = 234
RM_MAX_GETLIST_ATTEMPTS = 6

#: RM_APP_TYPE, verbatim from restartmanager.h. Reported by NAME as well as by
#: number, because "4" in a bundle six months from now is not evidence.
RM_APP_TYPES = {
    0: "RmUnknownApp",
    1: "RmMainWindow",
    2: "RmOtherWindow",
    3: "RmService",
    4: "RmExplorer",
    5: "RmConsole",
    1000: "RmCritical",
}

if _WINDOWS:

    class RM_UNIQUE_PROCESS(ctypes.Structure):
        """A pid alone is not an identity - a pid is reused. RM pairs it with
        the process's creation time, which is what makes the reference unique.
        (The same rule `service/engine_ownership.py` applies to the engine: a
        number alone is never trusted.)"""

        _fields_ = [
            ("dwProcessId", wintypes.DWORD),
            ("ProcessStartTime", wintypes.FILETIME),
        ]

    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [
            ("Process", RM_UNIQUE_PROCESS),
            ("strAppName", wintypes.WCHAR * (CCH_RM_MAX_APP_NAME + 1)),
            ("strServiceShortName", wintypes.WCHAR * (CCH_RM_MAX_SVC_NAME + 1)),
            ("ApplicationType", ctypes.c_uint),      # RM_APP_TYPE
            ("AppStatus", wintypes.ULONG),           # RM_APP_STATUS bitmask
            ("TSSessionId", wintypes.DWORD),
            ("bRestartable", wintypes.BOOL),
        ]

    #: 12 + 512 + 128 + 4 + 4 + 4 + 4. Recorded in the bundle: if a future
    #: Windows SDK ever changes the layout, the wrong number here is how the
    #: owner lists silently become garbage, and the bundle should say so itself.
    RM_PROCESS_INFO_SIZE: int | None = ctypes.sizeof(RM_PROCESS_INFO)
    RM_PROCESS_INFO_EXPECTED_SIZE = 668
else:                                                        # pragma: no cover
    RM_PROCESS_INFO_SIZE = None
    RM_PROCESS_INFO_EXPECTED_SIZE = 668


_rstrtmgr_lib: Any = None
_rstrtmgr_error: str | None = None
_rstrtmgr_lock = threading.Lock()


def _rstrtmgr() -> Any:
    """Load and prototype `rstrtmgr.dll` once, thread-safely.

    argtypes are declared for every call. Without them ctypes passes 32-bit ints
    where 64-bit pointers are expected and the struct array arrives truncated -
    which does not raise, it just returns plausible nonsense.
    """
    global _rstrtmgr_lib, _rstrtmgr_error
    if _rstrtmgr_lib is not None or _rstrtmgr_error is not None:
        return _rstrtmgr_lib
    with _rstrtmgr_lock:
        if _rstrtmgr_lib is not None or _rstrtmgr_error is not None:
            return _rstrtmgr_lib
        if not _WINDOWS:
            _rstrtmgr_error = "not Windows"
            return None
        try:
            lib = ctypes.WinDLL("rstrtmgr.dll", use_last_error=True)
        except OSError as exc:
            _rstrtmgr_error = f"could not load rstrtmgr.dll: {exc}"
            return None

        lib.RmStartSession.argtypes = [
            ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
        lib.RmStartSession.restype = wintypes.DWORD

        lib.RmRegisterResources.argtypes = [
            wintypes.DWORD,
            wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
            wintypes.UINT, ctypes.POINTER(RM_UNIQUE_PROCESS),
            wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
        ]
        lib.RmRegisterResources.restype = wintypes.DWORD

        lib.RmGetList.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(RM_PROCESS_INFO), ctypes.POINTER(wintypes.DWORD),
        ]
        lib.RmGetList.restype = wintypes.DWORD

        lib.RmEndSession.argtypes = [wintypes.DWORD]
        lib.RmEndSession.restype = wintypes.DWORD

        _rstrtmgr_lib = lib
        return lib


def _win_error_text(code: int) -> str:
    if not _WINDOWS:                                         # pragma: no cover
        return ""
    try:
        return ctypes.FormatError(code).strip()
    except Exception:                                        # noqa: BLE001
        return ""


def _filetime_to_epoch(ft: Any) -> float | None:
    """FILETIME (100 ns ticks since 1601-01-01 UTC) to a Unix epoch float."""
    raw = (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)
    if raw == 0:
        return None
    return (raw - 116444736000000000) / 1e7


def rm_owners_for_files(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """One Restart Manager session per file, so ownership is ATTRIBUTED.

    A single session registering every file returns the union of owners, which
    answers "is anything holding the install directory" and not "which process
    holds python312.dll" - and the second question is the one WP-R01 asks.

    Never returns an empty owner list to mean "could not ask": a file whose
    session failed carries `owners = None` and the API error that caused it.
    """
    out: list[dict[str, Any]] = []
    lib = _rstrtmgr()
    for path in paths:
        record: dict[str, Any] = {
            "path": str(path),
            "exists": None,
            "owners": None,
            "reboot_reasons": None,
            "error": None,
        }
        # Appended BEFORE it is filled in, and mutated in place afterwards. The
        # first version appended at the end of the loop body, so every path that
        # `continue`d out of the session - a file RM refused to register, a
        # failed RmGetList - vanished from the result entirely: two files in,
        # one record out, and nothing saying which one was dropped. A capture
        # that silently omits the file it could not ask about is exactly the
        # "could not determine == nothing to report" collapse this bundle is
        # supposed to make impossible.
        out.append(record)
        try:
            record["exists"] = path.exists()
        except OSError as exc:
            record["exists"] = None
            record["error"] = f"could not stat: {exc}"
        if lib is None:
            record["error"] = _rstrtmgr_error or "rstrtmgr.dll unavailable"
            continue

        session = wintypes.DWORD(0)
        session_key = ctypes.create_unicode_buffer(CCH_RM_SESSION_KEY + 1)
        rc = lib.RmStartSession(ctypes.byref(session), 0, session_key)
        if rc != 0:
            record["error"] = f"RmStartSession -> {rc} ({_win_error_text(rc)})"
            continue
        try:
            files = (wintypes.LPCWSTR * 1)(str(path))
            rc = lib.RmRegisterResources(session, 1, files, 0, None, 0, None)
            if rc != 0:
                record["error"] = (
                    f"RmRegisterResources -> {rc} ({_win_error_text(rc)})")
                continue

            needed = wintypes.UINT(0)
            have = wintypes.UINT(0)
            reasons = wintypes.DWORD(0)
            buffer: Any = None

            # ERROR_MORE_DATA is the NORMAL path, not an edge case: the first
            # call is a size probe, and a process can appear between the probe
            # and the read - which is precisely what a mid-install sample is
            # trying to catch - so the retry is mandatory. Bounded, because a
            # machine churning processes could otherwise spin here forever.
            for _ in range(RM_MAX_GETLIST_ATTEMPTS):
                rc = lib.RmGetList(session, ctypes.byref(needed),
                                   ctypes.byref(have), buffer,
                                   ctypes.byref(reasons))
                if rc == 0:
                    break
                if rc != ERROR_MORE_DATA:
                    break
                size = max(int(needed.value), 1)
                buffer = (RM_PROCESS_INFO * size)()
                have = wintypes.UINT(size)
            if rc != 0:
                record["error"] = f"RmGetList -> {rc} ({_win_error_text(rc)})"
                continue

            record["reboot_reasons"] = int(reasons.value)
            owners: list[dict[str, Any]] = []
            for i in range(int(have.value)):
                info = buffer[i]
                started = _filetime_to_epoch(info.Process.ProcessStartTime)
                app_type = int(info.ApplicationType)
                owners.append({
                    "pid": int(info.Process.dwProcessId),
                    "process_start_time": started,
                    "process_start_time_utc": _iso(started),
                    "app_name": info.strAppName,
                    "service_short_name": info.strServiceShortName,
                    "app_type": app_type,
                    "app_type_name": RM_APP_TYPES.get(app_type, f"unknown({app_type})"),
                    "app_status": int(info.AppStatus),
                    "ts_session_id": int(info.TSSessionId),
                    "restartable": bool(info.bRestartable),
                })
            record["owners"] = owners
        finally:
            lib.RmEndSession(session)
    return out


def lockable_files(root: Path, *, limit: int) -> tuple[list[Path], bool, int | None]:
    """Every `.exe` / `.dll` / `.pyd` under `root`, most interesting first.

    Returns (files, truncated, total). The ordering is load-bearing: if the cap
    is hit, the files that matter to WP-R01 - the two shipped executables and
    the CPython DLL named in the failure - must still be in the sample. A
    truncated capture that dropped `python312.dll` would answer question 1 with
    silence.
    """
    if not root.is_dir():
        return [], False, None
    found: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        for name in filenames:
            if name.lower().endswith((".exe", ".dll", ".pyd")):
                found.append(Path(dirpath) / name)
    total = len(found)

    def priority(path: Path) -> tuple[int, str]:
        low = path.name.lower()
        if low in ("rag.exe", "ragw.exe"):
            return (0, low)
        if low.startswith("python") and low.endswith(".dll"):
            return (1, low)
        if low.endswith(".exe"):
            return (2, low)
        return (3, str(path).lower())

    found.sort(key=priority)
    truncated = total > limit
    return found[:limit], truncated, total


# =============================================================================
# Phase B / Phase D captures
# =============================================================================


def _run_bytes(argv: list[str], timeout: int) -> dict[str, Any]:
    """A subprocess call whose output is decoded defensively.

    `text=True` decodes with the locale codec and raises on anything else;
    `schtasks` emits OEM code pages. Losing the whole task inventory to a
    UnicodeDecodeError would be the harness destroying its own evidence.
    """
    record: dict[str, Any] = {"argv": argv, "returncode": None,
                              "stdout": None, "stderr": None, "error": None}
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record
    record["returncode"] = proc.returncode
    for key, raw in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        for codec in ("utf-8", "cp1252", "latin-1"):
            try:
                record[key] = raw.decode(codec)
                break
            except UnicodeDecodeError:
                continue
        else:                                                # pragma: no cover
            record[key] = raw.decode("latin-1", "replace")
    return record


def classify_process(name: str | None, cmdline: Sequence[str] | None) -> dict[str, str]:
    """Which component of the product a process is - tray, supervisor, service,
    managed Qdrant, MCP, or other - and how confident that reading is.

    The confidence is not decoration. When a pid is only ever seen as a name (it
    exited before its command line could be read), the role is a guess from a
    filename, and question 2 must say so rather than assert "tray".
    """
    low_name = (name or "").lower()
    parts = [p.lower() for p in (cmdline or [])]
    joined = " ".join(parts)
    # Tokenised, not substring-matched: `serve` is a subcommand, and a
    # substring test would also fire on any path or flag that happens to
    # contain the letters.
    tokens = {token.strip("-/") for part in parts for token in part.split()}
    tokens |= {Path(part).name for part in parts if part}

    if low_name == "qdrant.exe":
        return {"role": "managed_qdrant", "confidence": "high",
                "basis": "image name is the pinned engine"}
    if not joined:
        if low_name in ("rag.exe", "ragw.exe"):
            return {"role": "ragtools_binary_unclassified", "confidence": "name_only",
                    "basis": "no command line was readable for this pid"}
        if low_name:
            return {"role": "other", "confidence": "name_only", "basis": low_name}
        return {"role": "unknown", "confidence": "none", "basis": "no name, no cmdline"}

    if "tray" in tokens:
        return {"role": "tray", "confidence": "high", "basis": "cmdline names `tray`"}
    # `rag serve` IS the MCP server (see the entry points in pyproject.toml and
    # the Key Commands table in CLAUDE.md), so it must not be filed as a generic
    # worker - question 2 asks specifically whether an MCP process held the file.
    if "serve" in tokens or any("mcp" in token for token in tokens):
        return {"role": "mcp", "confidence": "high",
                "basis": "`serve` / `mcp` - the MCP server entry point"}
    if "service" in tokens and ({"start", "run"} & tokens):
        return {"role": "service_supervisor", "confidence": "high",
                "basis": "cmdline starts/runs the service"}
    if "uvicorn" in joined:
        return {"role": "service_supervisor", "confidence": "high", "basis": "uvicorn"}
    if "service" in tokens:
        return {"role": "service_cli", "confidence": "moderate",
                "basis": "a transient `service ...` CLI invocation"}
    if {"watch", "index", "rebuild"} & tokens:
        return {"role": "worker_cli", "confidence": "moderate", "basis": joined[:120]}
    if low_name in ("rag.exe", "ragw.exe"):
        return {"role": "ragtools_binary_unclassified", "confidence": "moderate",
                "basis": joined[:120]}
    return {"role": "other", "confidence": "moderate", "basis": joined[:120]}


def process_snapshot(install: Path) -> dict[str, Any]:
    """Every process whose image OR any loaded module lies under `install`.

    `memory_maps()` is what makes this a lock inventory rather than a process
    list: a process that loaded `python312.dll` holds it whether or not its own
    executable lives in the install directory. Access denials are RECORDED per
    process - a process we were not allowed to inspect is `undetermined`, not
    "not holding anything", and collapsing those two is how a harness invents a
    clean bill of health.
    """
    started = time.monotonic()
    snapshot: dict[str, Any] = {
        "install_dir": str(install),
        "psutil_available": psutil is not None,
        "error": PSUTIL_UNAVAILABLE,
        "processes": None,
        "inspection_denied": [],
        "total_processes_seen": None,
        "elapsed_s": None,
    }
    if psutil is None:
        return snapshot

    root = _normcase(install)
    matched: list[dict[str, Any]] = []
    denied: list[dict[str, Any]] = []
    seen = 0

    for proc in psutil.process_iter(
            attrs=["pid", "ppid", "name", "exe", "cmdline", "create_time", "username"],
            ad_value=None):
        seen += 1
        info = dict(proc.info)
        holds: list[dict[str, str]] = []
        if _under(info.get("exe"), root):
            holds.append({"kind": "exe", "path": info.get("exe") or ""})

        modules_state = "examined"
        try:
            for mapping in proc.memory_maps():
                if _under(getattr(mapping, "path", None), root):
                    holds.append({"kind": "module", "path": mapping.path})
        except psutil.AccessDenied:
            modules_state = "access_denied"
            denied.append({"pid": info.get("pid"), "name": info.get("name"),
                           "reason": "AccessDenied on memory_maps()"})
        except psutil.NoSuchProcess:
            modules_state = "exited_during_scan"
        except (OSError, RuntimeError) as exc:
            modules_state = f"error: {type(exc).__name__}: {exc}"
            denied.append({"pid": info.get("pid"), "name": info.get("name"),
                           "reason": modules_state})

        if not holds:
            continue
        created = info.get("create_time")
        matched.append({
            "pid": info.get("pid"),
            "ppid": info.get("ppid"),
            "name": info.get("name"),
            "exe": info.get("exe"),
            "cmdline": info.get("cmdline"),
            "username": info.get("username"),
            "create_time": created,
            "create_time_utc": _iso(created),
            "holds": holds,
            "modules_state": modules_state,
            "classification": classify_process(info.get("name"), info.get("cmdline")),
        })

    snapshot["processes"] = matched
    snapshot["inspection_denied"] = denied
    snapshot["total_processes_seen"] = seen
    snapshot["elapsed_s"] = round(time.monotonic() - started, 3)
    return snapshot


def scheduled_tasks(install: Path) -> dict[str, Any]:
    """Scheduled tasks whose action points into the install directory.

    Also keeps any task NAMED for this product whose action points elsewhere:
    a RAGTools task aimed at a stale path is exactly the sort of thing that
    restarts a process the installer has just killed, and filtering purely on
    the current path would hide it.
    """
    capture = _run_bytes(["schtasks", "/query", "/fo", "CSV", "/v"], timeout=180)
    out: dict[str, Any] = {
        "returncode": capture["returncode"],
        "error": capture["error"],
        "columns": None,
        "tasks": None,
        "parse_warning": None,
    }
    if capture["error"] is not None or not capture["stdout"]:
        out["parse_warning"] = "schtasks produced no parseable output"
        return out

    root = _normcase(install)
    reader = csv.DictReader(io.StringIO(capture["stdout"]))
    out["columns"] = reader.fieldnames
    columns = reader.fieldnames or []
    action_col = next((c for c in columns if c.strip().lower() == "task to run"), None)
    name_col = next((c for c in columns if c.strip().lower() == "taskname"), None)
    if action_col is None or name_col is None:
        out["parse_warning"] = (
            "expected columns 'TaskName' and 'Task To Run' are absent - schtasks "
            "localises its CSV headers, so this machine's locale is not English")
        return out

    tasks: list[dict[str, Any]] = []
    for row in reader:
        if (row.get(name_col) or "").strip().lower() == "taskname":
            continue                       # schtasks repeats the header per task
        name = (row.get(name_col) or "").strip()
        action = (row.get(action_col) or "").strip()
        reasons = []
        if action and root in _normcase(action.strip('"').split('"')[0] or "."):
            reasons.append("action_path_in_install_dir")
        elif action and root in action.lower().replace("/", "\\"):
            reasons.append("action_mentions_install_dir")
        if "ragtools" in name.lower():
            reasons.append("task_name_names_this_product")
        if not reasons:
            continue
        tasks.append({
            "task_name": name,
            "action": action,
            "matched_on": reasons,
            "state": row.get("Status") or row.get("Scheduled Task State"),
            "last_run_time": row.get("Last Run Time"),
            "next_run_time": row.get("Next Run Time"),
            "last_result": row.get("Last Result"),
            "raw_row": row,
        })
    out["tasks"] = tasks
    return out


def service_health(port: int) -> dict[str, Any]:
    """`/health`, VERBATIM. The parsed form is a convenience beside the raw body,
    never a replacement: a body that does not parse is itself the finding."""
    import urllib.error
    import urllib.request

    record: dict[str, Any] = {"url": f"http://127.0.0.1:{port}/health",
                              "status": None, "raw": None, "parsed": None,
                              "error": None}
    try:
        with urllib.request.urlopen(record["url"], timeout=8) as response:
            record["status"] = response.status
            record["raw"] = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        record["status"] = exc.code
        try:
            record["raw"] = exc.read().decode("utf-8", "replace")
        except Exception:                                    # noqa: BLE001
            pass
        record["error"] = f"HTTPError {exc.code}"
        return record
    except Exception as exc:                                 # noqa: BLE001
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record
    try:
        record["parsed"] = json.loads(record["raw"])
    except json.JSONDecodeError as exc:
        record["error"] = f"body did not parse as JSON: {exc}"
    return record


def sha256_file(path: Path) -> tuple[str | None, str | None]:
    """(digest, error). A file that could not be read is `None` - never a digest
    of nothing, and never the empty-string hash, which is a real SHA-256 and
    would compare equal across two unreadable files."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return digest.hexdigest(), None


def file_inventory(root: Path) -> dict[str, Any]:
    """SHA-256, size and mtime of every file under `root`, keyed by relative path."""
    out: dict[str, Any] = {
        "root": str(root),
        "exists": root.is_dir(),
        "files": None,
        "walk_errors": [],
        "unreadable": [],
        "file_count": None,
    }
    if not out["exists"]:
        out["walk_errors"].append(f"{root} is not a directory")
        return out

    files: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    def _on_error(exc: OSError) -> None:
        errors.append(f"{getattr(exc, 'filename', '?')}: {exc}")

    for dirpath, _dirnames, filenames in os.walk(root, onerror=_on_error):
        for name in filenames:
            full = Path(dirpath) / name
            rel = os.path.relpath(full, root)
            key = os.path.normcase(rel)
            try:
                stat = full.stat()
                size: int | None = stat.st_size
                mtime: float | None = stat.st_mtime
                stat_error = None
            except OSError as exc:
                size = mtime = None
                stat_error = f"{type(exc).__name__}: {exc}"
            digest, digest_error = sha256_file(full)
            if digest is None:
                out["unreadable"].append({"path": rel, "error": digest_error})
            files[key] = {
                "path": rel,
                "size": size,
                "mtime": mtime,
                "mtime_utc": _iso(mtime),
                "sha256": digest,
                "error": stat_error or digest_error,
            }
    out["files"] = files
    out["file_count"] = len(files)
    out["walk_errors"] = errors
    return out


def directory_digest(root: Path) -> dict[str, Any]:
    """One digest over a whole tree - or `None` and the reason.

    A digest computed over a tree where three files could not be read is not a
    weaker digest, it is a false one: it would compare equal to a later capture
    that failed on the same three files for a completely different reason.
    """
    inventory = file_inventory(root)
    record: dict[str, Any] = {
        "root": str(root),
        "exists": inventory["exists"],
        "file_count": inventory["file_count"],
        "digest": None,
        "error": None,
        "unreadable": inventory["unreadable"],
    }
    if not inventory["exists"]:
        record["error"] = "directory does not exist"
        return record
    if inventory["unreadable"]:
        record["error"] = (
            f"{len(inventory['unreadable'])} file(s) could not be hashed; a digest "
            "over the rest would compare equal to an unrelated partial capture")
        return record
    if inventory["walk_errors"]:
        record["error"] = f"walk errors: {inventory['walk_errors'][:5]}"
        return record

    roll = hashlib.sha256()
    for key in sorted(inventory["files"]):
        entry = inventory["files"][key]
        roll.update(key.encode("utf-8", "surrogatepass"))
        roll.update(b"\0")
        roll.update((entry["sha256"] or "").encode("ascii"))
        roll.update(b"\n")
    record["digest"] = roll.hexdigest()
    return record


#: Where the product's own state lives. Both layouts are probed because the data
#: directory moved under `data/` during v3, and an upgrade harness has to be
#: able to read a v2 machine's artefacts as well as a v3 one's.
USER_DATA_ARTEFACTS = {
    "config.toml": ("config.toml", "data/config.toml"),
    "registry.db": ("data/registry.db", "registry.db"),
    "index_state.db": ("data/index_state.db", "index_state.db"),
}
QDRANT_STORAGE_CANDIDATES = ("data/qdrant", "qdrant", "data/storage")


def user_data_digests() -> dict[str, Any]:
    """SHA-256 of the user's own artefacts, plus a recursive digest of the store.

    A broken binary install that LEFT USER DATA ALONE and a broken binary
    install that also ate the index are different bugs with different blast
    radii, and the verdict has to be able to say which one happened.
    """
    out: dict[str, Any] = {"data_dir": str(DATA_DIR), "artefacts": {}, "qdrant": None}
    for label, candidates in USER_DATA_ARTEFACTS.items():
        record: dict[str, Any] = {"path": None, "sha256": None, "size": None,
                                  "error": None, "searched": []}
        for relative in candidates:
            candidate = DATA_DIR / relative
            record["searched"].append(str(candidate))
            if candidate.is_file():
                record["path"] = str(candidate)
                digest, error = sha256_file(candidate)
                record["sha256"] = digest
                record["error"] = error
                try:
                    record["size"] = candidate.stat().st_size
                except OSError as exc:
                    record["error"] = f"{record['error'] or ''} stat: {exc}".strip()
                break
        if record["path"] is None:
            record["error"] = "not found at any known location"
        out["artefacts"][label] = record

    for relative in QDRANT_STORAGE_CANDIDATES:
        candidate = DATA_DIR / relative
        if candidate.is_dir():
            out["qdrant"] = directory_digest(candidate)
            break
    if out["qdrant"] is None:
        out["qdrant"] = {"root": None, "exists": False, "digest": None,
                         "file_count": None,
                         "error": "no Qdrant storage directory at "
                                  + ", ".join(str(DATA_DIR / c)
                                              for c in QDRANT_STORAGE_CANDIDATES)}
    return out


def uninstall_entry() -> dict[str, Any]:
    """The uninstall registry entry, every value, verbatim, and which hive it
    came from - `{autopf}` resolves differently per install mode, so the hive is
    part of the fact."""
    record: dict[str, Any] = {"key": UNINSTALL_KEY, "hive": None,
                              "values": None, "error": None}
    if not _WINDOWS:                                         # pragma: no cover
        record["error"] = "not Windows"
        return record
    for hive, hive_name in ((winreg.HKEY_CURRENT_USER, "HKCU"),
                            (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
        try:
            with winreg.OpenKey(hive, UNINSTALL_KEY) as key:
                count = winreg.QueryInfoKey(key)[1]
                values: dict[str, Any] = {}
                for index in range(count):
                    name, value, _kind = winreg.EnumValue(key, index)
                    values[name] = value
                record["hive"] = hive_name
                record["values"] = values
                return record
        except OSError as exc:
            record["error"] = f"{hive_name}: {type(exc).__name__}: {exc}"
            continue
    return record


def capture_state(label: str, install: Path, port: int, *,
                  rm_file_limit: int) -> dict[str, Any]:
    """One complete picture of the machine. Run identically before and after, so
    the two are comparable field for field."""
    print(f"\n--- capturing machine state: {label} ---", flush=True)
    started = time.monotonic()
    lock_files, truncated, total = lockable_files(install, limit=rm_file_limit)
    rm_started = time.monotonic()
    rm_records = rm_owners_for_files(lock_files)
    rm_elapsed = round(time.monotonic() - rm_started, 3)
    state: dict[str, Any] = {
        "label": label,
        "captured_at": _iso(time.time()),
        "install_dir": str(install),
        "install_dir_exists": install.is_dir(),
        "processes": process_snapshot(install),
        "restart_manager": {
            "files_examined": len(lock_files),
            "files_total": total,
            "truncated": truncated,
            "limit": rm_file_limit,
            "process_info_struct_size": RM_PROCESS_INFO_SIZE,
            "process_info_struct_expected": RM_PROCESS_INFO_EXPECTED_SIZE,
            "elapsed_s": rm_elapsed,
            "records": rm_records,
        },
        "scheduled_tasks": scheduled_tasks(install),
        "health": service_health(port),
        "install_inventory": file_inventory(install),
        "uninstall_registry": uninstall_entry(),
        "user_data": user_data_digests(),
    }
    state["elapsed_s"] = round(time.monotonic() - started, 3)

    holders = [r for r in state["restart_manager"]["records"] if r["owners"]]
    procs = state["processes"]["processes"]
    print(f"    {len(lock_files)} lockable file(s) probed, "
          f"{len(holders)} with an RM owner; "
          f"{'unknown' if procs is None else len(procs)} process(es) hold install files"
          f" ({state['elapsed_s']}s)", flush=True)
    return state


# =============================================================================
# Phase C - sampling the install window
# =============================================================================


def _scan_install_files(root: Path) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """(normcase path -> (size, mtime_ns), errors). Deliberately stat-only.

    Hashing 1500 files cannot be done four times a second, and a sampler that
    fell behind would mis-timestamp the very event it exists to catch. The
    metadata change is the DETECTION; Phase D's real SHA-256 comparison is the
    CONFIRMATION, and the verdict reports both so neither is mistaken for the
    other.
    """
    files: dict[str, tuple[int, int]] = {}
    errors: list[str] = []
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            stat = entry.stat()
                            files[os.path.normcase(entry.path)] = (
                                stat.st_size, stat.st_mtime_ns)
                    except OSError as exc:
                        errors.append(f"{entry.path}: {exc}")
        except OSError as exc:
            errors.append(f"{current}: {exc}")
    return files, errors


class InstallSampler(threading.Thread):
    """Samples processes and install-directory file metadata every `interval`.

    Two findings depend entirely on this thread:

      * the FIRST DESTRUCTIVE WRITE - the moment the installer stopped asking
        and started changing the machine, which is what a shutdown timestamp has
        to be compared against;
      * a PROCESS APPEARANCE - a (pid, create-time) pair that was not in the
        previous sample. That is a restart, and whether one happened inside the
        install window is the difference between "the installer failed to stop
        something" and "something started again behind it".

    Its accuracy is bounded and the bounds are published in the output: a
    process that starts and exits inside one interval is invisible, and an event
    is timestamped when it was NOTICED, not when it happened.
    """

    #: At most this many never-before-seen pids are inspected in one sample.
    #: An installer run produces a handful; a runner that suddenly spawns
    #: hundreds must not be allowed to stall the file walk behind them.
    NEW_PID_INSPECTION_CAP = 40

    #: Ceiling on recorded sampler errors, so a directory disappearing under the
    #: walker cannot fill the bundle with one repeated message.
    MAX_RECORDED_ERRORS = 200

    def __init__(self, install: Path, interval: float, max_events: int) -> None:
        super().__init__(name="install-sampler", daemon=True)
        self.install = install
        self.interval = interval
        self.max_events = max_events
        # `_halt`, NOT `_stop`: `threading.Thread` already owns a private
        # `_stop()` and shadowing it with an Event makes `join()` raise
        # "'Event' object is not callable" - after the installer has run, which
        # is exactly when the evidence would be lost.
        self._halt = threading.Event()
        self.t0_monotonic: float | None = None
        self.t0_wall: float | None = None
        self.primed = False
        self.primed_at_wall: float | None = None
        self.prime_duration_s: float | None = None
        self.samples = 0
        self.process_events: list[dict[str, Any]] = []
        self.file_events: list[dict[str, Any]] = []
        self.first_destructive_write: dict[str, Any] | None = None
        self.first_content_change: dict[str, Any] | None = None
        self.errors: list[str] = []
        self.events_truncated = False
        self.max_sample_duration_s = 0.0
        self.total_sample_duration_s = 0.0
        self.new_pid_cap_hit = 0
        self.alive_at_first_write: list[dict[str, Any]] | None = None
        self._owned: dict[tuple[int, int], dict[str, Any]] = {}
        self._inspected_pids: set[int] = set()
        self._last_files: dict[str, tuple[int, int]] | None = None

    # -- process side ---------------------------------------------------
    #
    # MEASURED, not assumed. `psutil.process_iter(attrs=[..., "exe"])` costs
    # 5-23 s on a machine with 750 processes, because resolving `exe` opens a
    # handle per process. A sampler built on it does not sample every 250 ms; it
    # samples every eight seconds and reports "interval_s: 0.25", which is worse
    # than not sampling at all - the number in the bundle would be a fiction and
    # every elapsed time derived from it would inherit the fiction.
    #
    # `psutil.pids()` is one syscall and costs nothing. So the sample is a pid
    # SET diff: only pids never seen before are inspected (there are a handful
    # during an install), and the small owned set is re-verified for pid reuse.

    @staticmethod
    def _inspect(pid: int) -> dict[str, Any] | None:
        if psutil is None:
            return None
        try:
            proc = psutil.Process(pid)
            with proc.oneshot():
                return {
                    "pid": pid,
                    "ppid": proc.ppid(),
                    "name": proc.name(),
                    "exe": proc.exe(),
                    "create_time": proc.create_time(),
                    "create_time_utc": _iso(proc.create_time()),
                }
        except (psutil.Error, OSError):
            # A pid that vanished or refused inspection. Returning None here
            # loses it - but it is recorded as an inspection failure by the
            # caller rather than silently counted as "not owned".
            return None

    def _is_owned(self, info: dict[str, Any], root: str) -> bool:
        """Matched by IMAGE NAME as well as by path: once `[InstallDelete]` has
        removed `_internal`, a surviving process's `exe` may no longer resolve
        under the install directory, and dropping it there would erase the
        process at the moment it matters most."""
        name = (info.get("name") or "").lower()
        return name in ("rag.exe", "ragw.exe", "qdrant.exe") or _under(info.get("exe"), root)

    def _refresh_owned(self) -> tuple[dict[tuple[int, int], dict[str, Any]],
                                      list[dict[str, Any]]]:
        """(owned processes keyed by (pid, create-time-ms), newly appeared)."""
        if psutil is None:
            return {}, []
        root = _normcase(self.install)
        try:
            live = set(psutil.pids())
        except OSError as exc:
            self.errors.append(f"psutil.pids(): {exc}")
            return self._owned, []

        # Re-verify the small owned set. Keyed on (pid, create-time) rather than
        # the pid alone because Windows reuses pids promptly, and a reused pid
        # would otherwise read as "still running" - the mistake
        # `service/engine_ownership.py` exists to prevent.
        surviving: dict[tuple[int, int], dict[str, Any]] = {}
        for key, info in self._owned.items():
            pid = int(info["pid"])
            if pid not in live:
                continue
            try:
                if int(psutil.Process(pid).create_time() * 1000) == key[1]:
                    surviving[key] = info
            except (psutil.Error, OSError):
                continue

        appeared: list[dict[str, Any]] = []
        fresh = sorted(live - self._inspected_pids)
        if len(fresh) > self.NEW_PID_INSPECTION_CAP:
            self.new_pid_cap_hit += 1
            fresh = fresh[:self.NEW_PID_INSPECTION_CAP]
        for pid in fresh:
            self._inspected_pids.add(pid)
            info = self._inspect(pid)
            if info is None or not self._is_owned(info, root):
                continue
            key = (pid, int((info.get("create_time") or 0.0) * 1000))
            surviving[key] = info
            appeared.append(info)
        # Prune dead pids from the "already inspected" set. This is what closes
        # the pid-reuse hole: a pid that exits and is later handed to a restarted
        # rag.exe is unknown again by then, so the restart is seen as an
        # appearance instead of being mistaken for the process that used to hold
        # that number.
        self._inspected_pids &= live
        return surviving, appeared

    def prime(self) -> None:
        """Establish the baseline BEFORE the installer is spawned.

        A full owned-process inventory costs seconds on a busy machine (28 s on
        a 750-process developer box, a few seconds on a hosted runner). Paying
        that inside the install window would put t=0 that far after the
        installer actually started and mis-timestamp every finding derived from
        it; paying it here costs nothing that matters.

        Deliberately does NOT set t=0. The clock starts when the thread starts,
        which the caller arranges to be immediately before the installer is
        spawned - if `prime()` owned t=0, every elapsed time in the bundle would
        be offset by however long the baseline happened to take.
        """
        started = time.monotonic()
        self.primed_at_wall = time.time()
        if psutil is not None:
            root = _normcase(self.install)
            try:
                for proc in psutil.process_iter(
                        attrs=["pid", "ppid", "name", "exe", "create_time"],
                        ad_value=None):
                    info = dict(proc.info)
                    pid = int(info.get("pid") or -1)
                    self._inspected_pids.add(pid)
                    if not self._is_owned(info, root):
                        continue
                    created = info.get("create_time") or 0.0
                    info["create_time_utc"] = _iso(created)
                    self._owned[(pid, int(created * 1000))] = info
            except (psutil.Error, OSError) as exc:
                self.errors.append(f"prime: {type(exc).__name__}: {exc}")
        # Command lines for the OWNED set only - a handful of processes, ~16 ms
        # each. Asking `process_iter` for `cmdline` across all 750 would cost
        # seconds; skipping it entirely would leave every baseline process
        # classifiable only by filename, and "which of these three ragw.exe is
        # the tray" is question 2.
        for info in self._owned.values():
            info["cmdline"] = self._cmdline_for(int(info.get("pid") or -1))
            info["classification"] = classify_process(info.get("name"),
                                                      info.get("cmdline"))
        self._last_files, errors = _scan_install_files(self.install)
        self.errors.extend(errors[:5])
        for info in self._owned.values():
            self._record(self.process_events,
                         {"elapsed_s": 0.0, "event": "baseline", "process": info})
        self.primed = True
        self.prime_duration_s = round(time.monotonic() - started, 3)

    @staticmethod
    def _cmdline_for(pid: int) -> list[str] | None:
        if psutil is None:
            return None
        try:
            return psutil.Process(pid).cmdline()
        except (psutil.Error, OSError):
            return None

    def _record(self, bucket: list[dict[str, Any]], event: dict[str, Any]) -> None:
        if len(bucket) >= self.max_events:
            self.events_truncated = True
            return
        bucket.append(event)

    # -- thread ---------------------------------------------------------
    def run(self) -> None:                                   # noqa: D102
        if not self.primed:
            self.prime()
        # t=0 is HERE - the moment sampling begins, which the caller arranges to
        # be a few milliseconds before the installer is spawned. The offset
        # between the two is measured and published rather than assumed to be
        # zero. Pacing also starts here: an earlier version anchored the
        # deadline to `prime()`'s start, so the loop "caught up" on 114 missed
        # ticks in a burst, back to back, and every one of those samples carried
        # a timestamp that had never been observed.
        base = time.monotonic()
        self.t0_monotonic = base
        self.t0_wall = time.time()
        index = 0
        while not self._halt.is_set():
            sample_started = time.monotonic()
            try:
                self._sample(round(sample_started - base, 3))
            except Exception as exc:                         # noqa: BLE001
                # A sampler that dies takes the whole finding with it, silently.
                self.errors.append(f"sample {index}: {type(exc).__name__}: {exc}")
            duration = time.monotonic() - sample_started
            self.max_sample_duration_s = max(self.max_sample_duration_s, duration)
            self.total_sample_duration_s += duration
            self.samples += 1
            index += 1
            deadline = base + index * self.interval
            self._halt.wait(max(0.0, deadline - time.monotonic()))

    def _sample(self, elapsed: float) -> None:
        previous = self._owned
        current_processes, appeared = self._refresh_owned()
        self._owned = current_processes
        for info in appeared:
            payload = dict(info)
            payload["cmdline"] = self._cmdline_for(int(info["pid"] or -1))
            payload["classification"] = classify_process(
                info.get("name"), payload["cmdline"])
            self._record(self.process_events, {
                "elapsed_s": elapsed, "event": "appeared", "process": payload})
        for key, info in previous.items():
            if key not in current_processes:
                self._record(self.process_events, {
                    "elapsed_s": elapsed, "event": "disappeared", "process": info})

        files, errors = _scan_install_files(self.install)
        # Bounded. An install directory being deleted under the walker produces
        # errors on every sample, and five per sample across a few hundred
        # samples would bury the bundle in the same message.
        if errors and len(self.errors) < self.MAX_RECORDED_ERRORS:
            self.errors.extend(errors[:3])
        if self._last_files is None:
            self._last_files = files
            return

        changes: list[dict[str, Any]] = []
        for key, meta in files.items():
            previous = self._last_files.get(key)
            if previous is None:
                changes.append({"path": key, "change": "created",
                                "size": meta[0], "mtime_ns": meta[1]})
            elif previous != meta:
                changes.append({"path": key, "change": "modified",
                                "size_before": previous[0], "size_after": meta[0],
                                "mtime_ns_before": previous[1],
                                "mtime_ns_after": meta[1]})
        for key, previous in self._last_files.items():
            if key not in files:
                changes.append({"path": key, "change": "deleted",
                                "size_before": previous[0]})

        for change in changes:
            event = dict(change)
            event["elapsed_s"] = elapsed
            self._record(self.file_events, event)
            if self.first_destructive_write is None:
                self.first_destructive_write = event
                self.alive_at_first_write = [
                    dict(info) for info in current_processes.values()]
            if self.first_content_change is None and change["change"] == "modified":
                self.first_content_change = event
        self._last_files = files

    def stop(self) -> None:
        self._halt.set()

    def report(self) -> dict[str, Any]:
        """The measurements AND the resolution they were taken at.

        `achieved_interval_s` is the number that matters: the requested interval
        is an intention, and every elapsed time in this bundle is only as precise
        as the cadence actually reached. Publishing the request and hiding the
        achievement is how a sampler that ran every eight seconds gets quoted as
        millisecond evidence.
        """
        window = None
        if self.t0_monotonic is not None and self.samples:
            window = round(self.total_sample_duration_s, 3)
        achieved = (round(self.total_sample_duration_s / self.samples, 4)
                    if self.samples else None)
        achieved_interval = max(achieved or 0.0, self.interval) if self.samples else None
        return {
            "interval_s": self.interval,
            "achieved_interval_s": achieved_interval,
            "mean_sample_duration_s": achieved,
            "max_sample_duration_s": round(self.max_sample_duration_s, 3),
            "total_sampling_time_s": window,
            "samples": self.samples,
            "prime_duration_s": self.prime_duration_s,
            "primed_at_utc": _iso(self.primed_at_wall),
            "t0_wall_utc": _iso(self.t0_wall),
            "t0_meaning": "sampling start; the installer was spawned "
                          "installer_spawn_offset_s later",
            "events_truncated": self.events_truncated,
            "max_events": self.max_events,
            "new_pid_cap_hit_samples": self.new_pid_cap_hit,
            "psutil_available": psutil is not None,
            "psutil_error": PSUTIL_UNAVAILABLE,
            "first_destructive_write": self.first_destructive_write,
            "first_content_change": self.first_content_change,
            "alive_at_first_write": self.alive_at_first_write,
            "process_events": self.process_events,
            "file_events": self.file_events,
            "errors": self.errors,
            "accuracy_limits": [
                f"the requested interval was {self.interval}s; the ACHIEVED "
                f"interval was {achieved_interval}s (mean sample cost {achieved}s, "
                f"worst {round(self.max_sample_duration_s, 3)}s) - every elapsed "
                "time here is only as precise as that",
                "a process that starts and exits between two samples is never "
                "observed",
                "events are timestamped when NOTICED, not when they happened",
                "file changes are detected by (size, mtime_ns); a rewrite that "
                "preserved both is missed here and caught instead by the Phase D "
                "SHA-256 comparison",
                "processes are keyed by (pid, create_time), so a reused pid is not "
                "mistaken for a surviving process; dead pids are forgotten each "
                "sample, so a restart onto a recycled pid IS seen - unless the "
                "exit and the reuse both happened inside one interval",
                f"at most {self.NEW_PID_INSPECTION_CAP} new pids are inspected per "
                f"sample; the cap was reached in {self.new_pid_cap_hit} sample(s)",
            ],
        }


class LockProbe(threading.Thread):
    """Restart Manager ownership of a few critical files, sampled during install.

    The 250 ms sampler cannot do this - an RM session costs tens of milliseconds
    per file - and the question "who held python312.dll WHEN the installer tried
    to replace it" cannot be answered from a capture taken before the installer
    started. So a handful of files are probed on a slower beat, and the timeline
    records a sample only when the owner set CHANGES, which is what makes it
    readable.
    """

    def __init__(self, files: Sequence[Path], interval: float) -> None:
        super().__init__(name="lock-probe", daemon=True)
        self.files = list(files)
        self.interval = interval
        # `_halt`, NOT `_stop`: `threading.Thread` already owns a private
        # `_stop()` and shadowing it with an Event makes `join()` raise
        # "'Event' object is not callable" - after the installer has run, which
        # is exactly when the evidence would be lost.
        self._halt = threading.Event()
        self.t0: float | None = None
        self.samples = 0
        self.total_sample_duration_s = 0.0
        self.max_sample_duration_s = 0.0
        self.timeline: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self._previous: dict[str, tuple[Any, ...]] = {}

    def run(self) -> None:                                   # noqa: D102
        self.t0 = time.monotonic()
        index = 0
        while not self._halt.is_set():
            sample_started = time.monotonic()
            elapsed = round(sample_started - self.t0, 3)
            try:
                for record in rm_owners_for_files(self.files):
                    owners = record["owners"]
                    if owners is None:
                        signature: tuple[Any, ...] = ("error", record["error"])
                    else:
                        signature = tuple(sorted(
                            (owner["pid"], owner["app_name"]) for owner in owners))
                    if self._previous.get(record["path"]) == signature:
                        continue
                    self._previous[record["path"]] = signature
                    self.timeline.append({
                        "elapsed_s": elapsed,
                        "path": record["path"],
                        "exists": record["exists"],
                        "owners": owners,
                        "error": record["error"],
                    })
            except Exception as exc:                         # noqa: BLE001
                self.errors.append(f"{type(exc).__name__}: {exc}")
            duration = time.monotonic() - sample_started
            self.total_sample_duration_s += duration
            self.max_sample_duration_s = max(self.max_sample_duration_s, duration)
            self.samples += 1
            index += 1
            deadline = (self.t0 or 0) + index * self.interval
            self._halt.wait(max(0.0, deadline - time.monotonic()))

    def stop(self) -> None:
        self._halt.set()

    def report(self) -> dict[str, Any]:
        mean = (round(self.total_sample_duration_s / self.samples, 4)
                if self.samples else None)
        # One RM session costs ~0.3 s against a real installation, so six files
        # is ~1.9 s and the requested 2 s interval is nearly saturated. Say what
        # was ACHIEVED - a timeline claiming 2 s resolution while running at 3 s
        # would misdate every ownership change in it.
        achieved = max(mean or 0.0, self.interval) if self.samples else None
        return {
            "interval_s": self.interval,
            "achieved_interval_s": achieved,
            "mean_sample_duration_s": mean,
            "max_sample_duration_s": round(self.max_sample_duration_s, 3),
            "samples": self.samples,
            "files": [str(f) for f in self.files],
            "timeline": self.timeline,
            "errors": self.errors,
            "note": "an entry is written only when a file's owner set changes; "
                    "between two entries the earlier owner set still held",
        }


def lock_probe_targets(install: Path) -> list[Path]:
    """The handful of files worth probing every couple of seconds.

    Deliberately small: one Restart Manager session costs ~0.3 s against a real
    installation, so a probe list of six is already most of a 2 s interval and a
    long list would silently stretch the cadence it claims to keep.

    The managed engine's binary is searched for in BOTH trees. It ships in
    `<install>\\bin` on a packaged install and can also sit in `<data>\\bin` when
    the user supplied it - and it is a prime suspect, because a running engine
    holds its own image for the whole install.
    """
    targets: list[Path] = []
    for relative in ("rag.exe", "ragw.exe", "unins000.exe",
                     "bin/qdrant.exe", "qdrant.exe"):
        candidate = install / relative
        if candidate.exists():
            targets.append(candidate)
    internal = install / "_internal"
    if internal.is_dir():
        targets.extend(sorted(internal.glob("python*.dll")))
    for relative in ("bin/qdrant.exe", "qdrant.exe"):
        candidate = DATA_DIR / relative
        if candidate.exists():
            targets.append(candidate)
    return targets


# =============================================================================
# Inno's own record
# =============================================================================


def _decode_inno(raw: bytes) -> tuple[str, str]:
    """Inno writes UTF-16LE with a BOM. Decoding it as UTF-8 yields a string
    starting U+FEFF, and printing that to a cp1252 console raises - which is how
    a previous helper crashed the script it was added to diagnose."""
    for codec in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            return raw.decode(codec), codec
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", "replace"), "latin-1"


def read_inno_log(path: Path) -> dict[str, Any]:
    """The Inno log, VERBATIM and in full.

    The observed failure produced a one-line log. That single line is the datum:
    Setup writes this file incrementally, so where it stops is where Setup
    stopped. Summarising it away - keeping only a tail, or only the lines that
    matched a pattern - would destroy the evidence the bundle exists to carry.
    """
    record: dict[str, Any] = {"path": str(path), "exists": path.is_file(),
                              "line_count": None, "encoding": None,
                              "text": None, "error": None, "size": None}
    if not record["exists"]:
        record["error"] = "Inno wrote no log at this path"
        return record
    try:
        record["size"] = path.stat().st_size
        raw = path.read_bytes()
    except OSError as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record
    text, codec = _decode_inno(raw)
    text = text.replace("﻿", "")
    record["encoding"] = codec
    record["text"] = text
    record["line_count"] = len(text.splitlines())
    return record


def temp_setup_logs(since_epoch: float) -> list[dict[str, Any]]:
    """Inno's FALLBACK log location.

    When `/LOG=<path>` cannot be written, Setup logs to `%TEMP%\\Setup Log
    YYYY-MM-DD #NNN.txt` instead - and a one-line file at the requested path
    beside a complete one in TEMP is the explanation for the observed evidence,
    not a curiosity. Cheap to collect, and impossible to reconstruct later.
    """
    temp = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    found: list[dict[str, Any]] = []
    try:
        candidates = sorted(temp.glob("Setup Log*.txt"))
    except OSError:
        return found
    for candidate in candidates:
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if mtime + 5 < since_epoch:
            continue
        record = read_inno_log(candidate)
        record["mtime_utc"] = _iso(mtime)
        found.append(record)
    return found


#: Lines the installer's own code and Inno's engine write while shutting things
#: down. `ForceKill:` entries come from `ForceKillRagProcesses()`; the rest are
#: Inno's, and the last two are the exit-5 signature named in `installer.iss`.
SHUTDOWN_MARKERS = (
    "ForceKill: taskkill rag.exe",
    "ForceKill: taskkill ragw.exe",
    "ForceKill: scoped qdrant.exe stopped",
    "ForceKill: owned processes still running afterwards",
    "RestartManager found an application using one of our files",
    "Shutting down applications using our files",
    "Some applications could not be shut down",
    "Starting the installation process",
    "Defaulting to Abort for suppressed message box",
    "User canceled the installation process",
    "Rolling back changes",
    "Need to restart Windows",
)


def parse_inno_timeline(log: dict[str, Any], t0_wall: float | None) -> dict[str, Any]:
    """Pull the shutdown/abort markers out of the Inno log, with elapsed times.

    Inno stamps each line `HH:MM:SS.mmm`, wall-clock and date-less. Converting
    to elapsed uses the installer's own start date; a run that crosses midnight
    would be wrong, and rather than silently produce a negative elapsed the
    conversion is reported as `None` in that case.
    """
    out: dict[str, Any] = {"markers": [], "parsed_lines": None, "note": None}
    text = log.get("text")
    if not text:
        out["note"] = "no log text to parse: " + str(log.get("error"))
        return out
    base_date = datetime.fromtimestamp(t0_wall).date() if t0_wall else None
    lines = text.splitlines()
    out["parsed_lines"] = len(lines)
    for line in lines:
        stripped = line.strip()
        matched = next((m for m in SHUTDOWN_MARKERS if m in stripped), None)
        if matched is None:
            continue
        elapsed: float | None = None
        head = stripped[:12]
        if base_date is not None and len(head) >= 8 and head[2] == ":" and head[5] == ":":
            try:
                clock = datetime.strptime(head.strip(), "%H:%M:%S.%f").time()
                stamp = datetime.combine(base_date, clock).timestamp()
                elapsed = round(stamp - t0_wall, 3) if t0_wall else None
                if elapsed is not None and elapsed < -1:
                    elapsed = None          # crossed midnight: refuse to guess
            except ValueError:
                elapsed = None
        out["markers"].append({"marker": matched, "line": stripped,
                               "elapsed_s": elapsed})
    return out


# =============================================================================
# Analysis
# =============================================================================


def diff_inventories(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """What the install window did to the install directory, by content."""
    out: dict[str, Any] = {
        "comparable": False, "reason": None,
        "unchanged": None, "changed": [], "removed": [], "added": [],
        "unhashable_before": before.get("unreadable") or [],
        "unhashable_after": after.get("unreadable") or [],
    }
    files_before = before.get("files")
    files_after = after.get("files")
    if files_before is None or files_after is None:
        out["reason"] = ("one side has no inventory: "
                         f"before_exists={before.get('exists')} "
                         f"after_exists={after.get('exists')}")
        return out
    out["comparable"] = True
    unchanged = 0
    for key, entry in files_before.items():
        later = files_after.get(key)
        if later is None:
            out["removed"].append(entry["path"])
            continue
        if entry["sha256"] is None or later["sha256"] is None:
            out["changed"].append({"path": entry["path"], "kind": "undetermined",
                                   "reason": "one side could not be hashed",
                                   "before_error": entry.get("error"),
                                   "after_error": later.get("error")})
        elif entry["sha256"] != later["sha256"]:
            out["changed"].append({"path": entry["path"], "kind": "content",
                                   "sha256_before": entry["sha256"],
                                   "sha256_after": later["sha256"]})
        else:
            unchanged += 1
    for key, entry in files_after.items():
        if key not in files_before:
            out["added"].append(entry["path"])
    out["unchanged"] = unchanged
    return out


def _answer(question: str, answer: Any, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"question": question, "answer": answer, "evidence": evidence}


def _undetermined(question: str, reason: str,
                  evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """The honest form. A verdict field that says `undetermined` and why is worth
    more than a confident one the harness did not measure, because this bundle
    gets quoted and a guess gets quoted with it."""
    return {"question": question, "answer": "undetermined", "reason": reason,
            "evidence": evidence or {}}


def compare_signatures(this: dict[str, Any],
                       others: list[dict[str, Any]]) -> dict[str, Any]:
    """Line this run's signature up against bundles from other `--from-version`
    runs. Without them there is nothing to compare and the questions that depend
    on a comparison say so."""
    rows = [this] + [o for o in others if o]
    rows = [r for r in rows if r.get("from_version")]
    rows.sort(key=lambda r: str(r.get("from_version")))
    return {"runs": rows, "count": len(rows)}


def run_signature(from_version: str, exit_code: int | None,
                  sampler: dict[str, Any], probe: dict[str, Any],
                  inno: dict[str, Any]) -> dict[str, Any]:
    """The few facts another run of this harness can be compared against."""
    owners_during = None
    if probe.get("timeline") is not None:
        owners_during = any(entry.get("owners") for entry in probe["timeline"])
    restarts = None
    if sampler.get("psutil_available"):
        restarts = [e for e in sampler.get("process_events") or []
                    if e.get("event") == "appeared"]
    return {
        "from_version": from_version,
        "installer_exit_code": exit_code,
        "inno_log_line_count": inno.get("line_count"),
        "rm_owner_observed_during_install": owners_during,
        "restart_observed_during_install": None if restarts is None else bool(restarts),
        "restart_count": None if restarts is None else len(restarts),
        "first_destructive_write_elapsed_s":
            (sampler.get("first_destructive_write") or {}).get("elapsed_s"),
    }


def build_verdict(*, args: argparse.Namespace, exit_code: int | None,
                  before: dict[str, Any], after: dict[str, Any],
                  sampler: dict[str, Any], probe: dict[str, Any],
                  inno: dict[str, Any], inno_timeline: dict[str, Any],
                  inventory_diff: dict[str, Any],
                  after_binary_version: str,
                  signature: dict[str, Any],
                  comparison: dict[str, Any]) -> dict[str, Any]:
    """The ten questions WP-R01 has to come back with answers to."""
    verdict: dict[str, Any] = {}

    # -- 1. who held the file -------------------------------------------
    q1 = "Which exact PID(s) retained _internal\\python312.dll or another install file?"
    during = [entry for entry in (probe.get("timeline") or []) if entry.get("owners")]
    before_rm = [r for r in (before.get("restart_manager", {}).get("records") or [])
                 if r.get("owners")]
    rm_errors = [r for r in (before.get("restart_manager", {}).get("records") or [])
                 if r.get("error")]
    if during:
        pids = sorted({owner["pid"] for entry in during for owner in entry["owners"]})
        verdict["locking_pids"] = _answer(q1, pids, {
            "source": "Restart Manager, sampled during the install window",
            "probe_interval_s": probe.get("interval_s"),
            "observations": during,
        })
    elif before_rm:
        pids = sorted({owner["pid"] for r in before_rm for owner in r["owners"]})
        verdict["locking_pids"] = _answer(q1, pids, {
            "source": "Restart Manager, PRE-INSTALL capture only",
            "caveat": "RM reported no owner during the install window at "
                      f"{probe.get('interval_s')}s cadence; these pids held the "
                      "files before the installer started and may have been "
                      "killed by it",
            "observations": before_rm,
        })
    elif not any(r.get("owners") is not None
                 for r in (before.get("restart_manager", {}).get("records") or [])):
        # NOT the same as "nothing held the files". Every probed file failed to
        # produce an owner LIST - no session, no registration, or no GetList -
        # so the question was never actually asked, and saying "no owner" here
        # would be reporting a failure to measure as a measurement.
        verdict["locking_pids"] = _undetermined(
            q1, "no probed file yielded a Restart Manager answer, so ownership "
                "was never measured - this is not evidence that nothing held them",
            {"errors": rm_errors[:10],
             "files_probed_before": before.get("restart_manager", {}).get("files_examined"),
             "rstrtmgr_load_error": _rstrtmgr_error})
    else:
        verdict["locking_pids"] = _answer(q1, "no_owner_observed", {
            "source": "Restart Manager",
            "meaning": "RM reported no holder for any probed file, before or "
                       "during the install",
            "files_probed_before": before.get("restart_manager", {}).get("files_examined"),
            "probe_targets": probe.get("files"),
            "probe_samples": probe.get("samples"),
        })

    # -- 2. what those processes are ------------------------------------
    q2 = "What are those processes - tray, supervisor, service, managed Qdrant, MCP, other?"
    owner_pids: set[int] = set()
    for entry in during or []:
        owner_pids.update(owner["pid"] for owner in entry["owners"])
    for record in before_rm:
        owner_pids.update(owner["pid"] for owner in record["owners"])
    if not owner_pids:
        verdict["lock_owner_roles"] = _undetermined(
            q2, "no owning pid was identified, so there is nothing to classify",
            {"depends_on": "locking_pids"})
    else:
        known: dict[int, dict[str, Any]] = {}
        for proc in (before.get("processes", {}).get("processes") or []):
            known[int(proc["pid"])] = proc
        for event in sampler.get("process_events") or []:
            proc = event.get("process") or {}
            if proc.get("pid") is not None and int(proc["pid"]) not in known:
                known[int(proc["pid"])] = proc
        roles = []
        for pid in sorted(owner_pids):
            proc = known.get(pid)
            rm_names = sorted({owner["app_name"]
                               for entry in (during or []) + before_rm
                               for owner in (entry.get("owners") or [])
                               if owner["pid"] == pid})
            if proc is None:
                roles.append({"pid": pid, "role": "undetermined",
                              "confidence": "none",
                              "reason": "no process record captured for this pid; "
                                        "it was gone before it could be inspected",
                              "restart_manager_app_names": rm_names})
                continue
            classification = proc.get("classification") or classify_process(
                proc.get("name"), proc.get("cmdline"))
            roles.append({"pid": pid, "name": proc.get("name"),
                          "exe": proc.get("exe"), "cmdline": proc.get("cmdline"),
                          "restart_manager_app_names": rm_names, **classification})
        verdict["lock_owner_roles"] = _answer(q2, roles, {
            "classifier": "image name plus command line; `confidence: name_only` "
                          "means the role was read off a filename and nothing else",
        })

    # -- 3. what the installer actually did to shut things down ---------
    q3 = "Which shutdown actions did the installer perform?"
    markers = inno_timeline.get("markers") or []
    disappearances = [e for e in sampler.get("process_events") or []
                      if e.get("event") == "disappeared"]
    if markers:
        verdict["installer_shutdown_actions"] = _answer(q3, markers, {
            "source": "the installer's own Inno log",
            "inno_log_line_count": inno.get("line_count"),
            "processes_that_vanished_during_install": disappearances,
        })
    elif (inno.get("line_count") or 0) <= 1:
        absent = inno.get("line_count") is None
        verdict["installer_shutdown_actions"] = _undetermined(
            q3,
            ("no Inno log was written at all, so the installer's own record of "
             f"its shutdown phase does not exist ({inno.get('error')})"
             if absent else
             f"the Inno log holds {inno.get('line_count')} line(s), so the "
             "installer's own record of its shutdown phase does not exist. Setup "
             "writes this file incrementally, so it stopped writing before it "
             "logged anything")
            + " - the sampler evidence below is all there is",
            {"inno_log_text": inno.get("text"),
             "inno_log_path": inno.get("path"),
             "processes_that_vanished_during_install": disappearances})
    else:
        verdict["installer_shutdown_actions"] = _answer(q3, "none_recorded", {
            "meaning": f"the Inno log has {inno.get('line_count')} lines and none "
                       "of them is a shutdown marker; a complete log would carry "
                       "the ForceKill entries",
            "processes_that_vanished_during_install": disappearances,
        })

    # -- 4. requested but not awaited -----------------------------------
    q4 = "Was shutdown requested but not awaited?"
    first_write = sampler.get("first_destructive_write")
    alive = sampler.get("alive_at_first_write")
    if first_write is None:
        verdict["shutdown_not_awaited"] = _undetermined(
            q4, "no file change was observed inside the install window, so there "
                "is no 'before the first write' to compare a shutdown against",
            {"sampler_samples": sampler.get("samples")})
    elif alive:
        verdict["shutdown_not_awaited"] = _answer(q4, "not_awaited", {
            "reasoning": "owned processes were still alive in the same sample as "
                         "the first destructive write, so whatever shutdown ran "
                         "had not taken effect when file modification began",
            "first_destructive_write": first_write,
            "alive_at_that_moment": alive,
            "inno_markers": markers,
        })
    else:
        verdict["shutdown_not_awaited"] = _answer(q4, "awaited_or_effective", {
            "reasoning": "no owned process was alive in the sample that first saw "
                         "a file change",
            "first_destructive_write": first_write,
            "sampling_interval_s": sampler.get("interval_s"),
            "caveat": "a process that died inside the sampling interval is "
                      "indistinguishable here from one that was never there",
        })

    # -- 5. a restart inside the window ---------------------------------
    q5 = "Did Task Scheduler or a supervisor restart a process during the install window?"
    if not sampler.get("psutil_available"):
        verdict["restart_during_install"] = _undetermined(
            q5, f"process sampling was unavailable: {sampler.get('psutil_error')}")
    else:
        appearances = [e for e in sampler.get("process_events") or []
                       if e.get("event") == "appeared"]
        if appearances:
            verdict["restart_during_install"] = _answer(q5, appearances, {
                "reading": "each entry is a (pid, create-time) pair absent from the "
                           "previous sample. `ppid` names the starter: a scheduler "
                           "host points at Task Scheduler, an owned image points at "
                           "a supervisor, the Setup process points at the installer",
                "scheduled_tasks_before": before.get("scheduled_tasks", {}).get("tasks"),
                "sampling_interval_s": sampler.get("interval_s"),
            })
        else:
            verdict["restart_during_install"] = _answer(q5, "none_observed", {
                "meaning": f"no process appearance in {sampler.get('samples')} "
                           f"samples at {sampler.get('interval_s')}s",
                "limit": "a process that started and exited inside one interval "
                         "would not appear here",
                "scheduled_tasks_before": before.get("scheduled_tasks", {}).get("tasks"),
            })

    # -- 6/7. cross-version comparisons ---------------------------------
    q6 = "Why do 2.7.0 and 3.0.1 pass?"
    q7 = "Did v3.3.0 introduce the relevant lifecycle behaviour?"
    runs = comparison.get("runs") or []
    versions = [r.get("from_version") for r in runs]
    if len(runs) < 2:
        hint = ("run this harness with --from-version 2.7.0 and --from-version "
                "3.0.1 (and 3.2.0 / 3.3.0), then pass those bundles with "
                "--compare; the comparison is mechanical once two runs exist")
        verdict["why_earlier_versions_pass"] = _undetermined(
            q6, f"this run observed only --from-version {signature['from_version']}; "
                f"one run cannot compare versions. {hint}",
            {"this_run": signature})
        verdict["v330_introduced_behaviour"] = _undetermined(
            q7, f"same reason: only {signature['from_version']} was observed. {hint}",
            {"this_run": signature})
    else:
        passing = [r for r in runs if r.get("installer_exit_code") == 0]
        failing = [r for r in runs if r.get("installer_exit_code") not in (0, None)]
        verdict["why_earlier_versions_pass"] = _answer(q6, {
            "passing_from_versions": [r["from_version"] for r in passing],
            "failing_from_versions": [r["from_version"] for r in failing],
            "difference": [
                {"from_version": r["from_version"],
                 "exit_code": r["installer_exit_code"],
                 "rm_owner_observed_during_install": r.get("rm_owner_observed_during_install"),
                 "restart_observed_during_install": r.get("restart_observed_during_install"),
                 "inno_log_line_count": r.get("inno_log_line_count")}
                for r in runs],
        }, {"source": "signatures from this run plus every --compare bundle",
            "versions_compared": versions})
        boundary = None
        for run_row in runs:
            if run_row.get("installer_exit_code") not in (0, None):
                boundary = run_row["from_version"]
                break
        if boundary is None:
            verdict["v330_introduced_behaviour"] = _undetermined(
                q7, "no compared run failed, so no boundary version is visible",
                {"versions_compared": versions})
        else:
            verdict["v330_introduced_behaviour"] = _answer(q7, {
                "first_failing_from_version": boundary,
                "claim_supported": boundary == "3.3.0",
            }, {"versions_compared": versions,
                "caveat": "this identifies the earliest COMPARED from-version that "
                          "fails. It is only evidence about 3.3.0 if a version "
                          "below it was compared and passed",
                "runs": runs})

    # -- 8. writing before quiescence -----------------------------------
    q8 = "Did the installer begin modifying files before proving quiescence?"
    shutdown_times = [m["elapsed_s"] for m in markers if m.get("elapsed_s") is not None]
    if first_write is None:
        verdict["wrote_before_quiescence"] = _undetermined(
            q8, "no first write was observed, so there is no timestamp to compare")
    elif alive:
        verdict["wrote_before_quiescence"] = _answer(q8, "yes", {
            "first_write_elapsed_s": first_write.get("elapsed_s"),
            "first_write": first_write,
            "processes_still_alive_then": alive,
            "shutdown_marker_times_s": shutdown_times or None,
            "reasoning": "the machine was not quiescent when the first destructive "
                         "write landed: named processes were still running",
        })
    else:
        verdict["wrote_before_quiescence"] = _answer(q8, "not_observed", {
            "first_write_elapsed_s": first_write.get("elapsed_s"),
            "shutdown_marker_times_s": shutdown_times or None,
            "reasoning": "nothing owned was alive in the sample that first saw a "
                         "write; within the sampler's resolution the machine was "
                         "quiescent first",
            "limit": f"resolution is {sampler.get('interval_s')}s",
        })

    # -- 9. rollback fidelity -------------------------------------------
    q9 = "Did rollback restore a byte-consistent previous installation?"
    runs_at_all = args.from_version in (after_binary_version or "")
    if not inventory_diff.get("comparable"):
        verdict["rollback_byte_consistent"] = _undetermined(
            q9, f"the two inventories are not comparable: {inventory_diff.get('reason')}",
            {"binary_version_after": after_binary_version})
    elif inventory_diff["changed"] or inventory_diff["removed"] or inventory_diff["added"]:
        verdict["rollback_byte_consistent"] = _answer(q9, "no", {
            "changed": inventory_diff["changed"][:50],
            "changed_count": len(inventory_diff["changed"]),
            "removed_count": len(inventory_diff["removed"]),
            "removed_sample": inventory_diff["removed"][:50],
            "added_count": len(inventory_diff["added"]),
            "added_sample": inventory_diff["added"][:50],
            "unchanged_count": inventory_diff["unchanged"],
            "binary_version_after": after_binary_version,
            "previous_version_still_runs": runs_at_all,
        })
    elif inventory_diff["unhashable_before"] or inventory_diff["unhashable_after"]:
        verdict["rollback_byte_consistent"] = _undetermined(
            q9, "every comparable file matched, but some files could not be hashed "
                "on one side or the other, so 'byte-consistent' is not proven",
            {"unhashable_before": inventory_diff["unhashable_before"][:20],
             "unhashable_after": inventory_diff["unhashable_after"][:20],
             "binary_version_after": after_binary_version})
    elif not inventory_diff["unchanged"]:
        # Zero differences across zero files is not a clean rollback, it is an
        # empty directory agreeing with an empty directory. Answering "yes" here
        # would let a wiped installation read as a perfectly restored one.
        verdict["rollback_byte_consistent"] = _undetermined(
            q9, "no files were compared on either side - there is nothing here to "
                "be byte-consistent WITH",
            {"install_dir_exists_before": before.get("install_dir_exists"),
             "install_dir_exists_after": after.get("install_dir_exists"),
             "binary_version_after": after_binary_version})
    else:
        verdict["rollback_byte_consistent"] = _answer(q9, "yes", {
            "files_compared": inventory_diff["unchanged"],
            "binary_version_after": after_binary_version,
            "previous_version_still_runs": runs_at_all,
            "meaning": "every installed file has the same SHA-256 it had before "
                       "the candidate installer ran",
        })

    # -- 10. user data ---------------------------------------------------
    q10 = "Is the user's data unchanged?"
    data_diff = []
    undetermined_data = []
    for label in sorted(set(before["user_data"]["artefacts"]) |
                        set(after["user_data"]["artefacts"])):
        first = before["user_data"]["artefacts"].get(label, {})
        second = after["user_data"]["artefacts"].get(label, {})
        if first.get("sha256") is None or second.get("sha256") is None:
            undetermined_data.append({"artefact": label,
                                      "before": first.get("sha256"),
                                      "after": second.get("sha256"),
                                      "before_error": first.get("error"),
                                      "after_error": second.get("error")})
        elif first["sha256"] != second["sha256"]:
            data_diff.append({"artefact": label, "before": first["sha256"],
                              "after": second["sha256"]})
    qdrant_before = before["user_data"]["qdrant"]
    qdrant_after = after["user_data"]["qdrant"]
    if qdrant_before.get("digest") is None or qdrant_after.get("digest") is None:
        undetermined_data.append({"artefact": "qdrant_storage",
                                  "before": qdrant_before.get("digest"),
                                  "after": qdrant_after.get("digest"),
                                  "before_error": qdrant_before.get("error"),
                                  "after_error": qdrant_after.get("error")})
    elif qdrant_before["digest"] != qdrant_after["digest"]:
        data_diff.append({"artefact": "qdrant_storage",
                          "before": qdrant_before["digest"],
                          "after": qdrant_after["digest"],
                          "file_count_before": qdrant_before.get("file_count"),
                          "file_count_after": qdrant_after.get("file_count")})
    attribution = ("the previous release's service was RUNNING across this window "
                   "and legitimately writes its own config and databases, so a "
                   "changed digest is not automatically the installer's doing")
    if data_diff:
        verdict["user_data_unchanged"] = _answer(q10, "changed", {
            "changed": data_diff, "undetermined": undetermined_data,
            "attribution_caveat": attribution,
            "severity": "a broken binary install that also lost user data is a "
                        "different and worse bug than one that only broke binaries",
        })
    elif undetermined_data:
        verdict["user_data_unchanged"] = _undetermined(
            q10, "some artefacts could not be digested on one side or the other",
            {"undetermined": undetermined_data, "attribution_caveat": attribution})
    else:
        verdict["user_data_unchanged"] = _answer(q10, "unchanged", {
            "artefacts": sorted(before["user_data"]["artefacts"]),
            "qdrant_digest": qdrant_after.get("digest"),
            "attribution_caveat": attribution,
        })

    return verdict


# =============================================================================
# Refusal
# =============================================================================


def disposable_runner_refusal(args: argparse.Namespace) -> list[str]:
    """Why this machine must not be used. Empty list means it may be.

    Checked BEFORE anything is downloaded or installed, because the first
    destructive act is the previous release's own installer and there is no
    undo after it.
    """
    reasons: list[str] = []
    if not args.i_am_a_disposable_runner:
        reasons.append(
            "--i-am-a-disposable-runner was not passed. This harness installs a "
            "real published release at the product's fixed AppId and then runs a "
            "second installer over it, which force-kills every owned process and "
            "deletes %LOCALAPPDATA%\\Programs\\RAGTools\\_internal. On a developer "
            "machine that is the live installation, not a fixture.")
    ci = os.environ.get("CI", "")
    if ci.strip().lower() != "true":
        reasons.append(
            f"CI is {ci!r}, not 'true'. The flag alone is one typo away from being "
            "meant, so a second, independent signal is required: a hosted runner "
            "sets CI=true and a developer shell does not.")
    return reasons


# =============================================================================
# Orchestration
# =============================================================================


def _mirror_inno_log_path(log_dir: Path, label: str) -> Path:
    """Where `install_silently` puts Inno's log for `label`.

    The formula is OWNED by `verify_upgrade_install.install_silently`; it is
    mirrored here because this harness has to read the file that function
    writes, and editing that file is out of scope for this change. If the two
    ever drift, the symptom is a bundle reporting "Inno wrote no log" - which is
    also a real finding, so the mirroring is stated rather than left to be
    inferred.
    """
    return log_dir / f"inno-{label.split()[0]}.log"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--installer", required=True,
                        help="the candidate installer to run over the previous release")
    parser.add_argument("--version", required=True,
                        help="the version that installer should install")
    parser.add_argument("--from-version", required=True,
                        help="published release to install first, e.g. 3.3.0")
    parser.add_argument("--out", required=True,
                        help="where to write the JSON evidence bundle")
    parser.add_argument("--work", default="upgrade-work", help="scratch directory")
    parser.add_argument("--port", type=int, default=21420, help="service port to read")
    parser.add_argument("--sample-interval", type=float, default=0.25,
                        help="seconds between process/file samples during the install")
    parser.add_argument("--lock-probe-interval", type=float, default=2.0,
                        help="seconds between Restart Manager probes during the install")
    parser.add_argument("--rm-file-limit", type=int, default=2000,
                        help="cap on files given a Restart Manager session per capture")
    parser.add_argument("--max-events", type=int, default=20000,
                        help="cap on recorded sampler events")
    parser.add_argument("--compare", action="append", default=[],
                        help="a bundle from another --from-version run; repeatable. "
                             "Questions 6 and 7 are comparisons and cannot be "
                             "answered from one run")
    parser.add_argument("--i-am-a-disposable-runner", action="store_true",
                        help="acknowledge that this machine's RAGTools installation "
                             "and data may be destroyed")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        # Returns 0, like its sibling: a cross-platform lint or import job is not
        # made red by a platform this harness was never meant to run on. It
        # measures the Windows Restart Manager and Windows scheduled tasks; there
        # is no degraded mode worth having.
        print("Windows only: this harness measures the Windows Restart Manager, "
              "Task Scheduler and a packaged Inno installer. Nothing was done.",
              flush=True)
        return 0

    refusal = disposable_runner_refusal(args)
    if refusal:
        print("REFUSING TO RUN. Nothing has been downloaded, installed or "
              "modified.\n", flush=True)
        for reason in refusal:
            print(f"  * {reason}", flush=True)
        print("\nRe-run on a disposable Windows runner with CI=true and "
              "--i-am-a-disposable-runner.", flush=True)
        return 2

    candidate = Path(args.installer).resolve()
    if not candidate.is_file():
        check("the candidate installer exists", False, str(candidate))
        return 3

    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    bundle: dict[str, Any] = {
        "harness": "scripts/diagnose_upgrade_lock.py",
        "work_package": "WP-R01",
        "started_utc": _iso(time.time()),
        "repo": REPO,
        "arguments": {k: (str(v) if isinstance(v, Path) else v)
                      for k, v in vars(args).items()},
        "environment": {
            "platform": sys.platform,
            "python": sys.version,
            "ci": os.environ.get("CI"),
            "runner_os": os.environ.get("RUNNER_OS"),
            "github_run_id": os.environ.get("GITHUB_RUN_ID"),
            "github_job": os.environ.get("GITHUB_JOB"),
            "psutil_version": getattr(psutil, "__version__", None),
            "psutil_error": PSUTIL_UNAVAILABLE,
            "default_install_dir": str(INSTALL_DIR),
            "data_dir": str(DATA_DIR),
            "rm_process_info_struct_size": RM_PROCESS_INFO_SIZE,
            "rm_process_info_struct_expected": RM_PROCESS_INFO_EXPECTED_SIZE,
        },
    }
    check("RM_PROCESS_INFO has the documented layout",
          RM_PROCESS_INFO_SIZE == RM_PROCESS_INFO_EXPECTED_SIZE,
          f"sizeof={RM_PROCESS_INFO_SIZE} expected={RM_PROCESS_INFO_EXPECTED_SIZE}")
    check("psutil is importable", psutil is not None,
          PSUTIL_UNAVAILABLE or f"psutil {getattr(psutil, '__version__', '?')}")

    # --- Phase A: a genuine previous installation ------------------------
    print(f"\n=== Phase A: install the published v{args.from_version} ===", flush=True)
    previous_label = f"v{args.from_version}"
    try:
        previous = download_previous(args.from_version, work)
    except RuntimeError as exc:
        check(f"downloaded the published v{args.from_version} installer", False, str(exc))
        return 3
    rc_previous = install_silently(previous, previous_label, log_dir=work)
    bundle["phase_a"] = {
        "installer": str(previous),
        "exit_code": rc_previous,
        "inno_log": read_inno_log(_mirror_inno_log_path(work, previous_label)),
    }
    if not check(f"the v{args.from_version} installer exited 0", rc_previous == 0,
                 f"exit {rc_previous}"):
        return 3

    install = installed_dir()
    print(f"    install directory (per registry): {install}", flush=True)
    before_binary = binary_version(install / "rag.exe")
    check(f"the previous release reports {args.from_version}",
          args.from_version in before_binary, before_binary)

    # Start it, so its binaries are LOCKED when the candidate installer runs.
    # That is the condition the failure occurs under; measuring a quiet machine
    # would measure nothing.
    run([str(install / "rag.exe"), "service", "start"], timeout=600)
    serving = health(port=args.port)
    check("the previous release is serving", bool(serving),
          json.dumps(serving)[:300] if serving else "no /health answer")
    bundle["phase_a"]["binary_version"] = before_binary
    bundle["phase_a"]["registry_version"] = registry_version()
    bundle["phase_a"]["serving"] = serving

    # --- Phase B: instrument BEFORE touching anything --------------------
    print("\n=== Phase B: the machine before the upgrade ===", flush=True)
    before = capture_state("before", install, args.port, rm_file_limit=args.rm_file_limit)
    bundle["before"] = before
    holders = [r for r in before["restart_manager"]["records"] if r["owners"]]
    check("the Restart Manager names at least one file holder before the upgrade",
          bool(holders),
          f"{len(holders)} file(s) with a named owner"
          if holders else "no holders - the previous release may not be running")

    # --- Phase C: the candidate installer, instrumented ------------------
    print("\n=== Phase C: the candidate installer, under a sampler ===", flush=True)
    probe_targets = lock_probe_targets(install)
    print(f"    Restart Manager probe targets: "
          f"{[p.name for p in probe_targets] or 'none found'}", flush=True)
    sampler = InstallSampler(install, args.sample_interval, args.max_events)
    prober = LockProbe(probe_targets, args.lock_probe_interval)
    label = f"v{args.version} (over v{args.from_version})"
    log_path = _mirror_inno_log_path(work, label)

    # Baseline BEFORE the threads and before the installer, so t=0 is the
    # installer's start and not "whenever the first full process inventory
    # happened to finish".
    sampler.prime()
    print(f"    sampler primed in {sampler.prime_duration_s}s: "
          f"{len(sampler._owned)} owned process(es), "
          f"{len(sampler._last_files or {})} file(s) under watch", flush=True)
    sampler.start()
    prober.start()
    # Let the threads reach their first sample so t=0 is real before the spawn.
    for _ in range(200):
        if sampler.t0_monotonic is not None:
            break
        time.sleep(0.01)
    install_started_wall = time.time()
    install_started = time.monotonic()
    exit_code: int | None = None
    install_error: str | None = None
    try:
        exit_code = install_silently(candidate, label, log_dir=work)
    except subprocess.TimeoutExpired as exc:
        install_error = f"the installer did not return: {exc}"
        print(f"    {install_error}", flush=True)
    finally:
        install_elapsed = round(time.monotonic() - install_started, 3)
        sampler.stop()
        prober.stop()
        sampler.join(timeout=30)
        prober.join(timeout=30)

    inno = read_inno_log(log_path)
    inno_timeline = parse_inno_timeline(inno, install_started_wall)
    fallback_logs = temp_setup_logs(install_started_wall)

    bundle["phase_c"] = {
        "installer": str(candidate),
        "installer_argv_source":
            "verify_upgrade_install.install_silently - /VERYSILENT "
            "/SUPPRESSMSGBOXES /NORESTART /TASKS=addtopath,startup /LOG=<path>",
        "exit_code": exit_code,
        "exit_code_meaning": {
            0: "success",
            1: "Setup failed to initialise",
            2: "the user cancelled before installation started (auto-answered)",
            3: "a fatal error occurred while preparing to install",
            4: "a fatal error occurred during installation",
            5: "cancelled DURING installation, or Abort chosen at an "
               "Abort-Retry-Ignore box - which is what /SUPPRESSMSGBOXES answers",
            6: "Setup was cancelled by the debugger",
            7: "the preparing-to-install stage decided Setup cannot proceed",
            8: "the preparing-to-install stage decided a restart is required",
        }.get(exit_code if exit_code is not None else -1, "unrecognised"),
        "error": install_error,
        "elapsed_s": install_elapsed,
        "started_utc": _iso(install_started_wall),
        # Stated, not assumed to be zero. Every `elapsed_s` in the sampler is
        # measured from sampling start; this is how far the installer's own
        # start sits from it.
        "installer_spawn_offset_s":
            round(install_started - sampler.t0_monotonic, 4)
            if sampler.t0_monotonic is not None else None,
        "inno_log": inno,
        "inno_timeline": inno_timeline,
        "inno_fallback_logs_in_temp": fallback_logs,
        "sampler": sampler.report(),
        "lock_probe": prober.report(),
    }
    check("the candidate installer exited 0", exit_code == 0,
          f"exit {exit_code} - "
          + str(bundle['phase_c']['exit_code_meaning']))
    check("the Inno log holds more than one line",
          (inno.get("line_count") or 0) > 1,
          f"{inno.get('line_count')} line(s)"
          + (f"; fallback logs in TEMP: {len(fallback_logs)}" if fallback_logs else ""))
    first_write = sampler.first_destructive_write
    check("a destructive write was observed inside the install window",
          first_write is not None,
          f"first at +{first_write['elapsed_s']}s: {first_write['change']} "
          f"{first_write['path']}" if first_write else "none seen")
    appearances = [e for e in sampler.process_events if e["event"] == "appeared"]
    check("no owned process appeared mid-install", not appearances,
          "; ".join(f"+{e['elapsed_s']}s pid={e['process']['pid']} "
                    f"{e['process']['name']}" for e in appearances) or "none")

    # --- Phase D: the after-state ----------------------------------------
    print("\n=== Phase D: the machine after the upgrade ===", flush=True)
    install_after = installed_dir()
    check("the install directory did not move",
          _normcase(install_after) == _normcase(install),
          str(install_after) if _normcase(install_after) == _normcase(install)
          else f"{install} -> {install_after}")
    after = capture_state("after", install_after, args.port,
                          rm_file_limit=args.rm_file_limit)
    bundle["after"] = after

    after_binary = binary_version(install_after / "rag.exe")
    check(f"rag.exe reports {args.version}", args.version in after_binary, after_binary)
    check("the uninstall registry entry moved to the candidate version",
          registry_version() == args.version,
          f"registry reads {registry_version()}")

    inventory_diff = diff_inventories(before["install_inventory"],
                                      after["install_inventory"])
    bundle["install_directory_diff"] = inventory_diff
    bundle["after_binary_version"] = after_binary

    # --- the verdict ------------------------------------------------------
    signature = run_signature(args.from_version, exit_code,
                              bundle["phase_c"]["sampler"],
                              bundle["phase_c"]["lock_probe"], inno)
    others: list[dict[str, Any]] = []
    for path in args.compare:
        try:
            other = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"    could not read --compare bundle {path}: {exc}", flush=True)
            continue
        record = (other or {}).get("signature")
        if not isinstance(record, dict) or not record.get("from_version"):
            # Said out loud. A bundle silently dropped here turns questions 6
            # and 7 into "undetermined" for a reason the operator never sees,
            # and they would reasonably conclude the comparison is unsupported.
            print(f"    --compare bundle {path} carries no usable 'signature' "
                  "block; it will not take part in the comparison", flush=True)
            continue
        record = dict(record)
        record["source_bundle"] = path
        others.append(record)
    comparison = compare_signatures(signature, others)

    bundle["signature"] = signature
    bundle["comparison"] = comparison
    bundle["verdict"] = build_verdict(
        args=args, exit_code=exit_code, before=before, after=after,
        sampler=bundle["phase_c"]["sampler"], probe=bundle["phase_c"]["lock_probe"],
        inno=inno, inno_timeline=inno_timeline, inventory_diff=inventory_diff,
        after_binary_version=after_binary, signature=signature,
        comparison=comparison)
    bundle["observations"] = [{"name": n, "ok": ok, "detail": d} for n, ok, d in results]
    bundle["finished_utc"] = _iso(time.time())

    out_path.write_text(json.dumps(bundle, indent=2, default=str), encoding="utf-8")

    # The Inno logs go beside the bundle as their own files too. A JSON string is
    # awkward to read at the exact moment someone needs to read it.
    for name, record in (("candidate", inno), ("previous", bundle["phase_a"]["inno_log"])):
        if record.get("text") is not None:
            sidecar = out_path.with_suffix(f".inno-{name}.log")
            sidecar.write_text(record["text"], encoding="utf-8")
    for index, record in enumerate(fallback_logs):
        sidecar = out_path.with_suffix(f".inno-temp-{index}.log")
        sidecar.write_text(record.get("text") or "", encoding="utf-8")

    print(f"\n  evidence bundle: {out_path}", flush=True)
    print("\n--- verdict ---", flush=True)
    for key, entry in bundle["verdict"].items():
        answer = entry["answer"]
        rendered = (json.dumps(answer, default=str)
                    if not isinstance(answer, str) else answer)
        if len(rendered) > 160:
            rendered = rendered[:157] + "..."
        print(f"  {key}: {rendered}", flush=True)
        if entry["answer"] == "undetermined":
            print(f"      reason: {entry.get('reason')}", flush=True)

    failed = [r for r in results if not r[1]]
    print(f"\n  {len(results) - len(failed)} observation(s) held, "
          f"{len(failed)} did not", flush=True)
    print(json.dumps({
        "bundle": str(out_path),
        "from_version": args.from_version,
        "candidate_version": args.version,
        "installer_exit_code": exit_code,
        "inno_log_line_count": inno.get("line_count"),
        "first_destructive_write_elapsed_s":
            (first_write or {}).get("elapsed_s"),
        "restarts_observed": len(appearances),
        "observations_held": len(results) - len(failed),
        "observations_failed": len(failed),
        "verdict": {k: v["answer"] for k, v in bundle["verdict"].items()},
    }, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
