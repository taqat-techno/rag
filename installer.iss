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
#define MyAppVersion "2.3.1"
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
; Folder picker script
Source: "scripts\pick_folder.vbs"; DestDir: "{app}"; Flags: ignoreversion

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
; Start service now (ON by default)
Filename: "{app}\rag.exe"; Parameters: "service start"; StatusMsg: "Starting service..."; Tasks: startnow; Flags: runhidden nowait
; Open admin panel in browser after a delay (let service start)
Filename: "cmd.exe"; Parameters: "/c timeout /t 15 /nobreak >nul & start http://localhost:21420"; StatusMsg: "Opening admin panel..."; Tasks: startnow; Flags: runhidden nowait

[UninstallRun]
; Stop service before uninstall
Filename: "{app}\rag.exe"; Parameters: "service stop"; Flags: runhidden; RunOnceId: "StopService"
; Remove scheduled task
Filename: "{app}\rag.exe"; Parameters: "service uninstall"; Flags: runhidden; RunOnceId: "RemoveTask"

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

// Pre-install: stop running service
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssInstall then
  begin
    // Try to stop service if upgrading
    if FileExists(ExpandConstant('{app}\rag.exe')) then
    begin
      Exec(ExpandConstant('{app}\rag.exe'), 'service stop', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Exec(ExpandConstant('{app}\rag.exe'), 'service uninstall', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
  end;
end;

// Uninstall: ask about keeping user data
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox('Do you want to keep your RAG Tools data?' + #13#10 +
              '(indexed content, config, logs, model cache)' + #13#10 + #13#10 +
              'Click "No" to delete everything.',
              mbConfirmation, MB_YESNO) = IDNO then
    begin
      // Delete user data directory (Qdrant DB, logs, config)
      DelTree(ExpandConstant('{localappdata}\RAGTools'), True, True, True);
    end;
  end;
end;
