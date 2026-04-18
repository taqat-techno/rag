# Portability Audit

> Deep static audit for cross-platform safety across **Windows, macOS, and Linux**. Scan the repo for platform-locked assumptions, hardcoded personal or machine-specific paths, and non-generic implementation choices. Produce severity-ranked findings, a per-platform readiness verdict, and a machine-readable **Release Gate** block that `/release-ship` consumes. Optionally apply mechanical safe fixes only when the user says so.

This command is the **platform-compatibility gate**. It is authoritative for whether the project is ready to ship per platform; `/release-ship` does not diagnose compatibility itself and only reads the Release Gate this command emits.

## Inputs the user might pass

- Free-form direction ("focus on Linux support", "I just added a LaunchAgent — validate it"). Take as authoritative scope.
- `safe-fix` → apply mechanical low-risk fixes at the end.
- `write-report` → save a full report to `tasks/portability-audit-reports/<ISO-timestamp>.md` for later consumption (by `/release-ship` or a human).
- `only <path>` → restrict scope to that subtree.
- `critical only` / `high+` → severity threshold for the visible report (Release Gate always computed from full scan).
- `skip docs` / `skip tests` / `skip packaging` → narrow scope.
- `platform windows|macos|linux` → focus the verdict on one platform.

If no direction is given, run the full audit against the default scope below.

## Scope

**Always analyze:**
- `src/ragtools/**` — the Python package
- `scripts/**` — build, launch, publish helpers
- `installer.iss` — Inno Setup script
- `.github/workflows/**` — CI/CD
- `pyproject.toml`, `ragtools.toml`, `rag.spec`

**Include by default (user can skip):**
- `docs/wiki-src/**`, `docs/decisions.md`, `docs/RELEASE_LIFECYCLE.md`
- `README.md`, `RELEASING.md`, `CLAUDE.md`
- `tests/**`

**Always exclude:**
- `.venv/`, `.git/`, `node_modules/`, `dist/`, `build/`, `data/`, `.stversions/`, `.stfolder`

## Platform lanes

Each finding is classified against the three platform lanes. Most findings affect exactly one or two lanes; a few (personal paths, release lifecycle) affect all three.

### Windows lane
Canonical patterns that must be branched on `sys.platform == "win32"`:
- `CREATE_NO_WINDOW`, `DETACHED_PROCESS` subprocess flags
- `ctypes.windll`, `winreg`, `schtasks`, `winotify`
- `%LOCALAPPDATA%`, `%APPDATA%`, `%USERPROFILE%`, `%TEMP%`
- `rag.exe`, drive letters (`C:\`, `D:\`)

### macOS lane
Canonical patterns that must be branched on `sys.platform == "darwin"`:
- `~/Library/Application Support/...`, `~/Library/Logs/...`, `~/Library/LaunchAgents/`
- `/Applications/`, `.app` bundles
- `launchctl`, LaunchAgent plist files
- `.icns` icon format, `create-dmg` / `hdiutil` in scripts

### Linux lane
Canonical patterns that must be branched on `sys.platform.startswith("linux")` (or left as the default Unix fallback):
- `$XDG_DATA_HOME` / `~/.local/share/`
- `$XDG_CONFIG_HOME` / `~/.config/`
- `$XDG_CACHE_HOME` / `~/.cache/`
- `$XDG_RUNTIME_DIR`
- systemd user units (`~/.config/systemd/user/*.service`), `systemctl --user`
- `inotify`, `.desktop` entries, `/usr/share/applications/`
- AppImage, `.deb`, `.rpm` packaging

## Severity rubric

### Critical — release blocker on at least one platform
- **Personal-identifier leakage** — `ahmed`, `lakosha`, `LAKOSHA-HOME`, `MY-WorkSpace`, personal email addresses — anywhere in source / scripts / tests / installer / packaging / shipped docs. (CLAUDE.md itself is developer context and may legitimately include the user's email; flag only if it appears in shipped artifacts.) Affects: **W+M+L**.
- **Hardcoded absolute user paths** — `C:\Users\<name>\`, `/Users/<name>/`, `/home/<name>/` — outside clearly labeled doc examples. Affects: **W+M+L**.
- **Data written into the install directory** — violates `docs/RELEASE_LIFECYCLE.md` §2.1-2.2. Affects: **W+M+L**.
- **Platform-locked destructive code with no fallback** — `ctypes.windll`, `winreg` writes, `schtasks` / `launchctl` without a guard and a matching fallback. Affects: the other platforms.
- **Registry writes** or `winreg` usage without a non-Windows branch. Affects: **M+L**.
- **`sys.platform` branch missing a Linux fallback** when the code path is reachable on Linux (e.g. `_get_app_dir` returns `None` for Linux). Affects: **L**.

### High — works today, fragile on the other platform(s)
- **Hardcoded drive letters** outside platform-branched code. Affects: **M+L**.
- **`.exe` suffix hardcoded** in subprocess calls or path strings (except inside `sys.platform == "win32"` branches). Affects: **M+L**.
- **Platform-specific shells invoked directly** — `powershell`, `cmd.exe`, `bash`, `zsh` — without platform detection. Affects: any platform not covered.
- **Platform-specific env var direct reads** — `os.environ["LOCALAPPDATA"]` on non-Windows, `os.environ["HOME"]` without fallback on Windows. Affects: the absent platform.
- **macOS paths** (`~/Library/...`) outside `_get_app_dir` or matching branches. Affects: **W+L**.
- **Linux paths** (`/etc/`, `/usr/share/`, `$XDG_*` hardcoded) outside Linux branches. Affects: **W+M**.
- **String-concatenated paths** instead of `pathlib.Path` / `os.path.join`. Affects: **W+M+L** (cross-OS separator hazard).
- **Tools invoked without PATH validation** — assumes a tool exists. Affects: any platform where the tool is missing.
- **Case-sensitivity assumptions** — comparing filenames literally, loading by fixed-case name. Windows/macOS are case-insensitive by default; Linux is case-sensitive. Affects: **L**.

### Medium — should be abstracted
- **`os.path` style joins** in new code where `pathlib` would be clearer.
- **Hardcoded line endings** (`\r\n`) or file reads missing `encoding="utf-8"`.
- **Temp files in `/tmp` or `%TEMP%`** without using the `tempfile` module. Affects: **W+L**.
- **Platform branches duplicated** across modules when they should delegate to a canonical resolver (`config.py:_get_app_dir`).
- **Shebangs without `/usr/bin/env`** in scripts intended to be cross-platform.

### Low — cosmetic / future hardening
- Docs/examples showing only one platform's paths without equivalents.
- Comments referencing only one platform when both/all are supported.
- Minor style inconsistencies.

## Reference patterns (known-good)

Cite these as the "correct" examples when proposing fixes:

- `src/ragtools/config.py:_get_app_dir` — platform-branch for app data dirs. **As of v2.4.2, Linux falls through to `None`** — that is itself a Linux lane finding (High: no installed-mode path for Linux; source-install uses CWD `./data/`).
- `src/ragtools/config.py:get_data_dir` — dev vs installed with `RAG_DATA_DIR` override.
- `src/ragtools/config.py:_default_service_port` — installed (21420) vs dev (21421). Not platform-branched; portable.
- `docs/decisions.md` Decision 10 — the data-directory contract.
- `docs/RELEASE_LIFECYCLE.md` §2 — the replaceable-app vs persistent-user-data layer.
- `.claude/skills/macos_release/SKILL.md` — per-platform differences reference.

## How to run the audit

1. **Enumerate** files in scope with `Glob` / `Grep`.
2. **Search for patterns** using the starter greps below. Matches are candidates, not findings.
3. **Open context** — for each candidate, read the enclosing function. Decide:
   - "platform-locked" (no branch, breaks elsewhere) → **flag**.
   - "platform-specific and properly branched" (guarded by `sys.platform`) → **do not flag**; list briefly under "Correctly isolated".
4. **Cross-check** against reference patterns. Duplicated platform-branch logic should redirect to `_get_app_dir`.
5. **Classify severity** using the rubric.
6. **Per-platform readiness** — for each lane (W/M/L):
   - Count blocking (Critical) findings.
   - Verify the corresponding branch exists where other branches exist (symmetry check).
   - Verify the packaging path (installer / bundle / AppImage / source-only).
   - Assign status: **`READY`** / **`BLOCKED`** / **`SOURCE_ONLY`** / **`NOT_SUPPORTED`** (see next section).

## Platform status semantics

| Status | Meaning | Release Ship behavior |
|---|---|---|
| `READY` | Source + installed-mode paths work, release artifact exists (`release.yml` produces one). | Require the artifact; release requires this platform. |
| `SOURCE_ONLY` | Source install works, no packaged artifact. No platform-locking findings. | Release does **not** require an artifact on this platform. Non-blocking. |
| `BLOCKED` | Critical finding prevents source OR installed use on this platform. | **Release blocked.** |
| `NOT_SUPPORTED` | Project explicitly declares no support (e.g. a platform unsupported by Python or by a hard dependency). | Release doesn't check this platform. |

**Default assignments as of v2.4.2** (auditor may revise):
- Windows: `READY` (installer + bundle).
- macOS (arm64): `READY` if `release.yml` has a `macos-*` job and produces an artifact; otherwise `BLOCKED` (project claims macOS support in Decision 10 + `macos_release/SKILL.md`, so missing artifact is a blocker).
- Linux: `SOURCE_ONLY` (source install works; `_get_app_dir` returning `None` is a High finding but not a Critical since CWD fallback handles it).

## Starter searches

```
# personal / machine identifiers
grep -rniE "ahmed|lakosha|MY-WorkSpace|LAKOSHA-HOME" src/ scripts/ tests/ docs/ installer.iss pyproject.toml

# Windows-only primitives
grep -rnE "CREATE_NO_WINDOW|DETACHED_PROCESS|winreg|windll|winotify|schtasks" src/ scripts/

# macOS-only primitives
grep -rnE "launchctl|LaunchAgents|/Applications/|\.icns|hdiutil|create-dmg" src/ scripts/

# Linux-only primitives
grep -rnE "systemctl|XDG_|inotify|\.desktop|/usr/share/|/etc/" src/ scripts/

# hardcoded Windows paths
grep -rnE 'C:\\\\|%LOCALAPPDATA%|%APPDATA%|%USERPROFILE%|Program Files' src/ scripts/

# macOS paths outside branches
grep -rnE "/Users/|~/Library/" src/ scripts/

# Linux paths outside branches
grep -rnE "/home/|~/\.config/|~/\.local/|~/\.cache/" src/ scripts/

# .exe hardcodes
grep -rn '\.exe' src/ scripts/

# subprocess usage (context-read required)
grep -rn "subprocess\." src/ scripts/
```

Use as starting points — open hits in context.

## Output format

### 1. Audit Summary
- Readiness: `Release-Ready` / `Needs Work` / `Blocker`.
- Findings by severity: `Critical: N · High: N · Medium: N · Low: N`.
- Top 3 risks (one line each).

### 2. Per-Platform Readiness

For each platform, emit exactly this block:

```
Windows: <READY|BLOCKED|SOURCE_ONLY|NOT_SUPPORTED>
  - Critical findings affecting Windows: <N>
  - High findings affecting Windows: <N>
  - Artifact path: <release.yml job name, or "none">
  - Notes: <1-2 lines>

macOS: <READY|BLOCKED|SOURCE_ONLY|NOT_SUPPORTED>
  - Critical findings affecting macOS: <N>
  - High findings affecting macOS: <N>
  - Artifact path: <release.yml job name, or "none">
  - Notes: <1-2 lines>

Linux: <READY|BLOCKED|SOURCE_ONLY|NOT_SUPPORTED>
  - Critical findings affecting Linux: <N>
  - High findings affecting Linux: <N>
  - Artifact path: <release.yml job name, or "none">
  - Notes: <1-2 lines>
```

### 3. Detailed Findings
Per finding:

```
- **[SEVERITY]** category · affects [W|M|L|W+M|W+M+L|...] · `file:line`
  Evidence: <3-line excerpt>
  Impact: <what breaks on the affected platform(s)>
  Fix: <concrete patch or refactor target>
  Auto-fix safe: <yes|no|partial>
```

Group by severity (Critical first), then by platform-lane within each severity.

### 4. Cross-Platform Readiness
- **Already generic** — short list.
- **Correctly isolated** platform-specific code — short list with line refs.
- **Blocks Windows** / **Blocks macOS** / **Blocks Linux** — ranked bullet lists of concrete gaps.
- **Needs abstraction** — call-sites that should delegate to `_get_app_dir` / `get_data_dir` / equivalent.

### 5. Remediation Plan
- **P1 release blockers** (Critical).
- **P2 fragile-but-working** (High).
- **Quick wins** — mechanical, <30 min each, safe to auto-apply.
- **Deeper refactors** — architectural; flag for human review.

### 6. Safe Fixes (only if user passed `safe-fix`)
Apply only:
- Personal-identifier replacements in docs/comments (not in shipped installer strings).
- `"rag.exe"` → platform-branched suffix.
- `pathlib.Path` migrations for string-concatenated paths in new code.
- `encoding="utf-8"` added to file reads.

Never apply: installer script changes, new platform branches, cross-file refactors, anything touching user data paths or the data-dir resolver.

Summarize with `file:line` before/after. End with "X applied; Y deferred".

### 7. Release Gate (required — this is the machine-readable block)

Always end the report with exactly this block, verbatim labels, one line each:

```
## Release Gate

- Status: <PASS|FAIL>
- Windows: <READY|BLOCKED|SOURCE_ONLY|NOT_SUPPORTED>
- macOS: <READY|BLOCKED|SOURCE_ONLY|NOT_SUPPORTED>
- Linux: <READY|BLOCKED|SOURCE_ONLY|NOT_SUPPORTED>
- Critical blockers: <N>
- High findings: <N>
- Blocking reasons:
  - <one line per blocker, or "none">
- Audit timestamp: <ISO-8601 UTC>
- Audit scope: <default|only <path>|platform <name>>
```

**Status rules** (compute deterministically):

- `Status: FAIL` iff any of:
  - `Critical blockers > 0`, or
  - any platform lane = `BLOCKED`.
- `Status: PASS` otherwise.

A `SOURCE_ONLY` platform **does not** cause `FAIL` — it means that platform is fine for source users and is not release-gated.

## Writing a report to disk

If the user passed `write-report` (or if this audit was invoked by `/release-ship`), also write the full output to:

```
tasks/portability-audit-reports/<YYYY-MM-DDTHH-MM-SSZ>.md
```

Create the directory if missing. Do not remove older reports — they are the freshness history.

## Hard rules

- **Never commit or push** without explicit user permission.
- **Never auto-fix risky items.** Safe-fix only when the user says so.
- **Every finding must cite `file:line`.** No speculative findings.
- **Do not duplicate `_get_app_dir`** in your fix recommendations — route there.
- **Respect the release-lifecycle contract.** Any finding that touches the replaceable vs persistent-data boundary is at minimum High.
- **Distinguish "locked" from "isolated".** A `sys.platform == "win32"` branch with matching `elif "darwin"` / `else` is correct, not a finding.
- **Emit the Release Gate block** exactly as specified. `/release-ship` parses it by exact labels; any drift breaks the integration.
- **Compute Status deterministically** from the rules above. Do not manually override.

## Final summary

End the free-form section (before the Release Gate block) with one paragraph:
- Overall readiness.
- Number of release blockers.
- Which platform lanes are gating the release, if any.
- What the next manual action is.
