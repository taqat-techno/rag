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

## 5a. ANSWERED — the lock owner, measured

**Measured 2026-08-01**, run `30676422208`, job `packaged v3.3.0 -> this build`, via the
Windows Restart Manager and an independent psutil module sweep. Both name one process:

```
bin\qdrant.exe: HELD BY 1 process(es)
  {"pid": 2720, "app_name": "qdrant", "app_type_name": "RmConsole", "restartable": false}

processes whose image OR a loaded module lies under <install> (1):
  {"pid": 2720, "ppid": 816, "name": "qdrant.exe",
   "exe": "...\Programs\RAGTools\bin\qdrant.exe",
   "cmdline": ["...\bin\qdrant.exe", "--config-path", "...\RAGTools\data\qdrant-config.yaml"],
   "holds": [{"kind": "exe",    "path": "...\bin\qdrant.exe"},
             {"kind": "module", "path": "...\bin\qdrant.exe"},
             {"kind": "module", "path": "...\_internal\msvcp140.dll"},
             {"kind": "module", "path": "...\_internal\vcruntime140_1.dll"},
             {"kind": "module", "path": "...\_internal\vcruntime140.dll"}],
   "classification": {"role": "managed_qdrant", "confidence": "high"}}
```

**Q1 — which PID retains a file under the installation:** pid 2720.
**Q2 — what it is:** the managed Qdrant engine. Not the tray, not the supervisor, not an
MCP server — those are stopped by the existing image-name kill. This one is a *native*
process the upgrade lifecycle was never written to account for.

**The mechanism, in one sentence.** `qdrant.exe` lives at `<install>\bin\` and loads the
**MSVC runtime out of `<install>\_internal\`** — the PyInstaller payload directory —
because the loader resolves `msvcp140.dll` / `vcruntime140.dll` / `vcruntime140_1.dll`
from the application directory. `[InstallDelete]` removes `{app}\_internal` *wholesale*.
So the delete meets a DLL held by a process nothing in the installer was stopping, raises
the file-in-use Abort/Retry/Ignore box, and `/SUPPRESSMSGBOXES` answers **Abort** — exit 5,
`_internal` half-deleted, `python312.dll` gone.

**Q6 and Q7 now follow mechanically rather than by correlation.** v3.0.1 is `embedded`:
Qdrant is in-process Python, there is no native child, nothing outside `rag.exe`/`ragw.exe`
holds `_internal`, and stopping those two is sufficient — so it passes. v3.3.0 introduced
the supervised managed engine, and with it the first installation-owned process the kill
does not target. The boundary is not a coincidence of timing; it is the feature.

Confirmed independently by the same run's outcome pattern: with the quiescence gate in
place, **2.7.0 and 3.0.1 pass and 3.3.0 / 3.4.0 / 3.5.0 refuse**, and the refusal names
exactly those four files.

## 6. What is NOT yet proven

Superseded in part by §5a. Remaining state, honestly:

1. **ANSWERED** — pid 2720, the managed Qdrant engine (§5a).
2. **ANSWERED** — `managed_qdrant`, high confidence, by image + path + loaded modules (§5a).
3. which shutdown actions the *v3.5.0* installer executed, from its own log — **undetermined**, and likely to stay so: that log was one line because Setup died in the delete phase before writing more. The equivalent question for the *current* installer is answered by the structured quiescence verdict.
4. whether shutdown was requested but not awaited — **undetermined for v3.5.0**; for the current build the verdict records every phase with its outcome and duration.
5. whether Task Scheduler restarted a process inside the install window — **not observed**. The sampler recorded no process appearing during the window. Absence of an observation at a 0.31 s sampling interval is weaker than a proof, and is reported as `none_observed`, not as `no`.
8. whether the first destructive write preceded proof of quiescence — **ANSWERED for v3.5.0: yes**, necessarily. `ForceKillRagProcesses` logged survivors and returned; `[InstallDelete]` ran next. There was no code path that could refuse. That is the defect WP-R02 removes.
9. whether rollback restores a byte-consistent, runnable v3.3.0 — **partially**. The refusal path leaves the previous installation untouched and *demonstrably* runnable: the same job records `[PASS] the service answers after the upgrade — status=ready version=3.3.0`. Rollback *after* a partial write is implemented and unit-tested but has not been exercised against a real half-written tree.
10. whether user data is unchanged — **not measured under the v3.5.0 failure**, because the post-upgrade service never answered and nothing read it. Under the current build the refusal writes nothing at all, so the question does not arise on that path.

**Nothing above is quoted as a finding unless it is marked ANSWERED.** Items 3, 4 and 10
are properties of a failure mode this release removes; they are recorded as open rather
than quietly dropped, because "we fixed it so the question no longer matters" is not the
same as "we know the answer".

## 7. Consequence for the release

WP-R02's acceptance invariant follows directly from §5: the installer must prove
quiescence **before** `[InstallDelete]` runs, and must be able to refuse. Inno's
`PrepareToInstall()` executes before any file operation and, by returning a
message, fails Setup with exit code **7** having written nothing — giving the
product a distinct, honest outcome for *"refused safely, your installation is
untouched"* as against exit 5's *"aborted mid-write, your installation is now
mixed"*. Today a user cannot tell those apart.
