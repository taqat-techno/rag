# SOP: Winget Submission

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Submit the updated Reg installer manifest to `microsoft/winget-pkgs` after a GitHub release is live, so `winget install RAGTools.RAGTools` picks up the new version.

## Scope
The post-release winget-pkgs PR flow. Does not cover tagging or building the installer — see [Release Checklist](Development-SOPs-Release-Release-Checklist).

## Trigger
- A release has just been cut (installer `.exe` is attached on GitHub).
- Downstream users expect the latest Reg via `winget`.

## Preconditions
- [ ] GitHub release published with the `RAGTools-Setup-X.Y.Z.exe` asset attached.
- [ ] SHA256 of that installer computed.
- [ ] `winget/RAGTools.RAGTools.installer.yaml` updated with the new version and real SHA256 (done in [Release Checklist](Development-SOPs-Release-Release-Checklist) step 13).
- [ ] Either `wingetcreate` installed (`winget install Microsoft.WingetCreate`) or a manual PR approach ready.

## Inputs
- Released version `X.Y.Z`.
- GitHub release asset URL (public).
- SHA256 of the installer.

## Steps

### Option A — `wingetcreate update` (recommended)

1. **Run the update:**
   ```
   wingetcreate update RAGTools.RAGTools ^
     --version X.Y.Z ^
     --urls https://github.com/taqat-techno/rag/releases/download/vX.Y.Z/RAGTools-Setup-X.Y.Z.exe ^
     --submit
   ```
2. `wingetcreate` opens a PR against `microsoft/winget-pkgs` using the manifest template and the new asset URL + SHA256.
3. Wait for the pipeline validation comment on the PR. Address any manifest-lint findings.
4. A Microsoft maintainer merges after validation.

### Option B — manual PR

1. Fork `microsoft/winget-pkgs`.
2. Copy the contents of `winget/` in this repo into `manifests/r/RAGTools/RAGTools/X.Y.Z/` of the fork.
3. Verify the three manifest files:
   - `RAGTools.RAGTools.yaml` (version manifest)
   - `RAGTools.RAGTools.installer.yaml` (installer manifest — SHA256 must match)
   - `RAGTools.RAGTools.locale.en-US.yaml` (default locale manifest)
4. Open a PR from the fork to `microsoft/winget-pkgs`.
5. Wait for pipeline validation and maintainer review.

## Validation / expected result

- PR opens against `microsoft/winget-pkgs` with green pipeline validation.
- `winget search RAGTools` shows the new version after merge (propagation takes minutes to hours).
- `winget install RAGTools.RAGTools` on a clean Windows box installs the new version.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Pipeline validation fails "SHA256 mismatch" | Manifest SHA does not match the downloaded asset | Recompute SHA against the actual release asset; update manifest; push again. |
| Validation fails "installer not reachable" | Release asset URL wrong or release not yet public | Confirm the release is published; confirm the asset URL works in a browser. |
| Validation fails "manifest schema" | Manifest field types or required keys wrong | Compare against the latest version entry for this package in `microsoft/winget-pkgs`; or re-run `wingetcreate update`. |
| `winget search` still shows the old version after merge | Propagation lag | Wait; `winget source update`; retry. |
| Package rejected for policy reasons | Unsigned installer, missing publisher info | Address the reviewer's comment; not a technical fix — may require signing work. |

## Recovery / rollback

- A merged winget entry cannot be unpublished quickly. If a broken installer was published:
  1. Immediately publish a patch release (`X.Y.Z+1`) with the fix.
  2. Submit a winget PR for the patch.
  3. Affected users must `winget upgrade RAGTools.RAGTools`.

## Related code paths

- `winget/RAGTools.RAGTools.yaml`
- `winget/RAGTools.RAGTools.installer.yaml`
- `winget/RAGTools.RAGTools.locale.en-US.yaml`
- `RELEASING.md` steps 11-13.

## Related commands

- `winget install Microsoft.WingetCreate`
- `wingetcreate update RAGTools.RAGTools ...`
- `certutil -hashfile RAGTools-Setup-X.Y.Z.exe SHA256`

## Change log
- 2026-04-18 — Initial draft.
