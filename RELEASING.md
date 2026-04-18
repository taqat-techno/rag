# Release Checklist

> **Every release must comply with `docs/RELEASE_LIFECYCLE.md`.**
> Read that document before cutting any new version. The rules in section 9
> of that doc are the release gate for this repo.

## Prepare

1. Ensure all tests pass: `pytest`
2. Verify local build works: `python scripts/build.py --no-model` (catches PyInstaller issues before CI)
3. Update version in **three** places:
   - `pyproject.toml` → `version = "X.Y.Z"`
   - `src/ragtools/__init__.py` → `__version__ = "X.Y.Z"`
   - `installer.iss` → `#define MyAppVersion "X.Y.Z"`
4. Update `winget/` manifests with new version and placeholder SHA256
5. **Lifecycle gate** — confirm each of these before committing:
   - [ ] No new code path writes user data into the install directory
   - [ ] Any schema change bumped its version AND ships a migration step
         (`config.toml` `version`, SQLite `PRAGMA user_version`, or Qdrant
         collection dim check)
   - [ ] Dev-mode startup (`python -m ragtools.service.run` from source)
         does not touch `%LOCALAPPDATA%\RAGTools\` or register a Startup task
   - [ ] Installer manually tested on a machine that already has the
         previous version installed — upgrade path preserves user data
   - [ ] Uninstall manually tested with the opt-in prompt answered both
         YES (full wipe) and NO (keep data) and both paths behave correctly
   - [ ] `docs/RELEASE_LIFECYCLE.md` is still accurate for this version
6. Commit: `git commit -m "Release vX.Y.Z"`

## Release

6. Tag: `git tag vX.Y.Z`
7. Push: `git push origin main --tags`
8. GitHub Actions builds and creates the release automatically
9. Verify the release on GitHub: installer `.exe` and portable `.zip` attached

## Post-Release

10. Download the installer from the release
11. Compute SHA256: `certutil -hashfile RAGTools-Setup-X.Y.Z.exe SHA256`
12. Update `winget/RAGTools.RAGTools.installer.yaml` with the real SHA256
13. Submit PR to `microsoft/winget-pkgs` (or use `wingetcreate update`)

## Version Numbering

- **X.Y.Z** — semantic versioning
- **X** — major (breaking changes)
- **Y** — minor (new features)
- **Z** — patch (bug fixes)
- Pre-release tags: `vX.Y.Z-beta.1` → marked as pre-release on GitHub
