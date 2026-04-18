# SOP: Fresh Install (Packaged)

| | |
|---|---|
| **Owner** | TBD (proposed: ops lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Install Reg on Windows using the supplied installer `.exe`. The installer bundles Python and all dependencies — no separate runtime required.

## Scope
Windows 11 only. macOS arm64 packaging ships as an unpacked bundle (see [Fresh Install (Dev)](Operational-SOPs-Installation-Fresh-Install-Dev) for macOS/Linux today). Does not cover upgrade-in-place (Phase 6 Release SOP) or nuclear cleanup (see [Nuclear Reset](Operational-SOPs-Repair-Nuclear-Reset)).

## Not sure which flow you want?

See the decision diagram on [Fresh Install (Dev)](Operational-SOPs-Installation-Fresh-Install-Dev#which-install-path-should-i-use).

## Trigger
- New Windows machine.
- End user wants Reg without touching Python.
- Provisioning a fresh service host.

## Preconditions
- [ ] Windows 11 target with admin rights (installer writes to `C:\Program Files\RAGTools\`).
- [ ] No prior Reg installation, or prior installation has been cleanly uninstalled.
- [ ] No process holding port **21420**. See [Port 21420 In Use](Runbooks-Port-21420-In-Use).
- [ ] Antivirus / SmartScreen policy permits unsigned installers (current builds are not code-signed — see the signing item in `docs/backlog-future-releases.md`).

## Inputs
- Installer: `RAGTools-Setup-<version>.exe` from the [GitHub Release page](https://github.com/taqat-techno/rag/releases).

## Steps

1. **Download** `RAGTools-Setup-<version>.exe` from the latest [GitHub Release](https://github.com/taqat-techno/rag/releases).

2. **Run the installer.** If Windows SmartScreen warns "unrecognized app":
   - Click **More info** → **Run anyway** (only if you trust the source).

3. **Accept installer defaults.** Inno Setup will:
   - Install binaries to `C:\Program Files\RAGTools\`.
   - Create the data/config directory at `%LOCALAPPDATA%\RAGTools\`.
   - Add `rag.exe` to system `PATH`.

4. **Open a fresh terminal.** A new shell is required for the updated `PATH` to take effect.

5. **Verify:**
   ```
   rag version
   rag doctor
   ```

6. **Optional — start the service now:**
   ```
   rag service start
   ```
   Then open `http://127.0.0.1:21420`.

7. **Optional — register auto-start on login:**
   ```
   rag service install
   ```
   Registers a Task Scheduler entry. See [Install Startup Task](Operational-SOPs-Service-Install-Startup-Task).

## Validation / expected result

- `rag version` prints the installed version.
- `rag doctor` exits `0`, reports config path at `%LOCALAPPDATA%\RAGTools\config.toml` (or notes "defaults only" if no config file yet), shows encoder available.
- If service was started: `curl http://127.0.0.1:21420/health` returns **200**.
- Start menu contains a "RAGTools" shortcut (if the installer created one).
- `%LOCALAPPDATA%\RAGTools\` exists.

## Failure modes

| Symptom | Likely cause | Fix / Runbook |
|---|---|---|
| SmartScreen blocks installer | Unsigned binary | Click **More info** → **Run anyway** if trust is established. Signing is tracked in `docs/backlog-future-releases.md`. |
| `rag` not found after install | PATH not refreshed in current shell | Open a new terminal. |
| `rag doctor` reports BLOCKER "port 21420 in use" | Another service on 21420 | [Port 21420 In Use](Runbooks-Port-21420-In-Use). |
| Installer reports "access denied" | No admin rights | Re-run installer as administrator. |
| Service will not start after install | Qdrant from a prior install still locked | [Qdrant Lock Contention](Runbooks-Qdrant-Lock-Contention). |
| `rag service start` returns immediately but `/health` never goes 200 | Encoder still loading (5-10 s) or failed | Check `%LOCALAPPDATA%\RAGTools\data\logs\service.log`. If load failed, see [Service Fails to Start](Runbooks-Service-Fails-to-Start). |

## Recovery / rollback

- Uninstall via **Settings → Apps & features → RAGTools**.
- The uninstaller removes program files and Task Scheduler entries but **preserves** `%LOCALAPPDATA%\RAGTools\` (data + config).
- For a full wipe, follow [Nuclear Reset](Operational-SOPs-Repair-Nuclear-Reset).

## Related code paths
- `installer.iss` — Inno Setup script.
- `scripts/build.py` — PyInstaller + Inno Setup orchestration.
- `src/ragtools/service/startup.py` — Task Scheduler registration.
- `src/ragtools/config.py:is_packaged` — installed-mode detection.

## Related commands
- `rag version`, `rag doctor`, `rag service start / stop / install / uninstall`.

## Change log
- 2026-04-18 — Initial draft.
