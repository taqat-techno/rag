# Cross-Platform App Launch & System Integration

> Reusable reference for platform-agnostic patterns used across Windows and macOS.
> Use this when implementing features that must work on both platforms.

## Purpose
Guide development of cross-platform behaviors: browser opening, health checking, first-run flows, service lifecycle, and graceful degradation.

## When to Use
- Implementing any feature that runs on both Windows and macOS
- Designing startup, shutdown, or restart flows
- Adding browser-open logic
- Handling "service not running" states
- Making platform-detection decisions

---

## Platform Detection Pattern

```python
import sys, os
from pathlib import Path

def get_platform():
    if sys.platform == "win32":
        return "windows"
    elif sys.platform == "darwin":
        return "macos"
    return "linux"

def is_frozen():
    """Running from PyInstaller bundle?"""
    return getattr(sys, "frozen", False)

def get_data_dir():
    if os.environ.get("RAG_DATA_DIR"):
        return Path(os.environ["RAG_DATA_DIR"])
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "RAGTools"
    elif sys.platform == "darwin":
        return Path.home() / "Library/Application Support/RAGTools"
    return Path.home() / ".local/share/ragtools"
```

**Project file:** `src/ragtools/config.py`

---

## Browser Opening

### When to Open Browser
| Trigger | Open browser? | Why |
|---------|--------------|-----|
| User clicks launcher/shortcut | Yes | User explicitly asked |
| Installer finishes (first run) | Yes | User expects to see the app |
| Service starts via Task Scheduler/LaunchAgent | **No** | User didn't ask — silent background start |
| Service restarts after crash | **No** | Would be disruptive |
| CLI `rag service start` | **No** | CLI users don't expect GUI side effects |

### Implementation
```python
import webbrowser
webbrowser.open("http://127.0.0.1:21420")  # Cross-platform, reliable
```

### Anti-Patterns
- Do NOT open browser on every service start
- Do NOT open browser in headless/CI environments
- Use `--from-scheduler` flag or config to distinguish user-initiated vs auto-starts

---

## Health-Check-Before-Open Pattern

### The Standard Flow
```
User clicks launcher → Check health → Start if needed → Poll until healthy → Open browser
```

### Implementation
```python
import httpx, time

def wait_for_service(host="127.0.0.1", port=21420, timeout=30):
    url = f"http://{host}:{port}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(0.5)
    return False
```

### Edge Cases
- **Port conflict:** Verify response body contains expected data, not just HTTP 200
- **IPv6:** Use `127.0.0.1` explicitly, not `localhost` (avoids `::1` resolution)
- **Firewall:** Bind to `127.0.0.1` not `0.0.0.0` — avoids macOS firewall dialog
- **Variable startup time:** Model loading takes 5-15s. Polling handles this naturally.

---

## First-Run vs Subsequent-Run

### First Run Should:
- Create data directories
- Generate default config
- Show onboarding UI ("Add your first project")
- Open browser automatically
- Register startup task (if user opted in)

### Subsequent Runs Should:
- Start silently (no browser unless user-initiated)
- Resume watcher for configured projects
- Run startup sync (check for offline changes)
- Do NOT re-prompt for setup

### Detection
```python
def is_first_run(data_dir: Path) -> bool:
    config = data_dir / "config.toml"
    return not config.exists()
```

---

## Service Lifecycle

### Start Flow
```
1. Write PID file
2. Setup logging
3. Load settings
4. Load ML model (~5-15s)
5. Open Qdrant storage
6. Start HTTP server
7. Post-startup:
   a. Start file watcher
   b. Register startup task (idempotent)
   c. Run startup sync (incremental index)
   d. Open browser (only if from-scheduler + configured)
```

### Stop Flow
```
1. POST /api/shutdown (graceful)
2. Stop watcher thread
3. Close Qdrant client
4. Exit process
5. Delete PID file
```

### Platform-Specific Process Creation
| Platform | Detached process | Single-instance guard |
|----------|-----------------|----------------------|
| Windows | `CREATE_NO_WINDOW \| DETACHED_PROCESS` flags | Named mutex via `CreateMutexW` |
| macOS/Linux | `start_new_session=True` | Lock file via `fcntl.flock` |

---

## Startup Registration

| Platform | Mechanism | Delay support | Auto-restart |
|----------|-----------|--------------|-------------|
| Windows | Task Scheduler (`schtasks`) | Yes (minutes) | No (one-shot) |
| macOS | LaunchAgent (plist) | No | Yes (`KeepAlive`) |
| Linux | systemd user service | Yes | Yes (`Restart=on-failure`) |

### Abstraction Pattern
```python
def install_startup(exe_path: str, delay_seconds: int = 30):
    if sys.platform == "win32":
        _install_windows_task(exe_path, delay_seconds)
    elif sys.platform == "darwin":
        _install_launchagent(exe_path)
    else:
        _install_systemd_service(exe_path)

def uninstall_startup():
    if sys.platform == "win32":
        _uninstall_windows_task()
    elif sys.platform == "darwin":
        _uninstall_launchagent()
    else:
        _uninstall_systemd_service()
```

**Project file:** `src/ragtools/service/startup.py` (currently Windows-only)

---

## Installer vs Runtime Responsibilities

| Responsibility | Installer | Runtime |
|----------------|-----------|---------|
| Create data directories | Yes | Yes (fallback) |
| Register startup task | Yes (default) | Yes (auto-register on first run) |
| Add to PATH | Yes (optional) | No |
| Start service | Yes (optional) | No (CLI command) |
| Open browser | Yes (post-install) | Only on user-initiated launch |
| Clean PATH on uninstall | Yes | N/A |
| Prompt to delete user data | Yes (uninstall) | N/A |
| Stop service on uninstall | Yes | N/A |

---

## Error Handling

### "Service Not Running"
| Context | Behavior |
|---------|----------|
| CLI command | Print warning, fall back to direct mode or fail with guidance |
| MCP server | Use proxy mode if service up, direct mode if not |
| Launcher shortcut | Try to start service, poll, open browser |
| Admin panel URL | Browser shows "connection refused" — nothing we can do |

### "Port Already In Use"
- Check with health endpoint first (may be our own service)
- If health returns unexpected response: another app on the port
- Fail with clear message: "Port 21420 is in use by another application"

---

## Future Evolution Notes

### Current State (Windows-only)
- Task Scheduler for startup
- Inno Setup for installer
- VBScript for launcher and folder picker
- PyInstaller for exe bundling

### macOS Support (future)
- LaunchAgent plist for startup (replaces Task Scheduler)
- DMG or Homebrew for distribution (replaces Inno Setup)
- Python or shell script for launcher (replaces VBScript)
- PyInstaller .app bundle (same tool, different output)

### Abstraction Points Ready for macOS
- `config.py get_data_dir()` — already has platform detection stub
- `startup.py` — needs macOS LaunchAgent implementation
- `process.py` — already has Unix fallback (`os.kill`, `start_new_session`)
- `run.py _post_startup()` — platform-neutral (HTTP calls, no Windows API)

### Not Ready for macOS
- `installer.iss` — Windows-only, need separate macOS distribution
- `launch.vbs` / `pick_folder.vbs` — VBScript, Windows-only
- `rag.spec` — needs macOS BUNDLE step for .app output
- `build.py` — needs macOS signing/notarization steps

---

## Testing Guidance
- Test on both platforms when adding cross-platform features
- Use `sys.platform` checks, never `os.name` (less granular)
- Test with `sys.frozen = True` simulation for packaging paths
- Verify `127.0.0.1` binding works without firewall prompts
- Test health check polling under slow startup conditions
