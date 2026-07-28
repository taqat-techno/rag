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
  #define MyAppVersion "3.1.0"
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
; Windows Restart Manager: detect running rag.exe (service, tray, supervisor,
; MCP clients holding a handle) and offer to close them before copying files.
; Pre-v2.5.1 users had to Task-Manager-kill everything by hand — this fixes it.
CloseApplications=yes
; Don't relaunch them after install — the post-install [Run] section handles
; starting the service cleanly with the new binary.
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
; Start service now (ON by default)
Filename: "{app}\rag.exe"; Parameters: "service start"; StatusMsg: "Starting service..."; Tasks: startnow; Flags: runhidden nowait
; Open admin panel in browser after a delay (let service start)
Filename: "cmd.exe"; Parameters: "/c timeout /t 15 /nobreak >nul & start http://localhost:21420"; StatusMsg: "Opening admin panel..."; Tasks: startnow; Flags: runhidden nowait
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
  I: Integer;
begin
  // EVERY image an installation owns, not just the one it used to ship.
  //
  // This killed `rag.exe` alone until v3.1.0 — written when that was the only
  // executable. v3.0.1 then added `ragw.exe` and pointed BOTH scheduled tasks
  // at it, so from that release on the service and tray at login are `ragw.exe`
  // and the kill missed them entirely. A running image holds its own file, so
  // the very processes an upgrade must stop were the ones it left alone: the
  // copy would skip locked files and the installer would report success over a
  // half-replaced tree. `CloseApplications=yes` catches some of this through
  // Restart Manager, but it is a prompt, not a guarantee, and it does not cover
  // a process holding a file it did not open by name.
  //
  // `qdrant.exe` is owned too: managed storage supervises one, and it holds
  // handles under the data dir that a reconciling upgrade may need.
  Images[0] := 'rag.exe';
  Images[1] := 'ragw.exe';
  Images[2] := 'qdrant.exe';

  // SCOPED BY PATH, not by image name.
  //
  // `/IM qdrant.exe` matches every Qdrant on the machine. Ours is one of them;
  // the others are not ours to kill. `storage_backend = "external"` explicitly
  // means "a server you run yourself", a second ragtools install has its own,
  // and an unrelated product may simply ship a binary with the same common
  // name. Killing those is data loss in someone else's application, caused by
  // installing this one — and the product already draws this distinction
  // correctly everywhere else: `selfcheck` compares process paths against the
  // install directory precisely so it does not accuse a stranger's process.
  //
  // `Get-Process`, not `Get-CimInstance -Filter`, and NO embedded double
  // quotes anywhere.
  //
  // The CIM form needs a quoted WQL filter (`-Filter "Name='rag.exe'"`), which
  // has to reach PowerShell through Inno's Exec, through CommandLineToArgvW,
  // as `""Name='rag.exe'""`. That survives only if every layer agrees on the
  // escaping; when it does not, `Get-CimInstance` errors, the error is
  // swallowed by SilentlyContinue, NOTHING is killed, and the upgrade fails
  // later with a locked file — a silent failure that looks exactly like the
  // defect this procedure exists to prevent.
  //
  // `Get-Process` takes bare names, so the whole command is single-quoted
  // throughout and there is nothing to escape. It also matches all three images
  // in ONE call, which removes the window in which a PID could be reused
  // between deciding and acting.
  //
  // `wmic` is not an option: deprecated, and absent from recent Windows builds.
  Filter :=
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' +
    '$ErrorActionPreference = ''SilentlyContinue''; ' +
    '$app = ''' + ExpandConstant('{app}') + '''; ' +
    '$data = ''' + ExpandConstant('{localappdata}\RAGTools') + '''; ' +
    // BASE names, not filenames. `Get-Process -Name` matches ProcessName, which
    // carries no extension: `-Name rag.exe` matches NOTHING and reports no
    // error, so passing the filenames would silently kill nothing and leave the
    // upgrade to fail later on a locked file. Measured on a live machine:
    // `-Name rag.exe,ragw.exe,qdrant.exe` -> 0 processes; `-Name rag,ragw,qdrant`
    // -> 7. Derived with RemoveFileExt so the list above stays the single source.
    'Get-Process -Name ' + RemoveFileExt(Images[0]) + ',' +
    RemoveFileExt(Images[1]) + ',' + RemoveFileExt(Images[2]) +
    ' -ErrorAction SilentlyContinue | ' +
    'Where-Object { $_.Path -and ' +
    '( $_.Path.StartsWith($app, ''OrdinalIgnoreCase'') -or ' +
    '  $_.Path.StartsWith($data, ''OrdinalIgnoreCase'') ) } | ' +
    'Stop-Process -Force"';
  Exec('powershell.exe', Filter, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  // Short delay for NTFS file handles to fully release so the copy step
  // doesn't hit a "file is in use" error immediately after kill.
  Sleep(1500);
end;

// Run a program with a hard time limit, and carry on when it overruns.
//
// `Exec(..., ewWaitUntilTerminated, ...)` waits forever. That is tolerable when
// the callee is this release's own binary; it is not when the callee is the
// PREVIOUS release's binary, whose behaviour this installer cannot control and
// cannot fix. A `service stop` that never returns hangs the upgrade before a
// single file has been replaced — and in a silent install there is no output to
// say so. It simply stops, which is indistinguishable from a wedged machine.
//
// Overrunning is not an error here. Every bounded call below is a courtesy step
// whose job is done more forcefully a moment later by StopOwnedTasks and
// ForceKillRagProcesses; the point is to give the graceful path a chance, not to
// depend on it.
//
// PowerShell is the mechanism because it is the only always-present way to wait
// with a timeout and then kill: `timeout.exe` waits without killing, `start
// /wait` is unbounded, and Inno exposes no timeout of its own.
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

// Forward declaration: the verifier is defined below, next to the reasoning
// that explains it, but CurStepChanged has to reach it. Pascal resolves this
// with a declaration, not by demanding the reader meet the mechanism first.
procedure VerifyInstallation(); forward;

// Pre-install: stop running service and force-close any remaining rag.exe.
// Post-install (ssDone, after [Run] has re-registered the tasks): verify.
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssDone then
    VerifyInstallation();

  if CurStep = ssInstall then
  begin
    // Phase 1: ask the service to stop gracefully (if upgrading).
    //
    // BOUNDED, because this runs the PREVIOUS release's `rag.exe`. Which
    // release that is, and how it behaves when its own service is wedged, is
    // not knowable from here — every version ever shipped is a possible callee.
    // Unbounded, a single old binary that never returns stops the upgrade dead
    // before any file is touched, with no output explaining why.
    //
    // Overrunning the limit is fine. Phases 2 and 3 do the same job without
    // asking, so this is the polite attempt, not the mechanism.
    if FileExists(ExpandConstant('{app}\rag.exe')) then
    begin
      ResultCode := ExecBounded(ExpandConstant('{app}\rag.exe'), 'service stop', 60000);
      ResultCode := ExecBounded(ExpandConstant('{app}\rag.exe'), 'service uninstall', 60000);
    end;
    // Phase 2: end the scheduled tasks BEFORE killing, so the scheduler cannot
    // restart what we are about to kill. The registration carries
    // RestartOnFailure and a force-kill is precisely the failure it reacts to.
    StopOwnedTasks();
    // Phase 3: force-kill every owned image still holding a file open.
    ForceKillRagProcesses();
  end;
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
  Verifier := ExpandConstant('{app}\rag.exe');
  if not FileExists(Verifier) then
  begin
    MsgBox('Installation problem: ' + Verifier + ' is missing after setup.' + #13#10 + #13#10 +
           'The installation is incomplete. Please re-run this installer and, if it fails '
           + 'again, restart Windows first so no old files remain locked.',
           mbCriticalError, MB_OK);
    Exit;
  end;

  // Bounded: this runs at ssDone, after every file is already in place. A
  // verifier that hangs would leave the installer running forever having
  // ALREADY completed the installation successfully — the worst possible
  // trade, since the check exists to add confidence, not to be load-bearing.
  // 258 is the overrun code from ExecBounded; -1 means it could not be
  // launched. Neither is evidence of a bad install, so neither invents a
  // verdict.
  ResultCode := ExecBounded(Verifier,
                            'selfcheck --quiet --expect-version {#MyAppVersion}', 120000);
  if (ResultCode = -1) or (ResultCode = 258) then
    Exit;   // could not run the check, or it overran; do not invent a verdict

  if ResultCode <> 0 then
    MsgBox('RAG Tools {#MyAppVersion} was installed, but verification found that this '
           + 'machine is NOT fully running it.' + #13#10 + #13#10 +
           'This usually means a process from the previous version was still running '
           + 'while files were replaced, so some files were skipped.' + #13#10 + #13#10 +
           'To fix it: restart Windows, then run this installer again. Your projects, '
           + 'configuration and index are not affected.' + #13#10 + #13#10 +
           'For details run:  rag selfcheck',
           mbCriticalError, MB_OK);
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
    ForceKillRagProcesses();
  end;

  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\RAGTools');

    // Skip prompt entirely if there is no data dir to worry about
    if not DirExists(DataDir) then
      Exit;

    Response := MsgBox(
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
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2);

    if Response = IDYES then
    begin
      Backup := BackupConfig(DataDir, BackupFailed);

      // A config we could not copy is a config we must not delete. Keeping
      // everything is always a recoverable outcome; this is the only branch
      // that is not.
      if BackupFailed then
      begin
        MsgBox(
          'Your user data was NOT deleted.' + #13#10 + #13#10 +
          'The configuration file could not be copied to a backup, and' + #13#10 +
          'deleting it without one cannot be undone.' + #13#10 + #13#10 +
          'Everything is still at:' + #13#10 + DataDir + #13#10 + #13#10 +
          'Delete that folder by hand if you are sure you want it gone.',
          mbError, MB_OK);
        Exit;
      end;

      DelTree(DataDir, True, True, True);

      if Backup <> '' then
        MsgBox(
          'Your user data has been deleted.' + #13#10 + #13#10 +
          'Your configuration was copied to:' + #13#10 + Backup + #13#10 + #13#10 +
          'Keep this if you might reinstall — it holds your project list,' + #13#10 +
          'and the index rebuilds itself from it.',
          mbInformation, MB_OK);
    end;
  end;
end;
