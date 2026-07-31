# WP-R01 — reproduction of the packaged v3.3.0 → v3.5.x upgrade failure

**Status:** failure REPRODUCED independently, on a disposable machine, from genuine
packaged artifacts. Lock-ownership forensics pending (`scripts/diagnose_upgrade_lock.py`).

**Date:** 2026-08-01
**Reproduced on:** GitHub-hosted `windows-latest`, run `30666102565`, job `91273392455`
**Full log preserved at:** `.claude/evidence/r01/job-91273392455-full.log` (3.16 MB)
**Job metadata preserved at:** `.claude/evidence/r01/job-91273392455-before-cancel.json`

---

## 1. Why this section exists

The v3.5.0 release shipped with an advisory whose root cause was *inferred from a
field log*. Inference is not reproduction. This records the point at which the
failure was produced from scratch, on a machine nobody had touched, from the
published v3.3.0 installer and a candidate built in the same job — so that the
fix in WP-R02 is validated against a real failing case rather than a story about
one.

## 2. The reproduction

Verbatim, from the preserved log:

```
>>> installing v3.3.0: RAGTools-Setup-3.3.0.exe
    exit=0
--- the machine before the upgrade ---
  [PASS] previous release installed (3.3.0) - ragtools v3.3.0
  [PASS] previous uninstall registry entry - 3.3.0
  [PASS] previous release is serving - {"status": "ready", "collection": "markdown_kb",
         "version": "3.3.0", "storage_reachable": true,
         "engine": {"state": "ready", "pid": 1264, "detail": "managed engine ready on
         http://127.0.0.1:21500"}, "storage_backend": "managed",
         "collection_strategy": "per_project", "config_version": 3}
  [PASS] previous release has live processes holding its binaries - 3 process(es)
>>> installing v3.5.0 (over v3.3.0): RAGTools-Setup-3.5.0.exe
    exit=5
--- Inno log for v3.5.0 (over v3.3.0) (last 120 of 1 lines) ---
--- end Inno log ---
  [FAIL] the upgrade installer exited 0 - exit 5
--- the machine after the upgrade ---
  [FAIL] rag.exe reports 3.5.0 - [PYI-2072:ERROR] Failed to load Python DLL
         'C:\Users\runneradmin\AppData\Local\Programs\RAGTools\_internal\python312.dll'.
  [PASS] ragw.exe was installed
  [FAIL] uninstall registry entry updated - registry reads 3.3.0
  [PASS] scheduled tasks point into the install directory - {'Tray': '...\ragw.exe'}
  [FAIL] the service answers after the upgrade - nothing responded within 300s
```

Every symptom from the field report is present: **exit 5**, an unloadable
`_internal\python312.dll`, an uninstall registry entry still reading 3.3.0, and a
service that never returns.

## 3. What the comparison legs prove

All three legs ran in the same workflow run, on identical runner images, against
the same candidate installer. Only the previous version differed.

| leg | `storage_backend` | `collection_strategy` | live procs | installer exit |
|---|---|---|---|---|
| v2.7.0 | *(field predates the release)* | *(field predates)* | 2 | **0** — pass |
| v3.0.1 | `embedded` | `shared` | 3 | **0** — pass |
| v3.3.0 | **`managed`** | `per_project` | 3 | **5** — fail |

This answers two of the ten required questions with primary evidence:

**Q6 — why did genuine v2.7.0 and v3.0.1 upgrades pass?** Because neither runs a
supervised native engine process. v3.0.1 is `embedded`: Qdrant is in-process
Python, so stopping `rag.exe`/`ragw.exe` stops everything the installation owns.

**Q7 — did v3.3.0 introduce the relevant lifecycle behaviour?** Yes. v3.3.0 is the
first release whose installation supervises a **separate, long-lived native child
process** (`qdrant.exe`, managed engine, pid 1264 on this run). The upgrade
lifecycle it inherits was designed when the only owned images were `rag.exe` and
`ragw.exe`.

## 4. The engine was never stopped — GitHub's own evidence

At job teardown, the Actions runner reported:

```
Cleaning up orphan processes
Terminate orphan process: pid (1264) (qdrant)
```

Pid 1264 is the same pid the pre-upgrade `/health` payload named as the managed
engine. **It survived the entire upgrade and the remainder of the job.** The
installer's scoped `qdrant.exe` stop did not stop it.

## 5. The mechanism, from reading `installer.iss`

Confirmed by source, not by inference about behaviour:

- `CloseApplications=no` (line 74) — Restart Manager is deliberately disabled, so
  Inno performs no file-in-use detection of its own.
- `CurStepChanged(ssInstall)` runs the graceful stop, `StopOwnedTasks()`, then
  `ForceKillRagProcesses()`. `ForceKillRagProcesses` logs how many owned processes
  survived — **and then proceeds regardless.** There is no path by which it can
  refuse.
- `StopOwnedTasks()` issues `schtasks /end`, which stops a running instance but
  leaves the trigger armed. The registration carries `RestartOnFailure`, and a
  force-kill is exactly the failure it reacts to.
- `[InstallDelete]` then removes `{app}\_internal` wholesale. **This is the first
  destructive write, and it happens after `ssInstall`.**
- A file under `_internal` that is still held raises Inno's file-in-use
  Abort/Retry/Ignore box. `/SUPPRESSMSGBOXES` answers Abort/Retry/Ignore with
  **Abort**. Setup exits **5** with `_internal` partially deleted.

That sequence accounts for every observed symptom, including the otherwise odd
one: the Inno log contains **exactly one line**, because Setup died in the
delete phase before it had written anything else.

## 6. What is NOT yet proven

These remain open until `scripts/diagnose_upgrade_lock.py` runs in the
instrumented CI leg. They are listed so nothing here is mistaken for a finished
answer:

1. the exact PID(s) retaining `_internal\python312.dll` — **undetermined**;
2. what those processes are (tray / supervisor / service / engine / MCP) — **undetermined**;
3. which shutdown actions actually executed, from the installer's own log — **undetermined** (the 1-line log tells us when it died, not what it did);
4. whether shutdown was requested but not awaited — **undetermined**;
5. whether Task Scheduler restarted a process inside the install window — **undetermined**;
8. whether the first destructive write preceded proof of quiescence — *strongly indicated* by §5, not yet measured;
9. whether rollback restores a byte-consistent, runnable v3.3.0 — **undetermined**;
10. whether user data is unchanged — **undetermined** (the post-upgrade service never answered, so nothing read it).

**No item above may be quoted as a finding until measured.** The reproduction
establishes the failure and its version boundary; it does not establish the lock
owner.

## 7. Consequence for the release

WP-R02's acceptance invariant follows directly from §5: the installer must prove
quiescence **before** `[InstallDelete]` runs, and must be able to refuse. Inno's
`PrepareToInstall()` executes before any file operation and, by returning a
message, fails Setup with exit code **7** having written nothing — giving the
product a distinct, honest outcome for *"refused safely, your installation is
untouched"* as against exit 5's *"aborted mid-write, your installation is now
mixed"*. Today a user cannot tell those apart.
