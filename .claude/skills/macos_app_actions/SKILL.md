# macOS App & System Actions

> Reusable reference for macOS OS integration in RAG Tools.
> Use this when adding macOS support or implementing platform-specific features.

## Purpose
Guide future macOS development: LaunchAgents, app bundles, Homebrew distribution, notarization, and macOS-native conventions.

## When to Use
- Adding macOS support to the application
- Creating a macOS installer or distribution
- Implementing login-item/startup behavior on macOS
- Adapting file paths for macOS conventions
- Building a macOS PyInstaller bundle

---

## Startup on Login

### Recommended: LaunchAgent (plist)
**Location:** `~/Library/LaunchAgents/com.taqatechno.ragtools.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.taqatechno.ragtools</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/rag</string>
        <string>service</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>~/Library/Logs/RAGTools/service.log</string>
    <key>StandardErrorPath</key>
    <string>~/Library/Logs/RAGTools/service.log</string>
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
```

### Management Commands
```bash
# Install
launchctl load ~/Library/LaunchAgents/com.taqatechno.ragtools.plist

# Start/Stop
launchctl start com.taqatechno.ragtools
launchctl stop com.taqatechno.ragtools

# Uninstall
launchctl unload ~/Library/LaunchAgents/com.taqatechno.ragtools.plist
rm ~/Library/LaunchAgents/com.taqatechno.ragtools.plist

# Check status
launchctl list | grep ragtools
```

### From Python
```python
import plistlib, subprocess
from pathlib import Path

LABEL = "com.taqatechno.ragtools"
PLIST_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"

def install_launchagent(exe_path: str):
    plist = {
        "Label": LABEL,
        "ProgramArguments": [exe_path, "service", "run"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
    }
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "load", str(PLIST_PATH)])

def uninstall_launchagent():
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)], check=False)
    PLIST_PATH.unlink(missing_ok=True)

def is_installed():
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    return LABEL in r.stdout
```

### LaunchAgents vs LaunchDaemons
| | LaunchAgents | LaunchDaemons |
|---|---|---|
| Location | `~/Library/LaunchAgents/` | `/Library/LaunchDaemons/` |
| Runs as | Current user | root |
| Requires admin | No | Yes |
| GUI access | Yes | No |
| **Use for RAGTools** | **Yes** | No |

### Anti-Patterns
- Do NOT use LaunchDaemons for user-level apps
- Do NOT use deprecated `LSSharedFileList` API
- Do NOT put plists in `/Library/` without admin — use `~/Library/`

---

## App Bundle (.app)

### Structure
```
RAGTools.app/
  Contents/
    Info.plist
    MacOS/
      ragtools        # Main executable
    Resources/
      icon.icns       # App icon (must be .icns format)
```

### Key Info.plist Keys
- `LSBackgroundOnly: true` — pure background service, no Dock icon
- `LSUIElement: true` — no Dock icon but can have menu bar icon
- `CFBundleIdentifier: com.taqatechno.ragtools` — required for notarization

### PyInstaller macOS Bundle
```python
# In rag.spec, add BUNDLE step:
app = BUNDLE(exe, a.binaries, a.datas,
    name='RAGTools.app',
    icon='icon.icns',
    bundle_identifier='com.taqatechno.ragtools',
    info_plist={'LSBackgroundOnly': True})
```

### Menu Bar Icon (Status Bar)
Use `rumps` library for a lightweight status bar app:
```python
import rumps
class RAGToolsStatusBar(rumps.App):
    @rumps.clicked("Open Admin Panel")
    def open_panel(self, _):
        import webbrowser
        webbrowser.open("http://localhost:21420")
    @rumps.clicked("Quit")
    def quit(self, _):
        rumps.quit_application()
RAGToolsStatusBar("RAG", icon="icon.png").run()
```

---

## Homebrew Distribution

### Formula (pip-installable)
```ruby
class Ragtools < Formula
  include Language::Python::Virtualenv
  desc "Local-first Markdown RAG system"
  homepage "https://github.com/taqat-techno/rag"
  url "https://files.pythonhosted.org/packages/.../ragtools-2.2.0.tar.gz"
  sha256 "..."
  depends_on "python@3.12"
  # ... resource blocks for dependencies
  def install
    virtualenv_install_with_resources
  end
  service do
    run [opt_bin/"rag", "service", "run"]
    keep_alive true
  end
end
```

### Cask (pre-built .app)
```ruby
cask "ragtools" do
  version "2.2.0"
  url "https://github.com/taqat-techno/rag/releases/download/v#{version}/RAGTools-macos.dmg"
  app "RAG Tools.app"
  uninstall launchctl: "com.taqatechno.ragtools"
  zap trash: "~/Library/Application Support/RAGTools"
end
```

Users install with: `brew tap taqat-techno/tools && brew install ragtools`

`brew services start ragtools` auto-generates and loads LaunchAgent.

---

## Gatekeeper & Notarization

### Without Apple Developer Account
Users must: right-click > Open (bypass Gatekeeper once).

### With Apple Developer Account ($99/year)
```bash
# Sign
codesign --deep --force --sign "Developer ID Application: ..." --options runtime RAGTools.app

# Notarize
xcrun notarytool submit RAGTools.zip --apple-id ... --wait
xcrun stapler staple RAGTools.app
```

---

## File System Conventions

| Content | macOS Path | Windows Equivalent |
|---------|-----------|-------------------|
| User data (DB, config) | `~/Library/Application Support/RAGTools/` | `%LOCALAPPDATA%\RAGTools\` |
| Model cache | `~/Library/Caches/RAGTools/` | `%LOCALAPPDATA%\Programs\RAGTools\model_cache\` |
| Logs | `~/Library/Logs/RAGTools/` | `%LOCALAPPDATA%\RAGTools\logs\` |
| LaunchAgent | `~/Library/LaunchAgents/` | Task Scheduler |

### Python Detection
```python
import sys, os
from pathlib import Path

if sys.platform == "darwin":
    data_dir = Path.home() / "Library/Application Support/RAGTools"
    cache_dir = Path.home() / "Library/Caches/RAGTools"
    log_dir = Path.home() / "Library/Logs/RAGTools"
elif sys.platform == "win32":
    data_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "RAGTools"
    cache_dir = data_dir
    log_dir = data_dir / "logs"
```

---

## Browser Opening
- `webbrowser.open(url)` works on macOS (calls `open` command internally)
- Direct: `subprocess.run(['open', 'http://localhost:21420'])`
- Background: `subprocess.run(['open', '-g', 'http://...'])` — browser doesn't come to front
- Binding to `127.0.0.1` (not `0.0.0.0`) avoids triggering macOS firewall dialog

---

## Testing Guidance
- Test LaunchAgent load/unload cycle
- Verify `launchctl list` shows the service
- Test reboot behavior
- Test .app bundle in Finder (icon, name)
- Test Gatekeeper behavior with unsigned app
- Verify logs appear in `~/Library/Logs/`
