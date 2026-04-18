# Release Ship

> Orchestrate a safe release: run the portability gate, bump version, commit, tag, push, let GitHub Actions build Windows + macOS (+ Linux, when supported) artifacts, verify the declared platform set, and fail loudly if anything is missing. Ends with a clear success/failure summary. Does not bypass `RELEASING.md` or `docs/RELEASE_LIFECYCLE.md` — this skill executes them.

**Compatibility is not my job.** This skill is the release-execution gate. **`/portability-audit`** is the platform-compatibility gate; I only read its Release Gate block and abort if it says FAIL. I do not diagnose compatibility issues myself.

## Inputs the user might pass

- **Version** (required) — `2.4.3`, `v2.4.3`, or `version 2.4.3`. Must be semver `X.Y.Z` or `X.Y.Z-<pre>.N`.
- `dry-run` — validate everything; do not commit, tag, push, or upload.
- `skip-build` — skip the local PyInstaller verify.
- `skip-tag` — bump and commit only.
- `skip-upload` — build and tag locally; do not push.
- `draft` — ask GitHub Actions to publish as a draft.
- `allow-dirty` — allow uncommitted unrelated changes.
- `notes-file <path>` — release notes to attach.
- `audit-from <path>` — consume an existing `tasks/portability-audit-reports/<ISO>.md` instead of running a fresh audit. Error if file is older than the latest commit on HEAD.
- `skip-audit` — **requires** `dry-run`. Skips the portability gate; will refuse on a real release.
- Free-form direction ("re-run 2.4.2 as a draft to test the macOS build") — take as authoritative scope.

## Reference material (read these before doing anything)

- `RELEASING.md` — canonical step list. This skill **mirrors** it; if they disagree, `RELEASING.md` wins.
- `docs/RELEASE_LIFECYCLE.md` — non-negotiable lifecycle contract.
- `.github/workflows/release.yml` — CI automation that actually produces artifacts.
- `scripts/build.py` — local PyInstaller + Inno Setup orchestrator.
- `installer.iss`, `pyproject.toml`, `src/ragtools/__init__.py` — **the three version locations**.
- `.claude/skills/portability-audit/SKILL.md` — the compatibility gate; this skill depends on its Release Gate block format.
- `.claude/skills/macos_release/SKILL.md` — macOS-specific build notes.

## Preflight — stop if any of these fails

### PF-1 · Semver
Version matches `^v?\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$`. Normalize internally to `X.Y.Z`; tag uses `vX.Y.Z`.

### PF-2 · Working tree
`git -c safe.directory='*' status --porcelain` empty. Override with `allow-dirty`.

### PF-3 · Branch
On `master` or `main`. Releases off a feature branch require explicit confirmation.

### PF-4 · Local tests
`pytest`. Skipping tests requires `dry-run` mode.

### PF-5 · Tag uniqueness
`git -c safe.directory='*' tag -l "v<version>"` empty. If it exists and the user did not say "replace draft", abort.

### PF-6 · GitHub CLI + auth
`gh auth status`. Per `CLAUDE.md` "Auto-Switch GitHub Account": if the remote is under `taqat-techno/*` and the active account is `ahmed-lakosha`, auto-switch to `a-lakosha` before any write operation, and switch back after.

### PF-7 · Build tools (unless `skip-build`)
- `python --version` → 3.10+
- `pip install -e ".[build]"` deps available
- `iscc` (Inno Setup compiler) on PATH — Windows-only verify path.

### PF-8 · Version coherence
Read current `pyproject.toml` version. If user-supplied version is older or equal, require explicit confirmation.

### PF-9 · Portability gate (NEW — the compatibility gate)

This is the integration point with `/portability-audit`.

1. **Obtain a Release Gate block.** In priority order:
   - If `audit-from <path>` was passed: open that file. If its modification time is older than the latest commit on HEAD, abort with "stale audit — re-run /portability-audit or remove --audit-from".
   - Otherwise: invoke `/portability-audit write-report` inline. Wait for the report. Read the generated file under `tasks/portability-audit-reports/<ISO>.md`.
   - If `skip-audit` was passed **and** `dry-run` is set: skip this step with a warning. On a real release, `skip-audit` alone is rejected.

2. **Parse the `## Release Gate` block.** Expected labels, verbatim:
   - `Status: PASS | FAIL`
   - `Windows: READY | BLOCKED | SOURCE_ONLY | NOT_SUPPORTED`
   - `macOS: READY | BLOCKED | SOURCE_ONLY | NOT_SUPPORTED`
   - `Linux: READY | BLOCKED | SOURCE_ONLY | NOT_SUPPORTED`
   - `Critical blockers: <N>`
   - `High findings: <N>`
   - `Blocking reasons:` (bullet list)

   If any label is missing or malformed, abort with "portability gate protocol violation — /portability-audit did not emit a valid Release Gate block".

3. **Apply the gate:**
   - `Status: FAIL` → **abort the release**. Surface the Blocking reasons verbatim.
   - Any platform lane = `BLOCKED` → **abort**.
   - `Status: PASS` → proceed to PF-10. Note `High findings` in the release summary as warnings.

4. **Cache the verdict** — keep the parsed block in memory for the final summary (Phase F).

### PF-10 · Lifecycle gate
Walk the 6 checkboxes from `RELEASING.md` step 5 interactively. Each requires explicit acknowledgment. Do not infer answers. Any "no" / "not verified" → abort.

## Release flow

Stop at the first failure. Each failure surfaces the recovery command.

### Phase A — Prepare

1. Bump version in all three files:
   - `pyproject.toml` → `version = "X.Y.Z"`
   - `src/ragtools/__init__.py` → `__version__ = "X.Y.Z"`
   - `installer.iss` → `#define MyAppVersion "X.Y.Z"`
2. Update winget manifests under `winget/` with the new version and placeholder SHA256.
3. Optional local verify build unless `skip-build`:
   ```
   python scripts/build.py --no-model
   ```
   If this fails, abort.
4. Show `git diff`. Ask the user to confirm before committing.

### Phase B — Tag

5. Commit:
   ```
   git -c safe.directory='*' commit -m "Release vX.Y.Z"
   ```
6. Tag:
   ```
   git -c safe.directory='*' tag vX.Y.Z
   ```

### Phase C — Push and watch CI

7. Switch `gh` account if needed for `taqat-techno` remote (see PF-6).
8. Push branch then tag (CI runs on tag push; branch must be current):
   ```
   git -c safe.directory='*' push origin HEAD
   git -c safe.directory='*' push origin vX.Y.Z
   ```
9. Watch the release workflow:
   ```
   gh run watch --exit-status $(gh run list --workflow=release.yml --branch=vX.Y.Z --limit 1 --json databaseId -q '.[0].databaseId')
   ```
   Non-zero → abort. Surface `gh run view <id> --log-failed`.

### Phase D — Artifact validation (required platform-coverage gate)

**The artifact requirement for each platform is driven by that platform's status from the Portability Audit Release Gate:**

| Platform status (from audit) | Release Ship requirement |
|---|---|
| `READY` | Artifact **required** on the GitHub Release; absence = abort. |
| `BLOCKED` | Already caught in PF-9; we never reach Phase D. |
| `SOURCE_ONLY` | Artifact **not required**; informational only. |
| `NOT_SUPPORTED` | Platform skipped entirely. |

10. Pull the asset list:
    ```
    gh release view vX.Y.Z --json assets -q '.assets[].name'
    ```

11. **Windows** (if `Windows: READY`):
    - Filename matches `^RAGTools-Setup-X\.Y\.Z\.exe$`.
    - Size > 50 MB.
    - Missing / undersized → **release fails**.

12. **macOS** (if `macOS: READY`):
    - Filename matches `^RAGTools-X\.Y\.Z-macOS-arm64\.(zip|dmg)$` per `macos_release/SKILL.md`.
    - Size > 10 MB.
    - Missing / undersized → **release fails**.

13. **Linux** (if `Linux: READY`):
    - Filename matches `^RAGTools-X\.Y\.Z-linux-(x86_64|aarch64)\.(AppImage|tar\.gz|deb|rpm)$` (the packaging format defined by `release.yml` when Linux goes `READY`).
    - Size > 10 MB.
    - Missing / undersized → **release fails**.
    - If the audit reported `Linux: READY` but `release.yml` has no Linux job, fail in PF-9 (audit protocol violation — the auditor claimed READY without a matching artifact path). Never reach Phase D for Linux in this state.

14. Verify any additional assets declared by `release.yml` outputs.

### Phase E — Post-release

15. Compute SHA256 of the Windows installer:
    ```
    gh release download vX.Y.Z --pattern 'RAGTools-Setup-*.exe' --dir /tmp/release
    certutil -hashfile /tmp/release/RAGTools-Setup-X.Y.Z.exe SHA256   # Windows
    shasum -a 256 /tmp/release/RAGTools-Setup-X.Y.Z.exe                # macOS/Linux
    ```
16. Surface the SHA256 and next-step command for winget. Do not run `wingetcreate` automatically.
17. Switch `gh` account back to `ahmed-lakosha`.
18. Suggest running `/wiki-update` to sync the wiki against the release diff.

### Phase F — Summary (always emit, success or failure)

Produce this structured block:

```
Release vX.Y.Z — <SUCCESS | FAILED at phase <N> step <N>>

Preflight
  - PF-1 semver:               <ok | fail>
  - PF-2 tree clean:           <ok | fail | overridden>
  - PF-3 branch:               <master|main|other>
  - PF-4 tests:                <ok | fail | skipped>
  - PF-5 tag uniqueness:       <ok | fail>
  - PF-6 gh auth:              <ok | fail>
  - PF-7 build tools:          <ok | fail | skipped>
  - PF-8 version coherence:    <ok | fail>
  - PF-9 portability gate:     <PASS | FAIL | skipped>
      Windows: <READY|BLOCKED|SOURCE_ONLY|NOT_SUPPORTED>
      macOS:   <READY|BLOCKED|SOURCE_ONLY|NOT_SUPPORTED>
      Linux:   <READY|BLOCKED|SOURCE_ONLY|NOT_SUPPORTED>
      Audit:   <path to consumed report>
  - PF-10 lifecycle gate:      <ok | fail>

Release decision
  - Ready to release:          <yes | no>
  - Blocked reasons:           <bullet list | none>

Execution
  - Tag:                       <pushed vX.Y.Z | skipped | not created>
  - Workflow run:              <URL | failed: reason | skipped>

Artifacts (from Portability Audit platform matrix)
  - Windows .exe:              <path, size, sha256 | NOT REQUIRED | MISSING>
  - macOS bundle:              <path, size | NOT REQUIRED | MISSING>
  - Linux package:             <path, size | NOT REQUIRED | MISSING>
  - SHA256 for winget:         <hash | N/A>

Warnings (non-blocking)
  - <High-severity audit findings, from audit report>

Next steps
  - <winget PR | /wiki-update | manual recovery: ...>
```

## Hard rules

- **Never force-push to `main` / `master`.**
- **Never skip hooks** — no `--no-verify`, no `--no-gpg-sign`.
- **Never modify git config.** `safe.directory='*'` is passed per-command.
- **Never delete a released tag** without confirmation.
- **Never upload an incomplete asset set.** If any `READY` platform artifact is missing, the release fails.
- **Never auto-run wingetcreate.**
- **Never skip the lifecycle gate** silently.
- **Never skip the portability gate** on a real release. `skip-audit` requires `dry-run`.
- **Never diagnose compatibility issues yourself.** If you see something that looks platform-locked, do not add your own finding — the audit is authoritative. Surface it to the user as "consider re-running /portability-audit".
- **Stop at the first failure.** No best-effort past a broken build; no partial-push.
- **No auto-commit / auto-push without user confirmation** at each of: version-bump diff, commit, tag, push.

## Failure-mode cheat sheet

| Phase | Failure | Recovery |
|---|---|---|
| PF-2 | Dirty tree | Commit or stash, or `allow-dirty`. |
| PF-5 | Tag exists | `git tag -d vX.Y.Z && git push --delete origin vX.Y.Z` only with explicit OK. |
| PF-9 | `Status: FAIL` from audit | Fix the Critical blockers listed; re-run `/portability-audit`; retry. |
| PF-9 | `BLOCKED` on a platform | Fix the blocker or change the platform status to `NOT_SUPPORTED` in the audit (via codebase changes, not by editing the report). |
| PF-9 | Stale `audit-from` file | Re-run `/portability-audit write-report`. |
| PF-9 | Malformed Release Gate block | Re-run `/portability-audit`; the block is fixed-format per the audit SKILL. |
| Phase A | Local build fails | Fix `rag.spec` hidden imports; bump a patch; retry. |
| Phase C | Workflow failed | Fix forward; re-tag. |
| Phase D | Required artifact missing | Block release; inspect CI logs; fix; re-tag or delete-and-redo. |
| Phase D | Size below sanity threshold | Block release; inspect the actual asset. |

## Dry-run semantics

With `dry-run`:
- Run all Preflight checks and report each result.
- PF-9: run the audit fresh (or read pinned report) and show the Release Gate block; use `skip-audit` to omit.
- Show the diff Phase A would apply — do not write files.
- Show the exact tag/commit/push commands — do not execute.
- Inspect the last `release.yml` run and simulate the Phase D result.
- End with "DRY-RUN: would have shipped vX.Y.Z (or BLOCKED by <reason>). Next real command: `/release-ship <version>`."
