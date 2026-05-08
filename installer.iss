; RAG Tools Installer — Inno Setup Script
; Builds a Windows installer from PyInstaller output.
;
; Prerequisites:
;   - PyInstaller build completed: dist\rag\rag.exe exists
;   - Inno Setup 6+ installed
;
; Usage:
;   iscc installer.iss

#define MyAppName "RAG Tools"
#define MyAppVersion "2.5.5"
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
; Repair the watchdog Scheduled Task ONLY if the user already opted into it on a
; prior install. Pre-v2.5.3 the task action was the bare console exe, so every
; firing flashed a conhost window. Re-running `service watchdog install` writes
; the silent VBS launcher and overwrites the task action via `schtasks /create /f`.
; Never installs the watchdog for users who never opted in — see HasRAGToolsWatchdogTask().
Filename: "{app}\rag.exe"; Parameters: "service watchdog install"; StatusMsg: "Repairing watchdog task..."; Flags: runhidden; Check: HasRAGToolsWatchdogTask
; Start service now (ON by default)
Filename: "{app}\rag.exe"; Parameters: "service start"; StatusMsg: "Starting service..."; Tasks: startnow; Flags: runhidden nowait
; Open admin panel in browser after a delay (let service start)
Filename: "cmd.exe"; Parameters: "/c timeout /t 15 /nobreak >nul & start http://localhost:21420"; StatusMsg: "Opening admin panel..."; Tasks: startnow; Flags: runhidden nowait
; Launch the tray once after install/upgrade so the icon appears WITHOUT requiring
; logout or restart. Runs the same Startup-folder VBS Windows would invoke at next
; login (15 s WScript.Sleep + hidden shell.Run rag.exe tray). Hidden + nowait so
; the installer never blocks on it. Gated on `Tasks: startup` so we never run a
; non-existent VBS for users who declined autostart registration.
Filename: "{sys}\wscript.exe"; Parameters: """{userappdata}\Microsoft\Windows\Start Menu\Programs\Startup\RAGTools-Tray.vbs"""; StatusMsg: "Starting tray icon..."; Tasks: startup; Flags: runhidden nowait

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

// Detect whether the user already opted into the optional watchdog Scheduled
// Task on a prior install. Returns True when `schtasks /query` succeeds for
// "RAGTools Watchdog" (exit code 0). Used as a [Run] Check so we ONLY repair
// the task for users who already had it — never auto-install for users who
// declined. Pre-v2.5.3 the task action was the bare console exe and flashed a
// conhost window every 15 minutes; v2.5.3 introduced a silent VBS launcher
// but the installer never re-registered the existing task, leaving affected
// users stuck with the popup. This Check fixes that gap on upgrade.
function HasRAGToolsWatchdogTask(): Boolean;
var
  ResultCode: Integer;
begin
  Result := False;
  if Exec(ExpandConstant('{sys}\schtasks.exe'),
          '/query /tn "RAGTools Watchdog"',
          '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := (ResultCode = 0);
  end;
end;

// Force-kill every rag.exe process tree on the machine. Belt-and-suspenders
// pass after the graceful 'service stop' — covers the tray, the supervisor,
// MCP clients, and any lingering workers that CloseApplications=yes missed.
// /F = force, /T = kill the whole process tree, /IM = match by image name.
// Errors are ignored: taskkill returns 128 when no matching process exists,
// which is the happy "fresh install" path.
procedure ForceKillRagProcesses();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'),
       '/F /IM rag.exe /T',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  // Short delay for NTFS file handles to fully release so the copy step
  // doesn't hit a "file is in use" error immediately after kill.
  Sleep(1500);
end;

// Pre-install: stop running service and force-close any remaining rag.exe.
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssInstall then
  begin
    // Phase 1: ask the service to stop gracefully (if upgrading).
    if FileExists(ExpandConstant('{app}\rag.exe')) then
    begin
      Exec(ExpandConstant('{app}\rag.exe'), 'service stop', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Exec(ExpandConstant('{app}\rag.exe'), 'service uninstall', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
    // Phase 2: force-kill anything that's still holding the exe open.
    ForceKillRagProcesses();
  end;
end;

// Uninstall: ask about deleting user data. Default is KEEP (safe).
// The user must explicitly choose "Yes" to delete their indexed content,
// configuration, logs and caches. "No" (default), pressing Enter or closing
// the dialog all preserve user data.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Response: Integer;
  DataDir: string;
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
      'This includes:' + #13#10 +
      '  - Indexed content (vector database)' + #13#10 +
      '  - Configuration (ragtools.toml / config.toml)' + #13#10 +
      '  - Logs' + #13#10 +
      '  - Model cache' + #13#10 + #13#10 +
      'Location: ' + DataDir + #13#10 + #13#10 +
      'Default is NO (keep your data). Choose YES only if you want a full wipe.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2);

    if Response = IDYES then
    begin
      DelTree(DataDir, True, True, True);
    end;
  end;
end;
