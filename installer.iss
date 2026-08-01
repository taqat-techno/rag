; RAG Tools Installer — Inno Setup Script
; Builds a Windows installer from PyInstaller output.
;
; Prerequisites:
;   - PyInstaller build completed: dist\rag\rag.exe exists
;   - Inno Setup 6+ installed
;
; Usage:
;   iscc installer.iss                          ; uses the version below
;   iscc /DMyAppVersion=3.0.0-rc.3 installer.iss ; CI overrides from the tag

#define MyAppName "RAG Tools"
; Overridable so the installer filename always tracks the TAG being built.
; OutputBaseFilename derives from this, and the release workflow uploads
; `RAGTools-Setup-<tag version>.exe`. With the version hardwired, a tag like
; v3.0.0-rc.2 produced `RAGTools-Setup-3.0.0.exe`, the upload glob matched
; nothing, and the release published WITHOUT its installer while every step
; reported success.
#ifndef MyAppVersion
  #define MyAppVersion "3.5.1-rc.2"
#endif
#define MyAppPublisher "TaqaTechno"
#define MyAppURL "https://github.com/taqat-techno/rag"
#define MyAppExeName "rag.exe"

[Setup]
AppId={{7E4B2A3C-F1D8-4A5E-B9C0-1234567890AB}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\RAGTools
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=dist
OutputBaseFilename=RAGTools-Setup-{#MyAppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\rag.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
; User-level install — no admin needed
PrivilegesRequiredOverridesAllowed=dialog
; Prevent two installers running simultaneously (corrupts {app} during copy).
SetupMutex=RAGToolsInstallerMutex_7E4B2A3C
; Windows Restart Manager: OFF, because this installer closes its own processes
; deterministically and RM actively breaks that.
;
; Measured, from Inno's own log during a silent upgrade over a running 3.0.1:
;
;   05:24:09.772  RestartManager found an application using one of our files: rag
;                 (86-second gap: CurStepChanged(ssInstall) stopping and killing)
;   05:25:35.812  Starting the installation process.
;   05:25:35.814  Shutting down applications using our files.
;   05:25:35.919  Some applications could not be shut down.
;   05:25:35.919  Defaulting to Abort for suppressed message box (Abort/Retry/Ignore)
;   05:25:35.919  User canceled the installation process.
;
; RM enumerates BEFORE our pre-install phase and shuts down AFTER it, so it acts
; on a list that is 86 seconds stale. Whatever it then fails to close — a
; process we already stopped, or one that legitimately exited — produces an
; Abort/Retry/Ignore box, and `/SUPPRESSMSGBOXES` answers it with the DEFAULT,
; which is **Abort**. A cancelled install, rolled back, exit code 5: the upgrade
; is refused not because anything is wrong but because a second mechanism
; disagreed with the first.
;
; RM was added pre-v2.5.1 when nothing else closed running processes. Something
; else does now: `ForceKillRagProcesses` stops every owned image, scoped by
; executable path, at ssInstall — before [InstallDelete] and before [Files] —
; and verifies the result. If it ever misses one, the copy fails loudly with a
; file-in-use error and `rag selfcheck` reports a mixed installation at ssDone.
; Both are better than a silent, self-inflicted cancellation.
CloseApplications=no
; Moot with CloseApplications=no, kept explicit so re-enabling RM cannot also
; silently start relaunching applications: the post-install [Run] section is
; what starts the service, with the new binary.
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; All tasks checked by default (no "unchecked" flag = checked)
Name: "addtopath"; Description: "Add to PATH (recommended)"; GroupDescription: "Additional options:"
Name: "startup"; Description: "Start automatically on Windows login"; GroupDescription: "Additional options:"
Name: "startnow"; Description: "Start service and open admin panel after installation"; GroupDescription: "Additional options:"

[InstallDelete]
; Remove the previous bundle's payload BEFORE extracting the new one.
;
; Without this, Inno overlays the new `_internal` on the old one and every
; package manifest a previous release wrote survives — v3.0.0 shipped onto a
; tree carrying 86 `.dist-info` directories, 27 packages with more than one
; version, and `fastapi` in six. `importlib.metadata` returns the FIRST
; normalized-name match it finds, and `0.7.0` sorts before `0.8.0`, so a stale
; `safetensors-0.7.0.dist-info` beside the correct 0.8.0 made `transformers`
; read the version it version-guards at import. ImportError on every start,
; 76 crashes, supervisor gave up. Unrecoverable by the user: the bundle is
; self-contained, so the `pip install -U` the error recommends cannot reach it.
;
; Targeted at the payload, not at `{app}`, so a user file dropped in the program
; directory is not collateral. `ignoreversion` was never the problem — the
; problem is files the new bundle does not name at all, which no copy flag can
; reach. Runs after CurStepChanged(ssInstall) has stopped and killed rag.exe.
Type: filesandordirs; Name: "{app}\_internal"
; Pre-6.x PyInstaller put the payload at the root; sweep those layouts too so an
; upgrade from a much older install is not shadowed by the same mechanism.
Type: filesandordirs; Name: "{app}\*.dist-info"
Type: filesandordirs; Name: "{app}\*.egg-info"

[Files]
; The quiescence protocol, extracted to {tmp} and run from PrepareToInstall —
; BEFORE [InstallDelete] and [Files], which is the whole point. `dontcopy` so it
; never lands in the installation: it is Setup's tool, not the product's.
;
; PowerShell rather than a bundled executable because PowerShell is present on
; every Windows, and this has to run before anything is installed. It stays
; extracted for the whole Setup session, so ssDone re-invokes it in `-Mode
; Restore` to re-enable exactly the scheduled tasks it disabled.
Source: "installer\quiesce.ps1"; Flags: dontcopy
; Main application (PyInstaller one-dir output)
Source: "dist\rag\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Silent launcher script
Source: "scripts\launch.vbs"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Smart launcher: starts service if needed, opens admin panel
Name: "{group}\RAG Tools"; Filename: "{app}\launch.vbs"; IconFilename: "{app}\rag.exe"; Comment: "Start RAG Tools and open admin panel"
Name: "{group}\Uninstall RAG Tools"; Filename: "{uninstallexe}"

[Registry]
; Add to user PATH if selected
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath('{app}')

[Run]
; Create data directory structure
Filename: "cmd.exe"; Parameters: "/c mkdir ""{localappdata}\RAGTools\data"" 2>nul & mkdir ""{localappdata}\RAGTools\logs"" 2>nul"; Flags: runhidden
; Register startup task (ON by default)
Filename: "{app}\rag.exe"; Parameters: "service install"; StatusMsg: "Registering startup task..."; Tasks: startup; Flags: runhidden
; Register tray autostart (same login-startup checkbox, mirrors service install)
Filename: "{app}\rag.exe"; Parameters: "tray install"; StatusMsg: "Registering tray autostart..."; Tasks: startup; Flags: runhidden
; NO watchdog step here, deliberately. v3 removed the watchdog entirely — module,
; CLI command group and tests — because Task Scheduler, systemd and launchd all
; restart a failed service natively; the registered task now carries a
; RestartOnFailure policy instead. The legacy "RAGTools Watchdog" task is REMOVED
; by the upgrade engine (LEGACY_TASKS in ragtools/platform/windows.py), not
; repaired here.
;
; Until v3.0.0 this ran `rag.exe service watchdog install`, gated on
; HasRAGToolsWatchdogTask() — a Check true precisely on machines carrying the
; legacy task, i.e. every upgrading user. With the command gone that is an
; unknown-command exit, and because the step was `runhidden` WITHOUT `nowait` the
; installer waited for it and surfaced the failure. Two subsystems with opposite
; intentions: one removing the task, the other reinstalling it.
; START THE SERVICE. MANDATORY — no `Tasks:` gate, and it WAITS.
;
; A finished installation must leave a running service. This was gated on
; `startnow`, a checkbox whose real subject is "open the admin panel in your
; browser" — so a user who declined the browser got a registered service and
; no running one, and nothing would start it until their next Windows logon.
; On an upgrade that also means the post-upgrade re-index never begins: the
; rebuild is the service's job, and the service was not there to do it.
;
; The three concerns are now independent:
;   * starting the service      — MANDATORY, here, now;
;   * opening the browser       — optional (`startnow`);
;   * launching the tray icon   — optional (`startup`).
;
; `--wait` rather than `nowait`, because "the command was issued" and "the
; service is serving" are different claims and only the second is worth
; reporting. It returns as soon as /health answers — including while a
; migration is rebuilding, which is answering truthfully — so this does not
; block setup for the length of a re-index.
;
; The at-logon tasks registered above remain responsible for FUTURE sessions
; and for restart persistence. They are not responsible for finishing this
; installation.
Filename: "{app}\ragw.exe"; Parameters: "service start --wait --timeout 180"; StatusMsg: "Starting the background service..."; Flags: runhidden
; Open admin panel in browser — OPTIONAL, and now only the browser.
Filename: "cmd.exe"; Parameters: "/c start http://localhost:21420"; StatusMsg: "Opening admin panel..."; Tasks: startnow; Flags: runhidden nowait
; Launch the tray once after install/upgrade so the icon appears WITHOUT requiring
; logout or restart. Gated on `Tasks: startup` so nothing starts for users who
; declined autostart registration.
;
; This ran `wscript.exe "<Startup folder>\RAGTools-Tray.vbs"` until v3.0.1 — a
; path v2 wrote and v3 REMOVES (tray_startup.py:28 keeps the name only so the
; upgrade can delete it). On a fresh v3 install the file has never existed, so
; the step silently launched nothing and the tray icon did not appear until the
; user next logged in. Pointing at the shipped executable removes the dependency
; on a legacy artifact entirely.
;
; `ragw.exe`, not `rag.exe`: same bundle, GUI subsystem, so no console flashes
; even though the installer already passes `runhidden`.
Filename: "{app}\ragw.exe"; Parameters: "tray"; StatusMsg: "Starting tray icon..."; Tasks: startup; Flags: runhidden nowait

[UninstallRun]
; Stop service before uninstall
Filename: "{app}\rag.exe"; Parameters: "service stop"; Flags: runhidden; RunOnceId: "StopService"
; Remove scheduled task
Filename: "{app}\rag.exe"; Parameters: "service uninstall"; Flags: runhidden; RunOnceId: "RemoveTask"
; Remove tray autostart (symmetric with [Run] tray install)
Filename: "{app}\rag.exe"; Parameters: "tray uninstall"; Flags: runhidden; RunOnceId: "RemoveTrayTask"

[UninstallDelete]
; Clean up PID file
Type: files; Name: "{localappdata}\RAGTools\service.pid"
; Clean up .bak leftovers from in-place upgrades
Type: files; Name: "{app}\rag.exe.bak"
Type: filesandordirs; Name: "{app}\_internal.bak"
; Clean up model cache in install directory
Type: filesandordirs; Name: "{app}\model_cache"
; Clean entire install directory (catches any remaining stale files)
Type: filesandordirs; Name: "{app}"

[Code]
var
  // Set by PrepareToInstall, read at ssDone. Empty means "there was no previous
  // installation", which is a different thing from "the copy failed".
  RollbackDir: string;

// Check if {app} is already in PATH
function NeedsAddPath(Param: string): boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER,
    'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Param + ';', ';' + OrigPath + ';') = 0;
end;

// HasRAGToolsWatchdogTask() lived here. It detected the legacy "RAGTools
// Watchdog" scheduled task so the installer could re-register it, and was
// removed with its only call site in v3.0.0: the watchdog no longer exists, and
// the upgrade engine now REMOVES that task rather than repairing it. Leaving a
// dead detector behind is how the next person re-adds the [Run] step.

// Force-kill every rag.exe process tree on the machine. Belt-and-suspenders
// pass after the graceful 'service stop' — covers the tray, the supervisor,
// MCP clients, and any lingering workers that CloseApplications=yes missed.
// /F = force, /T = kill the whole process tree, /IM = match by image name.
// Errors are ignored: taskkill returns 128 when no matching process exists,
// which is the happy "fresh install" path.
procedure ForceKillRagProcesses();
var
  ResultCode: Integer;
  Images: array[0..2] of string;
  Filter: string;
begin
  // TWO mechanisms, because the two problems are different.
  //
  // `rag.exe` and `ragw.exe` are names this product owns outright — nothing
  // else on a Windows machine ships them — so matching by image name is both
  // safe and, decisively, PROVEN: v3.0.1 replaced a running `rag.exe` in place
  // on a real machine using exactly this call. A path-scoped PowerShell
  // equivalent was tried in its place and the upgrade failed with
  // `DeleteFile failed; code 5` on `rag.exe`, with owned processes still alive
  // afterwards. A mechanism that works is not replaced by a tidier one that
  // does not.
  //
  // `qdrant.exe` is the opposite case and keeps the scoping: it is a common
  // name, and `storage_backend = "external"` explicitly means "a server you run
  // yourself". Killing every Qdrant on the machine would be data loss in
  // someone else's application caused by installing this one.
  //
  // EVERY step logs its result. Inno records nothing about Exec, so a kill that
  // silently does nothing looks exactly like one that worked — which is how two
  // CI cycles went into fixing the wrong layer.
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM rag.exe /T',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Log('ForceKill: taskkill rag.exe -> ' + IntToStr(ResultCode));

  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ragw.exe /T',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Log('ForceKill: taskkill ragw.exe -> ' + IntToStr(ResultCode));

  Filter :=
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' +
    '$ErrorActionPreference = ''SilentlyContinue''; ' +
    '$app = ''' + ExpandConstant('{app}') + '''; ' +
    '$data = ''' + ExpandConstant('{localappdata}\RAGTools') + '''; ' +
    '$mine = @(Get-Process -Name qdrant -ErrorAction SilentlyContinue | ' +
    'Where-Object { $_.Path -and ' +
    '( $_.Path.StartsWith($app, ''OrdinalIgnoreCase'') -or ' +
    '  $_.Path.StartsWith($data, ''OrdinalIgnoreCase'') ) }); ' +
    'if ($mine.Count -gt 0) { $mine | Stop-Process -Force }; ' +
    'exit $mine.Count"';
  Exec('powershell.exe', Filter, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Log('ForceKill: scoped qdrant.exe stopped -> ' + IntToStr(ResultCode));

  // Longer than it looks like it needs to be. `taskkill` returns once the
  // signal is delivered, not once the kernel has released the image's file
  // handle, and the copy that follows fails on exactly that gap.
  Sleep(2500);

  // Say plainly whether anything survived, one step BEFORE the copy would
  // fail with "DeleteFile failed; code 5" and leave the reason to be inferred.
  Filter :=
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' +
    '$ErrorActionPreference = ''SilentlyContinue''; ' +
    'exit @(Get-Process -Name rag,ragw,qdrant -ErrorAction SilentlyContinue).Count"';
  Exec('powershell.exe', Filter, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Log('ForceKill: owned processes still running afterwards -> ' + IntToStr(ResultCode));
end;


function ExecBounded(const Exe, Params: string; TimeoutMs: Integer): Integer;
var
  ResultCode: Integer;
  Cmd: string;
begin
  Cmd := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' +
         '$ErrorActionPreference = ''SilentlyContinue''; ' +
         'try { $p = Start-Process -FilePath ''' + Exe + ''' -ArgumentList ''' + Params +
         ''' -WindowStyle Hidden -PassThru } catch { exit 0 }; ' +
         'if (-not $p) { exit 0 }; ' +
         'if (-not $p.WaitForExit(' + IntToStr(TimeoutMs) + ')) ' +
         '{ try { $p.Kill() } catch { }; exit 258 }; ' +
         'exit $p.ExitCode"';
  if not Exec('powershell.exe', Cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    ResultCode := -1;
  Result := ResultCode;
end;

// End every scheduled task this product owns before touching its files.
//
// Killing the process is not enough on its own: Task Scheduler can start it
// again between the kill and the copy — the registration carries
// RestartOnFailure, and a force-kill is exactly the failure it reacts to. So
// the task is ended first, then the processes are killed.
//
// `/end` rather than `/delete`: the [Run] section re-registers both tasks from
// the new binaries, and deleting here would strand a user who cancels the
// wizard between the two steps with no autostart at all.
procedure StopOwnedTasks();
var
  ResultCode: Integer;
  Tasks: array[0..2] of string;
  I: Integer;
begin
  Tasks[0] := '\RAGTools\Service';
  Tasks[1] := '\RAGTools\Tray';
  Tasks[2] := 'RAGTools Watchdog';   // legacy v2 registration, still present on upgrades
  for I := 0 to 2 do
    Exec(ExpandConstant('{sys}\schtasks.exe'),
         '/end /tn "' + Tasks[I] + '"',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// ==========================================================================
// UPGRADE QUIESCENCE — refuse BEFORE the first destructive write
// ==========================================================================
//
// `[InstallDelete]` removes `{app}\_internal` wholesale and THAT is the first
// destructive write of the whole installation. If anything still holds
// `_internal\python312.dll`, that delete raises Inno's file-in-use
// Abort/Retry/Ignore box, `/SUPPRESSMSGBOXES` answers it with the DEFAULT —
// Abort — and Setup exits 5 having half-deleted the payload. Reproduced on a
// GitHub-hosted windows-latest runner, job 91273392455, genuine packaged v3.3.0
// over the candidate:
//
//     >>> installing the candidate over it ... exit=5
//     --- Inno log ... (last 120 of 1 lines) ---   <- Setup died before its log
//       [FAIL] rag.exe reports the new version — [PYI-2072] Failed to load
//              Python DLL '...\Programs\RAGTools\_internal\python312.dll'
//       [FAIL] uninstall registry entry updated — registry reads 3.3.0
//       [FAIL] the service answers after the upgrade — nothing within 300s
//
// The defect was never the kill. It was that Setup began deleting before it had
// proven the files were replaceable, and HAD NO WAY TO REFUSE:
// `CurStepChanged(ssInstall)` cannot abort cleanly, so every problem it can see
// becomes a problem discovered mid-write.
//
// `PrepareToInstall()` can. It runs BEFORE `[InstallDelete]` and `[Files]`, and
// a non-empty return makes Setup fail with exit code **7** — "Preparing to
// Install determined Setup cannot proceed" — without modifying a single file.
// That finally gives this product two distinguishable outcomes where it had
// one:
//
//     exit 7   refused safely; your old installation is intact and runnable
//     exit 5   aborted mid-write; your installation is now mixed
//
// ONE PROTOCOL, NOT TWO. Everything the pre-install phase used to do at
// ssInstall — the bounded graceful stop, the scheduled-task control, the kill —
// now lives in `installer\quiesce.ps1`, which additionally:
//
//   * DISABLES the owned tasks rather than merely `/end`-ing them (an ended
//     task leaves its trigger armed, so the scheduler can start a replacement
//     between the kill and the copy — that is the restart race);
//   * finds holders by LOADED MODULE as well as by image name (a process with
//     an unrelated name that mapped `_internal\python312.dll` holds the lock
//     exactly as hard, and is invisible to `Get-Process -Name rag,ragw,qdrant`);
//   * PROVES every `.exe`, `.dll` and `.pyd` under `{app}` can be opened
//     exclusively, because process enumeration can come back clean while a
//     handle survives — and today the first thing to discover that is
//     `[InstallDelete]`, mid-write.
//
// The decision policy is mirrored and unit-tested in
// `src\ragtools\upgrade\quiescence.py`; the two are held in step structurally by
// `tests\test_installer_quiescence_contract.py`.

function QuiesceLogDirectory(): string;
begin
  Result := ExpandConstant('{localappdata}\RAGTools\logs');
end;

// Run the protocol. `Mode` is 'Quiesce' (pre-install) or 'Restore' (ssDone,
// re-enable exactly the tasks Quiesce disabled).
//
// NOT wrapped in `ExecBounded`, deliberately. `ExecBounded` builds a PowerShell
// `-Command "..."` string, so every argument it forwards would have to survive
// being nested inside those double quotes — and a path such as
// `C:\Program Files\RAGTools` cannot, without escaping that this project has
// already lost cycles to (`test_the_kill_needs_no_nested_double_quotes` exists
// for exactly that reason). `-File` with plainly quoted arguments has no such
// hazard.
//
// The bound moved INTO the script instead, where it is stronger: `quiesce.ps1`
// takes `-TimeoutSeconds`, checks a deadline between phases, and starts every
// child process — including the PREVIOUS release's `rag.exe`, whose behaviour
// when its own service is wedged is not knowable from here — through its own
// `Invoke-Bounded` helper with an explicit `WaitForExit` and a `Kill()`.
function RunQuiescence(const Mode: string): Integer;
var
  Params: string;
  ResultCode: Integer;
begin
  ExtractTemporaryFile('quiesce.ps1');
  ForceDirectories(QuiesceLogDirectory());

  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'
          + ExpandConstant('{tmp}\quiesce.ps1') + '"'
          + ' -AppDir "' + ExpandConstant('{app}') + '"'
          + ' -DataDir "' + ExpandConstant('{localappdata}\RAGTools') + '"'
          + ' -LogPath "' + QuiesceLogDirectory() + '\upgrade-quiesce.json"'
          + ' -TimeoutSeconds 180'
          + ' -Mode ' + Mode;

  if not Exec('powershell.exe', Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    ResultCode := 3;   // could not even launch it — an internal error, so abort
  Log('Quiescence(' + Mode + ') -> ' + IntToStr(ResultCode));
  Result := ResultCode;
end;

// What the script found, in the words the user needs: which processes and which
// files, by PID and by path. A plain-text sibling of the JSON verdict, because
// Pascal reads a string far more reliably than it parses JSON — and because
// "close this application" is actionable where "restart Windows" is what an
// installer says when it does not know.
function QuiesceBlockerText(): string;
var
  Text: AnsiString;
begin
  Result := '';
  if LoadStringFromFile(QuiesceLogDirectory() + '\quiesce-blockers.txt', Text) then
    Result := Trim(Text);
end;

// Copy the installed payload and executables to a SIBLING directory before a
// single byte of it is replaced.
//
// A sibling, never inside `{app}`: `[InstallDelete]` removes `{app}\_internal`
// wholesale and `[UninstallDelete]` removes `{app}` entirely, so a copy kept
// underneath would be deleted by the very operations it exists to survive —
// the mistake `BackupConfig` already avoids for `config.toml`.
//
// Returns False only when a copy was ATTEMPTED and did not complete. A machine
// with no previous installation returns True with nothing copied: a fresh
// install must not pay for any of this.
function BackupInstallation(): Boolean;
var
  Source: string;
  Flags: string;
  ResultCode: Integer;
begin
  Result := True;
  RollbackDir := '';
  Source := ExpandConstant('{app}');

  if not FileExists(AddBackslash(Source) + 'rag.exe') then
    Exit;

  RollbackDir := Source + '-rollback-' + GetDateTimeString('yyyymmdd-hhnnss', '-', '-');
  Flags := ' /NFL /NDL /NJH /NJS /NP /R:1 /W:1';

  if DirExists(AddBackslash(Source) + '_internal') then
  begin
    Exec(ExpandConstant('{sys}\robocopy.exe'),
         '"' + AddBackslash(Source) + '_internal" "'
             + AddBackslash(RollbackDir) + '_internal" /E' + Flags,
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Log('Rollback: copied _internal -> ' + IntToStr(ResultCode));
    // robocopy uses 0..7 for success and 8+ for failure; it is not a program
    // whose non-zero exit means anything went wrong.
    if ResultCode >= 8 then
    begin
      Result := False;
      Exit;
    end;
  end;

  Exec(ExpandConstant('{sys}\robocopy.exe'),
       '"' + Source + '" "' + RollbackDir + '" rag.exe ragw.exe' + Flags,
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Log('Rollback: copied executables -> ' + IntToStr(ResultCode));
  if ResultCode >= 8 then
  begin
    Result := False;
    Exit;
  end;

  // Verified HERE, while refusing is still free. A rollback copy discovered to
  // be incomplete after `_internal` has been deleted is not a rollback copy —
  // it is a second way to lose the installation.
  Result := FileExists(AddBackslash(RollbackDir) + 'rag.exe')
        and (DirExists(AddBackslash(RollbackDir) + '_internal')
             or not DirExists(AddBackslash(Source) + '_internal'));
  if not Result then
    Log('Rollback: the copy at ' + RollbackDir + ' is incomplete');
end;

// Put the previous version back. `_internal` FIRST, then the executables:
// `rag.exe` without its payload is precisely the mixed state being undone, so
// it must never be the thing that exists first.
procedure RestoreInstallation();
var
  Target: string;
  Flags: string;
  ResultCode: Integer;
begin
  if RollbackDir = '' then
    Exit;
  if not DirExists(RollbackDir) then
    Exit;

  Target := ExpandConstant('{app}');
  Flags := ' /IS /IT /NFL /NDL /NJH /NJS /NP /R:1 /W:1';

  if DirExists(AddBackslash(RollbackDir) + '_internal') then
  begin
    DelTree(AddBackslash(Target) + '_internal', True, True, True);
    Exec(ExpandConstant('{sys}\robocopy.exe'),
         '"' + AddBackslash(RollbackDir) + '_internal" "'
             + AddBackslash(Target) + '_internal" /E' + Flags,
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Log('Rollback: restored _internal -> ' + IntToStr(ResultCode));
  end;

  Exec(ExpandConstant('{sys}\robocopy.exe'),
       '"' + RollbackDir + '" "' + Target + '" rag.exe ragw.exe' + Flags,
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Log('Rollback: restored executables -> ' + IntToStr(ResultCode));
end;

// Only once the machine has been shown to be running the new version. Half a
// gigabyte left behind silently is its own defect.
procedure DiscardRollback();
begin
  if RollbackDir = '' then
    Exit;
  if DirExists(RollbackDir) then
  begin
    DelTree(RollbackDir, True, True, True);
    Log('Rollback: discarded ' + RollbackDir);
  end;
  RollbackDir := '';
end;

// The executables and the payload the bundle names are all present.
//
// This is the reproduced failure's exact signature: `rag.exe` survives a
// half-completed `[InstallDelete]` while `_internal\python312.dll` does not, and
// the only thing the user ever sees is `[PYI-2072]`.
function InstallationBinariesArePresent(): Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\rag.exe'))
        and FileExists(ExpandConstant('{app}\ragw.exe'))
        and FileExists(ExpandConstant('{app}\_internal\python312.dll'));
end;

// THE GATE. Runs before [InstallDelete] and [Files]; a non-empty return refuses
// the installation with exit 7 having written nothing.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
  Blockers: string;
begin
  NeedsRestart := False;
  Result := '';

  Code := RunQuiescence('Quiesce');
  if Code <> 0 then
  begin
    Blockers := QuiesceBlockerText();
    if Blockers = '' then
      Blockers := 'RAG Tools could not prove that the files of the installed '
                + 'version can be replaced (quiescence exit ' + IntToStr(Code) + ').';
    Result := Blockers + #13#10 + #13#10
            + 'NOTHING HAS BEEN CHANGED. The version already on this machine is '
            + 'intact and still runnable.' + #13#10 + #13#10
            + 'Close whatever is listed above — an editor, an MCP client, a '
            + 'terminal — and run this installer again.' + #13#10 + #13#10
            + 'Full details: ' + QuiesceLogDirectory() + '\upgrade-quiesce.json';
    Exit;
  end;

  // Quiescence is proven; the rollback copy is the second half of the same
  // promise. Refusing here is still free — nothing has been written yet.
  if not BackupInstallation() then
  begin
    Result := 'RAG Tools could not make a rollback copy of the version already '
            + 'installed, so it will not begin replacing it.' + #13#10 + #13#10
            + 'NOTHING HAS BEEN CHANGED. The most likely cause is free disk '
            + 'space: the copy needs roughly as much room as the installed '
            + 'program.' + #13#10 + #13#10
            + 'Free some space and run this installer again.';
    Exit;
  end;
end;

// Forward declaration: the verifier is defined below, next to the reasoning
// that explains it, but CurStepChanged has to reach it. Pascal resolves this
// with a declaration, not by demanding the reader meet the mechanism first.
procedure VerifyInstallation(); forward;

// Post-install (ssDone, after [Run] has re-registered the tasks): verify.
//
// ssInstall does NOTHING here any more, and that is the fix. Everything it used
// to do — the bounded graceful stop, `StopOwnedTasks`, `ForceKillRagProcesses` —
// is one protocol now, running in `PrepareToInstall` where a failure can REFUSE
// rather than abort mid-write. Two pre-install phases doing the same job in two
// places is how the second one came to be the only one that could fail.
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssDone then
    VerifyInstallation();
end;

// After the files are in place: prove the machine actually moved.
//
// Everything above is best-effort — a graceful stop that timed out, a kill that
// raced a restart, a file that stayed locked. None of it reports failure, and
// an installer that copied files has only proven that it copied files. So the
// newly installed binary is asked to audit its own installation: its version,
// its sibling `ragw.exe`, the uninstall registry entry, the scheduled-task
// targets, and any owned process still running from somewhere else.
//
// Non-zero exit means the machine is in a mixed state, and saying so is the
// entire point — a silent half-upgrade is indistinguishable from success until
// the user's service fails to start.
procedure VerifyInstallation();
var
  ResultCode: Integer;
  Verifier: string;
begin
  // STARTUP LIFECYCLE RESTORED. `quiesce.ps1` DISABLED the owned scheduled
  // tasks for the upgrade window — not merely `/end`-ed them, because an ended
  // task leaves its trigger armed and the scheduler is free to restart what we
  // just killed. Leaving them disabled afterwards would silently switch off a
  // user's autostart, so the same script re-enables EXACTLY what it disabled
  // and nothing else: a task the user had already turned off stays off.
  //
  // Runs before the verification below, because "autostart targets" is one of
  // the things being verified.
  RunQuiescence('Restore');

  // FINAL EXECUTABLE / DLL CONSISTENCY, checked before anything is asked to
  // run. This is the reproduced failure's exact signature — `rag.exe` survives
  // a half-completed `[InstallDelete]` while `_internal\python312.dll` does not,
  // and all the user ever sees is `[PYI-2072] Failed to load Python DLL`.
  //
  // An integrity failure, so the rollback copy goes back.
  Verifier := ExpandConstant('{app}\rag.exe');
  if not InstallationBinariesArePresent() then
  begin
    RestoreInstallation();
    SuppressibleMsgBox('RAG Tools could not be installed cleanly, so the version that was '
           + 'already on this machine has been RESTORED.' + #13#10 + #13#10 +
           'Files are missing from the new installation — most often because '
           + 'something was still holding them open while they were replaced.' + #13#10 + #13#10 +
           'Your projects, configuration and index were not touched. Close any '
           + 'application that uses RAG Tools (an editor, an MCP client, a terminal) '
           + 'and run this installer again.',
           mbCriticalError, MB_OK, IDOK);
    Exit;
  end;

  // Bounded: this runs at ssDone, after every file is already in place. A
  // verifier that hangs would leave the installer running forever having
  // ALREADY completed the installation successfully — the worst possible
  // trade, since the check exists to add confidence, not to be load-bearing.
  // 258 is the overrun code from ExecBounded; -1 means it could not be
  // launched. Neither is evidence of a bad install, so neither invents a
  // verdict.
  // WHAT `selfcheck` PROVES, and therefore what this step verifies: that
  // `rag.exe` launches at all (it is the process being run), that it reports
  // the candidate version, that the uninstall registry entry moved, that the
  // autostart targets name the new binaries, that the service started and
  // /health answers with the new version, that the storage contract and managed
  // engine ownership hold, and that the index identity and config schema
  // survived. Eleven checks, categorised — see `ragtools.selfcheck`.
  ResultCode := ExecBounded(Verifier,
                            'selfcheck --quiet --expect-version {#MyAppVersion}', 120000);
  if (ResultCode = -1) or (ResultCode = 258) then
  begin
    // Could not run the check, or it overran. Neither is evidence of a bad
    // install, so neither invents a verdict — and neither justifies restoring
    // the previous version over a new one that is probably fine. The binaries
    // were already proven present above, so the copy is discarded rather than
    // left behind as half a gigabyte nobody knows about.
    DiscardRollback();
    Exit;
  end;

  if ResultCode = 0 then
  begin
    DiscardRollback();
    Exit;   // clean
  end;

  // ONE MESSAGE PER CAUSE.
  //
  // This used to print a single fixed sentence for every non-zero exit —
  // "a process from the previous version was still running... some files were
  // skipped... restart Windows, then run this installer again". `selfcheck`
  // returns eleven checks, and five of them fail for RUNTIME reasons on a
  // machine whose files are byte-perfect. So a storage outage, or a rebuild
  // that was merely still running, told the user to reboot and reinstall: a
  // wrong diagnosis with a wrong and disruptive remedy.
  //
  // The classification belongs in the product, not here — Pascal is the worst
  // available place to decide what a failing check means, and the CLI already
  // knows. So `rag selfcheck` now exits with a CATEGORY and this only chooses
  // words. Codes are from `ragtools.selfcheck`: 1 integrity, 2 runtime,
  // 3 migrating, 4 warning.
  //
  // AND ONE ROLLBACK PER CATEGORY. Restoring the previous bundle is the right
  // answer to an INTEGRITY failure — a mixed directory is exactly what the copy
  // can undo. It is the wrong answer to a RUNTIME one: a storage outage, a held
  // port or a rebuild that stopped early would survive the rollback unchanged,
  // and the machine would end up on an older version with the original problem
  // still there. This installer has already learned the general form of that
  // lesson once, when a single fixed "restart Windows and reinstall" message was
  // shown for every failing check.
  case ResultCode of
    3:
      begin
        // Installed correctly; the index is being rebuilt. Nothing to do, and
        // saying anything alarming here is how a user reinstalls over a healthy
        // migration and starts it again from zero.
        DiscardRollback();
        SuppressibleMsgBox('RAG Tools {#MyAppVersion} was installed successfully.' + #13#10 + #13#10 +
               'Your index is being rebuilt for the new storage layout. This runs in '
               + 'the background and can take a while on a large corpus.' + #13#10 + #13#10 +
               'Searches will report "migration in progress" until it finishes. '
               + 'No action is needed.' + #13#10 + #13#10 +
               'To watch progress:  rag status',
               mbInformation, MB_OK, IDOK);
      end;
    2:
      begin
        // Installed correctly; something at run time is stuck. Name it, and do
        // NOT prescribe a reboot — nothing about this is fixed by restarting
        // Windows or replacing files that are already correct. For the same
        // reason the rollback copy is discarded rather than applied: an older
        // version would meet the identical runtime problem.
        DiscardRollback();
        SuppressibleMsgBox('RAG Tools {#MyAppVersion} was installed successfully, but it is not '
               + 'running properly yet.' + #13#10 + #13#10 +
               'The files on this machine are correct. The problem is at run time — '
               + 'most often the storage engine is unreachable, its port is held by '
               + 'another RAG Tools instance, or an index rebuild stopped early.' + #13#10 + #13#10 +
               'Reinstalling will NOT help. To see the exact cause and its remedy, run:'
               + #13#10 + #13#10 +
               '    rag selfcheck' + #13#10 + #13#10 +
               'If a rebuild stopped, resume it with:  rag upgrade --resume',
               mbError, MB_OK, IDOK);
      end;
    4:
      DiscardRollback();  // warnings only — logged by selfcheck, not worth a dialog
  else
    // 1, and anything unrecognised: treat as an integrity failure, because
    // that is the case where doing nothing leaves a genuinely broken install.
    //
    // And an integrity failure is precisely what the rollback copy exists for.
    // The machine goes back to the version it was running, which is a state
    // known to work, instead of being left on a mixed tree that will fail at
    // every start until someone reinstalls by hand. Never leave a mixed
    // PyInstaller directory silently.
    begin
      RestoreInstallation();
      SuppressibleMsgBox('RAG Tools {#MyAppVersion} could not be installed cleanly, so the '
             + 'version that was already on this machine has been RESTORED.' + #13#10 + #13#10 +
             'This usually means a process from the previous version was still running '
             + 'while files were replaced, so some files were skipped.' + #13#10 + #13#10 +
             'Your projects, configuration and index are not affected. Close any '
             + 'application that uses RAG Tools — an editor, an MCP client, a terminal — '
             + 'and run this installer again. If it fails the same way, restart Windows '
             + 'first so nothing can still be holding the old files.' + #13#10 + #13#10 +
             'For details run:  rag selfcheck',
             mbCriticalError, MB_OK, IDOK);
    end;
  end;
end;

// Copy the configuration out of the data root before anything deletes it.
//
// The prompt below bundles two very different things under "user data". The
// index and the model cache are DERIVED — expensive to rebuild (hours) but
// rebuildable from the machine itself. `config.toml` is NOT: it is the list of
// projects, ignore rules and per-project modes a user assembled by hand over
// months, and nothing regenerates it. In the 3.0.0 incident it took 25 project
// definitions with it, and only a precautionary manual copy made minutes
// earlier saved them.
//
// So the small, precious file is copied out unconditionally on the delete path,
// to a sibling directory the wipe cannot reach. Returns the backup directory,
// or '' when there was nothing to save. Sets Failed when a config file existed
// and could NOT be copied — the caller must then refuse to delete, because
// destroying what you have just failed to preserve is the one outcome with no
// recovery.
function BackupConfig(DataDir: string; var Failed: Boolean): string;
var
  Target: string;
  Names: array[0..1] of string;
  I: Integer;
  Source: string;
  Saved: Boolean;
begin
  Result := '';
  Failed := False;
  Saved := False;
  Names[0] := 'config.toml';
  Names[1] := 'ragtools.toml';
  // Sibling of the data root, not inside it: a backup within DataDir would be
  // deleted by the very DelTree it exists to survive.
  Target := ExpandConstant('{localappdata}\RAGTools-config-backup-') +
            GetDateTimeString('yyyymmdd-hhnnss', '-', '-');

  for I := 0 to 1 do
  begin
    Source := AddBackslash(DataDir) + Names[I];
    if FileExists(Source) then
    begin
      if not DirExists(Target) then
        ForceDirectories(Target);
      if FileCopy(Source, AddBackslash(Target) + Names[I], False) then
        Saved := True
      else
        Failed := True;
    end;
  end;

  if Saved then
    Result := Target;
end;

// Uninstall: ask about deleting user data. Default is KEEP (safe).
// The user must explicitly choose "Yes" to delete their indexed content,
// configuration, logs and caches. "No" (default), pressing Enter or closing
// the dialog all preserve user data.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Response: Integer;
  DataDir: string;
  Backup: string;
  BackupFailed: Boolean;
begin
  // Force-kill rag.exe processes BEFORE the uninstaller starts removing files —
  // same reasoning as CurStepChanged(ssInstall). The [UninstallRun] graceful
  // `service stop` runs first, but the tray and supervisor can survive that.
  if CurUninstallStep = usUninstall then
  begin
    // END THE TASKS FIRST — the same ordering install needs, for the same
    // reason. The registration carries RestartOnFailure, and a force-kill is
    // precisely the failure it reacts to, so killing first lets the scheduler
    // start a replacement service mid-uninstall. That service then spawns the
    // managed Qdrant, which holds `bin\qdrant.exe` open, and the uninstaller
    // silently leaves `bin` and `_internal` behind.
    //
    // Observed exactly that: `qdrant.exe pid=1184` still running afterwards,
    // with `['bin', '_internal']` surviving in the program directory. The
    // engine only started shipping in this release, so this failure mode is
    // new with it — an uninstall that leaves a database server running.
    StopOwnedTasks();
    ForceKillRagProcesses();
  end;

  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\RAGTools');

    // Skip prompt entirely if there is no data dir to worry about
    if not DirExists(DataDir) then
      Exit;

    Response := SuppressibleMsgBox(
      'RAG Tools has been uninstalled.' + #13#10 + #13#10 +
      'Do you ALSO want to DELETE your user data?' + #13#10 + #13#10 +
      'CANNOT be rebuilt:' + #13#10 +
      '  - Your configuration — the project list, ignore rules and' + #13#10 +
      '    per-project modes you set up by hand' + #13#10 + #13#10 +
      'Can be rebuilt, but it takes time:' + #13#10 +
      '  - Indexed content (vector database)' + #13#10 +
      '  - Model cache' + #13#10 +
      '  - Logs' + #13#10 + #13#10 +
      'Location: ' + DataDir + #13#10 + #13#10 +
      'A copy of your configuration is saved either way, and this dialog will' + #13#10 +
      'tell you where. Nothing goes to the Recycle Bin.' + #13#10 + #13#10 +
      'Default is NO (keep everything). Choose YES only for a full wipe.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO);

    if Response = IDYES then
    begin
      Backup := BackupConfig(DataDir, BackupFailed);

      // A config we could not copy is a config we must not delete. Keeping
      // everything is always a recoverable outcome; this is the only branch
      // that is not.
      if BackupFailed then
      begin
        SuppressibleMsgBox(
          'Your user data was NOT deleted.' + #13#10 + #13#10 +
          'The configuration file could not be copied to a backup, and' + #13#10 +
          'deleting it without one cannot be undone.' + #13#10 + #13#10 +
          'Everything is still at:' + #13#10 + DataDir + #13#10 + #13#10 +
          'Delete that folder by hand if you are sure you want it gone.',
          mbError, MB_OK, IDOK);
        Exit;
      end;

      DelTree(DataDir, True, True, True);

      if Backup <> '' then
        SuppressibleMsgBox(
          'Your user data has been deleted.' + #13#10 + #13#10 +
          'Your configuration was copied to:' + #13#10 + Backup + #13#10 + #13#10 +
          'Keep this if you might reinstall — it holds your project list,' + #13#10 +
          'and the index rebuilds itself from it.',
          mbInformation, MB_OK, IDOK);
    end;
  end;
end;
