# Quick Install (Packaged)

| | |
|---|---|
| **Owner** | TBD (proposed: ops lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

Windows installer (`.exe`) from GitHub Releases. Bundles Python and all dependencies — no separate Python install required.

## Steps

1. **Download** `RAGTools-Setup-<version>.exe` from the latest [GitHub Release](https://github.com/taqat-techno/rag/releases).

2. **Run the installer.** Inno Setup will:
   - Install binaries to `C:\Program Files\RAGTools\`.
   - Create the data/config directory at `%LOCALAPPDATA%\RAGTools\`.
   - Add `rag.exe` to `PATH`.

3. **Verify** from a new terminal:
   ```
   rag version
   rag doctor
   ```

4. **Optional — register auto-start:**
   ```
   rag service install
   ```
   Registers a Windows Task Scheduler entry so the service starts on login. See [Install Startup Task](Operational-SOPs-Service-Install-Startup-Task).

5. **Open the admin panel.** Launch "RAGTools" from the Start menu, or browse to `http://127.0.0.1:21420`.

## What changed on disk

| Path | Purpose |
|---|---|
| `C:\Program Files\RAGTools\` | Application binaries (read-only after install) |
| `%LOCALAPPDATA%\RAGTools\config.toml` | User config (created on first run) |
| `%LOCALAPPDATA%\RAGTools\data\` | Qdrant + state DB + logs |

See [File Layout](Reference-File-Layout).

## Uninstall

Use Windows "Apps & features" to remove. The uninstaller removes binaries and Task Scheduler entries but **preserves** `%LOCALAPPDATA%\RAGTools\` by default. For deeper resets see [Repair Broken Installation](Operational-SOPs-Repair-Repair-Broken-Installation).

## Next

- [Add a Project](Operational-SOPs-Projects-Add-a-Project) via the admin panel.
- [Installed App vs Standalone](Operational-SOPs-Runtime-Installed-App-vs-Standalone-Behavior) — behavioral differences relative to a dev install.
