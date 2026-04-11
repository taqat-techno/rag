# Windows App & System Actions

> Reusable reference for Windows-specific OS integration in RAG Tools.
> Use this when implementing or modifying any Windows system interaction.

## Purpose
Guide development of Windows-specific features: startup registration, background service management, browser launching, installer behavior, and native dialogs.

## When to Use
- Adding or modifying startup/login behavior
- Changing how the service runs as a background process
- Modifying the installer (Inno Setup)
- Adding native Windows dialogs or system interactions
- Debugging SmartScreen, PATH, or registry issues

---

## Startup on Login

### Recommended: Task Scheduler via `schtasks.exe`
**Why:** Supports delay, retry, hidden execution. No admin required for user-level tasks.

```bash
schtasks /create /tn "RAGTools Service" /tr "\"C:\path\to\rag.exe\" service run --from-scheduler" /sc onlogon /rl limited /delay 0000:30 /f
```

**Project file:** `src/ragtools/service/startup.py`

### Alternative: Registry Run Key
**When:** You need simpler startup without delay. No retry, no conditions.
```python
import winreg
key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
winreg.SetValueEx(key, "RAGTools", 0, winreg.REG_SZ,
    r'"C:\path\to\rag.exe" service run')
```

### Alternative: Startup Folder
**When:** Simplest approach, visible in Task Manager > Startup tab.
**Path:** `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`
Place a `.lnk` or `.vbs` script. No delay, no retry.

### Anti-Patterns
- Do NOT use Windows Services for user-level apps that need browser/GUI access
- Do NOT start without delay — network stack may not be ready
- Do NOT use `shell:startup` for production apps — users accidentally delete shortcuts

---

## Background Process Management

### Detached Process Pattern (current)
```python
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
                 stdout=log_file, stderr=log_file)
```

**Project file:** `src/ragtools/service/process.py`

### Process Alive Check
```python
# Windows: use kernel32 OpenProcess
handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
alive = handle != 0
if handle: ctypes.windll.kernel32.CloseHandle(handle)
```

### PID File Caveats
- PIDs are recycled on Windows — stale PID file may point to wrong process
- Always verify with health check first: `GET http://127.0.0.1:21420/health`
- Consider named mutex for single-instance guard:
  ```python
  mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "Global\\RAGToolsService")
  if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
      sys.exit(1)
  ```

---

## Browser Opening

### Recommended: `webbrowser.open(url)`
- Cross-platform. Calls `ShellExecuteW` on Windows internally.
- Returns `True` even if browser doesn't actually open (fire-and-forget).

### From VBScript: `shell.Run "http://url"`
- Uses `ShellExecuteW`. Reliable for Start Menu launchers.

### Anti-Patterns
- Do NOT auto-open browser on Task Scheduler startup (user didn't ask)
- Do NOT open browser on crash-restart (KeepAlive scenarios)
- DO open browser on explicit user action (launcher click, installer finish)

---

## Native Dialogs

### Folder Picker
**Current approach:** VBScript `Shell.BrowseForFolder` via `cscript.exe`
```vbs
Set shell = CreateObject("Shell.Application")
Set folder = shell.BrowseForFolder(0, "Choose folder", &H0051, "")
If Not folder Is Nothing Then WScript.Echo folder.Self.Path
```

**VBScript deprecation warning:** Microsoft is phasing out VBScript (Phase 2 expected ~2027+). Migration path: PowerShell `System.Windows.Forms.FolderBrowserDialog` or compiled helper.

**PowerShell alternative (slower startup):**
```powershell
Add-Type -AssemblyName System.Windows.Forms
$dlg = New-Object System.Windows.Forms.FolderBrowserDialog
if ($dlg.ShowDialog() -eq 'OK') { $dlg.SelectedPath }
```

**Must be async:** Any dialog must run in a background thread/process. Blocking uvicorn's single worker deadlocks the server.

---

## Installer (Inno Setup)

### Key Patterns
- `PrivilegesRequired=lowest` — user-level install, no admin
- `{autopf}` resolves to `%LOCALAPPDATA%\Programs\` for user installs
- `{localappdata}` for user data directory
- Stop service before upgrade in `CurStepChanged(ssInstall)`
- Prompt before deleting user data in `CurUninstallStepChanged`

### Known Gap: PATH Cleanup on Uninstall
Inno Setup adds to PATH but does NOT remove on uninstall. Must add cleanup code:
```pascal
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var Path, AppDir: string; P: Integer;
begin
  if CurUninstallStep = usUninstall then begin
    AppDir := ExpandConstant('{app}');
    if RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path) then begin
      P := Pos(';' + AppDir, Path);
      if P > 0 then begin
        Delete(Path, P, Length(';' + AppDir));
        RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path);
      end;
    end;
  end;
end;
```

### SmartScreen
- Unsigned exe → "Unknown publisher" warning on first run
- Fix: EV code signing certificate (~$300-500/year) for instant trust
- Interim: document "Click Run anyway" for users

**Project file:** `installer.iss`

---

## File System Conventions

| Content | Path |
|---------|------|
| Install directory | `%LOCALAPPDATA%\Programs\RAGTools\` |
| User data (Qdrant, state, config) | `%LOCALAPPDATA%\RAGTools\` |
| Logs | `%LOCALAPPDATA%\RAGTools\logs\` |
| Model cache | `%LOCALAPPDATA%\Programs\RAGTools\model_cache\` |
| PID file | `%LOCALAPPDATA%\RAGTools\service.pid` |

**Project file:** `src/ragtools/config.py` (`get_data_dir()`)

---

## Testing Guidance
- Test install/uninstall on a clean Windows VM
- Verify Task Scheduler task appears in `taskschd.msc`
- Verify Start Menu shortcut behavior
- Verify PATH changes survive terminal restart
- Verify SmartScreen behavior on fresh download
- Test service startup after reboot (auto-start)
