# macOS Release Playbook

> Execution guide for building, packaging, and releasing RAG Tools for macOS users.
> Development stays on Windows. CI builds the macOS artifact on GitHub Actions macOS runners.

## Overview

The macOS release is built entirely in CI — you push a tag from Windows, GitHub Actions runs on a macOS runner, produces a `.app` bundle or DMG, and attaches it to the release. No Mac hardware needed for development.

---

## Architecture: What Changes for macOS

| Component | Windows | macOS |
|-----------|---------|-------|
| Executable | `rag.exe` (PyInstaller) | `rag` binary inside `RAG Tools.app` |
| Installer | Inno Setup `.exe` | DMG with drag-to-Applications |
| Startup on login | Task Scheduler (`schtasks`) | LaunchAgent plist |
| Data directory | `%LOCALAPPDATA%\RAGTools\` | `~/Library/Application Support/RAGTools/` |
| Logs | `%LOCALAPPDATA%\RAGTools\logs\` | `~/Library/Logs/RAGTools/` |
| Service process | `CREATE_NO_WINDOW` flag | `start_new_session=True` |
| Icon | `app.ico` | `app.icns` |
| Code signing | Optional (SmartScreen) | Required for smooth UX (Gatekeeper) |

**What works unchanged:** Python code, FastAPI service, CLI, search, indexing, MCP server, admin panel, htmx templates — all cross-platform.

---

## Phased Release Strategy

### Phase 1: Unsigned .app in ZIP (First Release)
- **Cost:** $0
- **CI:** `macos-14` runner (ARM64/Apple Silicon)
- **Artifact:** `RAGTools-X.Y.Z-macOS-arm64.zip` containing `RAG Tools.app`
- **User install:** Download, unzip, move to `/Applications/`, right-click > Open
- **Gatekeeper:** User must right-click > Open on first launch
- **Best for:** Internal testing, early adopters

### Phase 2: DMG + Homebrew (Recommended First Public Release)
- **Cost:** $0
- **CI:** Adds `create-dmg` step
- **Artifact:** `RAGTools-X.Y.Z-macOS-arm64.dmg`
- **User install:** `brew install --cask taqat-techno/ragtools/ragtools`
- **Gatekeeper:** Homebrew auto-strips quarantine — **no warnings**
- **Best for:** Public distribution without Apple Developer account

### Phase 3: Signed + Notarized (Polished)
- **Cost:** $99/year Apple Developer account
- **CI:** Adds signing + notarization steps
- **Artifact:** Signed, notarized, stapled DMG
- **User install:** Double-click DMG, drag to Applications, done — zero warnings
- **Best for:** Professional distribution

---

## Prerequisites

### For Phase 1 (minimum)
- [ ] Create `app.icns` icon file (convert from `app.ico` or `Vector.png`)
- [ ] Create `rag-macos.spec` PyInstaller spec with `BUNDLE()` step
- [ ] Update `.github/workflows/release.yml` with macOS job

### For Phase 2 (add Homebrew)
- [ ] Create tap repo: `github.com/taqat-techno/homebrew-ragtools`
- [ ] Create `Casks/ragtools.rb` cask file
- [ ] Add DMG creation step to CI

### For Phase 3 (add signing)
- [ ] Apple Developer Program enrollment ($99/year)
- [ ] Generate "Developer ID Application" certificate
- [ ] Export as `.p12`, base64-encode, store in GitHub Secrets
- [ ] Generate app-specific password at appleid.apple.com
- [ ] Store all secrets in GitHub repo settings

---

## CI Workflow: macOS Build Job

Add this job to `.github/workflows/release.yml`:

```yaml
build-macos:
  runs-on: macos-14  # Apple Silicon M1
  steps:
    - uses: actions/checkout@v4

    - uses: actions/setup-python@v5
      with:
        python-version: '3.12'

    - name: Cache pip
      uses: actions/cache@v4
      with:
        path: ~/Library/Caches/pip
        key: pip-macos-arm64-${{ hashFiles('pyproject.toml') }}

    - name: Cache model
      uses: actions/cache@v4
      with:
        path: build/model_cache
        key: model-all-MiniLM-L6-v2-v1

    - name: Install dependencies
      run: pip install -e ".[dev,build]"

    - name: Run tests
      run: pytest -q

    - name: Build with PyInstaller
      run: |
        python scripts/build.py  # Downloads model + runs PyInstaller
        # Or if using separate spec:
        # pyinstaller rag-macos.spec --noconfirm --clean

    - name: Ad-hoc sign (required for ARM64)
      run: codesign --force --deep -s - "dist/RAG Tools.app"

    - name: Verify build
      run: "dist/RAG Tools.app/Contents/MacOS/rag" version

    - name: Create DMG  # Phase 2+
      run: |
        brew install create-dmg
        VERSION=${GITHUB_REF_NAME#v}
        create-dmg \
          --volname "RAG Tools" \
          --window-pos 200 120 \
          --window-size 600 400 \
          --icon-size 100 \
          --icon "RAG Tools.app" 150 190 \
          --hide-extension "RAG Tools.app" \
          --app-drop-link 450 190 \
          --no-internet-enable \
          "dist/RAGTools-${VERSION}-macOS-arm64.dmg" \
          "dist/RAG Tools.app"

    - name: Upload artifact
      uses: actions/upload-artifact@v4
      with:
        name: macos-release
        path: dist/RAGTools-*-macOS-arm64.dmg
```

### Runner Details

| Runner | Arch | RAM | Cost (public) | Cost (private) |
|--------|------|-----|---------------|----------------|
| `macos-13` | Intel x86_64 | 14 GB | Free | $0.08/min |
| `macos-14` | ARM64 (M1) | 7 GB | Free | $0.08/min |
| `macos-15` | ARM64 (M1) | 7 GB | Free | $0.08/min |

**Recommendation:** Use `macos-14` for ARM64 builds (covers M1/M2/M3/M4 Macs). Intel Macs are legacy — skip unless requested.

---

## PyInstaller macOS Spec File

Create `rag-macos.spec` (same as `rag.spec` but with `BUNDLE()` step):

Key differences from Windows spec:
```python
# After COLLECT, add:
app = BUNDLE(
    coll,
    name='RAG Tools.app',
    icon='app.icns',  # macOS icon format
    bundle_identifier='com.taqattechno.ragtools',
    info_plist={
        'CFBundleName': 'RAG Tools',
        'CFBundleDisplayName': 'RAG Tools',
        'CFBundleVersion': '2.2.0',
        'CFBundleShortVersionString': '2.2.0',
        'LSMinimumSystemVersion': '13.0',
        'LSUIElement': True,  # No dock icon
        'NSHighResolutionCapable': True,
    },
)
```

**Icon conversion** (in CI):
```bash
# From the 1024x1024 Vector.png:
mkdir -p app.iconset
sips -z 1024 1024 src/ragtools/service/static/Vector.png --out app.iconset/icon_512x512@2x.png
sips -z 512 512 src/ragtools/service/static/Vector.png --out app.iconset/icon_512x512.png
sips -z 256 256 src/ragtools/service/static/Vector.png --out app.iconset/icon_256x256.png
sips -z 128 128 src/ragtools/service/static/Vector.png --out app.iconset/icon_128x128.png
sips -z 64 64 src/ragtools/service/static/Vector.png --out app.iconset/icon_32x32@2x.png
sips -z 32 32 src/ragtools/service/static/Vector.png --out app.iconset/icon_32x32.png
sips -z 16 16 src/ragtools/service/static/Vector.png --out app.iconset/icon_16x16.png
iconutil -c icns app.iconset -o app.icns
```

---

## Code Signing (Phase 3)

### In CI workflow:
```yaml
- name: Import signing certificate
  uses: apple-actions/import-codesign-certs@v3
  with:
    p12-file-base64: ${{ secrets.APPLE_CERTIFICATE_P12 }}
    p12-password: ${{ secrets.APPLE_CERTIFICATE_PASSWORD }}

- name: Sign app
  run: |
    codesign --deep --force --verify --verbose \
      --sign "Developer ID Application: TaqaTechno (${{ secrets.APPLE_TEAM_ID }})" \
      --options runtime --timestamp \
      "dist/RAG Tools.app"

- name: Notarize
  run: |
    ditto -c -k --keepParent "dist/RAG Tools.app" dist/RAGTools.zip
    xcrun notarytool submit dist/RAGTools.zip \
      --apple-id "${{ secrets.APPLE_ID }}" \
      --password "${{ secrets.APP_SPECIFIC_PASSWORD }}" \
      --team-id "${{ secrets.APPLE_TEAM_ID }}" \
      --wait
    xcrun stapler staple "dist/RAG Tools.app"
```

### GitHub Secrets needed:
| Secret | Source |
|--------|--------|
| `APPLE_CERTIFICATE_P12` | Export from Keychain, base64-encode |
| `APPLE_CERTIFICATE_PASSWORD` | Password set during export |
| `APPLE_ID` | Your Apple ID email |
| `APP_SPECIFIC_PASSWORD` | appleid.apple.com > App-Specific Passwords |
| `APPLE_TEAM_ID` | developer.apple.com > Membership > Team ID |

---

## Homebrew Distribution (Phase 2+)

### Create tap repository
```bash
# On GitHub: create repo taqat-techno/homebrew-ragtools
mkdir -p Casks
```

### Cask file (`Casks/ragtools.rb`):
```ruby
cask "ragtools" do
  version "2.2.0"
  sha256 "SHA256_OF_DMG"

  url "https://github.com/taqat-techno/rag/releases/download/v#{version}/RAGTools-#{version}-macOS-arm64.dmg"
  name "RAG Tools"
  desc "Local-first Markdown RAG system for Claude CLI"
  homepage "https://github.com/taqat-techno/rag"

  depends_on arch: :arm64

  app "RAG Tools.app"

  postflight do
    system_command "/bin/mkdir",
      args: ["-p", "#{Dir.home}/Library/Application Support/RAGTools"]
  end

  uninstall quit: "com.taqattechno.ragtools"
  zap trash: [
    "~/Library/Application Support/RAGTools",
    "~/Library/Logs/RAGTools",
    "~/Library/LaunchAgents/com.taqattechno.ragtools.plist",
  ]
end
```

### User install:
```bash
brew tap taqat-techno/ragtools
brew install --cask ragtools
```

**Homebrew auto-strips quarantine** — no Gatekeeper warnings even without signing.

---

## macOS App Behavior: What to Implement

### Startup on Login
Replace Task Scheduler with LaunchAgent in `src/ragtools/service/startup.py`:
```python
if sys.platform == "darwin":
    # Install LaunchAgent plist
    plist = {...}
    plist_path = Path.home() / "Library/LaunchAgents/com.taqattechno.ragtools.plist"
    plistlib.dump(plist, open(plist_path, "wb"))
    subprocess.run(["launchctl", "load", str(plist_path)])
```

### Data Paths
Already partially implemented in `config.py`. Add macOS detection:
```python
if sys.platform == "darwin":
    data_dir = Path.home() / "Library/Application Support/RAGTools"
```

### Browser Opening
`webbrowser.open()` works on macOS. No changes needed.

---

## Risks and Constraints

| Risk | Impact | Mitigation |
|------|--------|------------|
| `macos-14` runner has only 7GB RAM | torch + PyInstaller may OOM | Fall back to `macos-13` (14GB, Intel) |
| No universal binary | Intel Mac users can't use ARM64 build | Ship Intel build from `macos-13` if needed |
| Unsigned app friction | Users must right-click > Open | Document clearly; use Homebrew (strips quarantine) |
| Apple Developer cost | $99/year for signing | Defer to Phase 3; Homebrew eliminates need |
| CI build time | 10-15 min on macOS runner | Cache pip + model aggressively |
| torch ARM64 wheels | Large download (~2GB) | Cache in CI |

---

## Testing Guidance

### For CI builds:
- Verify `rag version` runs from the built app
- Verify `rag service start` launches (may need to mock Qdrant)
- Verify DMG mounts and contains the app
- Verify ad-hoc signature: `codesign --verify "dist/RAG Tools.app"`

### For manual testing (on a real Mac):
- Download from GitHub release
- Unzip / mount DMG
- Drag to Applications
- Right-click > Open (first time)
- Verify admin panel opens at `localhost:21420`
- Verify `rag service start/stop` from Terminal
- Test file watcher with a project folder
- Test MCP connection from Claude Code on Mac

---

## Execution Checklist: First macOS Release

1. [ ] Create `app.icns` from `Vector.png` (can do in CI)
2. [ ] Create `rag-macos.spec` with BUNDLE step
3. [ ] Add `build-macos` job to `.github/workflows/release.yml`
4. [ ] Add macOS platform detection to `config.py` `get_data_dir()`
5. [ ] Push a test tag (e.g., `v2.3.0-beta.1`) to trigger CI
6. [ ] Verify macOS artifact appears on the release
7. [ ] Download and test on a real Mac (or ask a Mac user)
8. [ ] If working: tag a real release
9. [ ] Later: add Homebrew tap (Phase 2)
10. [ ] Later: add signing + notarization (Phase 3)
