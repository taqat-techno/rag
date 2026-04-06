# Release Checklist

## Prepare

1. Ensure all tests pass: `pytest`
2. Verify local build works: `python scripts/build.py --no-model` (catches PyInstaller issues before CI)
3. Update version in **three** places:
   - `pyproject.toml` → `version = "X.Y.Z"`
   - `src/ragtools/__init__.py` → `__version__ = "X.Y.Z"`
   - `installer.iss` → `#define MyAppVersion "X.Y.Z"`
4. Update `winget/` manifests with new version and placeholder SHA256
5. Commit: `git commit -m "Release vX.Y.Z"`

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
