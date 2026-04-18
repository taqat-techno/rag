# SOP: Release Checklist

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Cut a new Reg release — version bump, local verification, tag, GitHub Actions release, winget update.

## Scope
The full release flow for Windows installer, macOS bundle, and winget. The authoritative checklist is `RELEASING.md` in the repo root; this SOP wraps it with ownership, validation, and failure matrix. **If this SOP and `RELEASING.md` disagree, `RELEASING.md` wins.**

## Trigger
- A version is ready to ship (features merged, tests green).
- A hotfix is ready for a patch release.

## Preconditions
- [ ] All tests green on CI (`main` branch).
- [ ] [Release-gate test matrix](Development-SOPs-Testing-Install-Upgrade-Repair-Test-Matrix#matrix--release-gate) passed on Windows and macOS.
- [ ] `docs/RELEASE_LIFECYCLE.md` reviewed; no new code path violates the lifecycle contract.
- [ ] You have push access to the repo and to winget-pkgs (or an approver will handle the winget PR).
- [ ] Repo is on `main`, clean working tree.

## Inputs
- Target version `X.Y.Z` (semver).
- Whether this is a pre-release (tag `vX.Y.Z-beta.N`).

## Prepare (before tagging)

1. **Run the full test suite:**
   ```
   pytest
   ```

2. **Verify local build works:**
   ```
   python scripts/build.py --no-model
   ```
   Catches PyInstaller issues before CI spends minutes on them.

3. **Bump version in all three places:**
   - `pyproject.toml` → `version = "X.Y.Z"`
   - `src/ragtools/__init__.py` → `__version__ = "X.Y.Z"`
   - `installer.iss` → `#define MyAppVersion "X.Y.Z"`

4. **Update winget manifest placeholders:**
   - `winget/RAGTools.RAGTools.installer.yaml` — version field now; real SHA256 after release.

5. **Walk the lifecycle gate** (each box must be checked for a release to ship):
   - [ ] No new code path writes user data into the install directory.
   - [ ] Any schema change bumped its version AND ships a migration step (`config.toml` `version`, SQLite `PRAGMA user_version`, or Qdrant collection dim check).
   - [ ] Dev-mode startup (`python -m ragtools.service.run` from source) does not touch `%LOCALAPPDATA%\RAGTools\` or register a startup task.
   - [ ] Installer manually tested on a machine that already has the previous version installed — upgrade path preserves user data.
   - [ ] Uninstall manually tested with the opt-in prompt answered both YES (full wipe) and NO (keep data) and both paths behave correctly.
   - [ ] `docs/RELEASE_LIFECYCLE.md` is still accurate for this version.

6. **Commit:**
   ```
   git commit -m "Release vX.Y.Z"
   ```

## Release

7. **Tag:**
   ```
   git tag vX.Y.Z
   ```

8. **Push:**
   ```
   git push origin main --tags
   ```

9. **GitHub Actions** builds and creates the release automatically (Windows PyInstaller + Inno Setup installer; macOS arm64 bundle).

10. **Verify the release on GitHub:** installer `.exe` and portable `.zip` attached; release notes match the changelog.

## Post-release

11. **Download the installer** from the release page.

12. **Compute SHA256:**
    ```
    certutil -hashfile RAGTools-Setup-X.Y.Z.exe SHA256
    ```

13. **Update winget manifest** `winget/RAGTools.RAGTools.installer.yaml` with the real SHA256.

14. **Submit winget PR.** Follow [Winget Submission](Development-SOPs-Release-Winget-Submission) for the end-to-end PR flow.

## Version numbering

- **X.Y.Z** — semantic versioning.
- **X** — major (breaking changes).
- **Y** — minor (new features).
- **Z** — patch (bug fixes).
- Pre-release tags: `vX.Y.Z-beta.N` — GitHub Actions will mark the release as pre-release.

## Validation / expected result

- GitHub release exists with installer + portable zip artifacts.
- Installer runs on a clean Windows VM; `rag doctor` reports OK.
- Upgrade over the previous version preserves user data (checked during prepare).
- winget manifest updated; PR open or merged at `microsoft/winget-pkgs`.
- `pyproject.toml`, `__init__.py`, and `installer.iss` all show the new version on `main`.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Actions build fails on PyInstaller step | New dependency not in `rag.spec` hidden imports | Add the import; bump a patch; re-tag. |
| Actions build fails on Inno Setup | Missing asset or bad path in `installer.iss` | Fix; re-run. |
| Installer runs but upgrade loses data | Lifecycle gate was not actually verified | This is a release-blocker. Pull the release; investigate; only re-release after the gate is green. |
| SHA256 mismatch flagged by winget | Recomputed on a different file or after a rebuild | Re-compute against the actual attached release asset. |
| Pre-release attached as "latest" | Tag did not include `-beta.N` | Rename/re-tag. |
| Tests pass locally, fail on CI | Platform-specific behavior | Reproduce on the matching CI runner; do not bypass with `--no-verify`. |

## Recovery / rollback

- **If the tag is pushed but the release is broken:** delete the GitHub release, delete the tag locally and remotely (`git tag -d vX.Y.Z && git push --delete origin vX.Y.Z`), fix, re-tag. Do not silently overwrite a released tag.
- **If the winget PR is wrong:** update it with the correct SHA256 and re-request review.

## Related code paths

- `RELEASING.md` — canonical checklist (this SOP mirrors it).
- `docs/RELEASE_LIFECYCLE.md` — the lifecycle contract every release must honor.
- `scripts/build.py` — PyInstaller + Inno Setup orchestration.
- `.github/workflows/release.yml` — CI release automation.
- `installer.iss` — Inno Setup script.
- `winget/` — winget manifest files.

## Related commands

- `pytest`, `python scripts/build.py --no-model`.
- `git tag vX.Y.Z && git push origin main --tags`.
- `certutil -hashfile <file> SHA256`.

## Change log
- 2026-04-18 — Initial draft (ports the current `RELEASING.md` v2.4.2 content).
