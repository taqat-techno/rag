# Wiki Update

> Reconcile `docs/wiki-src/` with everything that has actually changed in the repo and with any direction the user just gave in chat. Invoke after a release, or any time the wiki has drifted. Updates the wiki in-place — does not commit or push.

## Inputs

When invoked, treat these as the signal (in priority order):

1. **User-provided direction in the invocation.** If the user says something like "update the API reference for the new endpoints" or "reflect the port-number change in config", that is the authoritative scope — follow it exactly.
2. **Recent conversation context.** If the user has been describing enhancements/fixes in the current session, those statements are first-class inputs. Quote them back when proposing changes.
3. **Git history since baseline.** Used to corroborate (or surface additional items the user did not mention).

If there is no user direction and no relevant conversation context, fall back to a pure release-style sync driven entirely by git.

## Baseline resolution

Determine the baseline in this order:

1. Explicit baseline the user typed (`"since v2.4.1"`, `"from commit abc123"`).
2. Most recent git tag: `git -c safe.directory='*' tag --sort=-version:refname | head -1`.
3. Last `## [X.Y.Z]` entry in `docs/wiki-src/Change-History/Changelog.md`.
4. If none of the above: ask the user.

State the baseline and the reason before doing anything else.

## Analysis

Between baseline and HEAD:

- `git -c safe.directory='*' log <baseline>..HEAD --pretty=format:'%h %s'` — commit subjects.
- `git -c safe.directory='*' log <baseline>..HEAD --name-only --pretty=format:` — changed files.
- **Do not trust commit messages alone.** Open the diffs for anything that looks load-bearing (config defaults, route signatures, CLI flags, schema changes).

## Route findings to wiki pages

The wiki uses the two-layer model: `docs/wiki-src/` is source-of-truth, the GitHub Wiki is the published projection. Only edit `docs/wiki-src/**`.

| Code area | Owning wiki page(s) |
|---|---|
| `src/ragtools/config.py`, `ragtools.toml` | `Reference/Configuration-Keys.md`, `Reference/Environment-Variables.md`, `Architecture/Configuration-Resolution-Flow.md`, `Operational-SOPs/Configuration/Configuration-Precedence.md` |
| `src/ragtools/service/routes.py` | `Reference/HTTP-API.md`, `Development-SOPs/HTTP-API/Add-a-New-Endpoint.md` |
| `src/ragtools/service/{run,process,owner,startup,app,watcher_thread,supervisor,activity,crash_history,notify}.py` | `Architecture/Service-Lifecycle.md`, `Operational-SOPs/Service/Start-and-Stop-Service.md`, `Runbooks/Service-Fails-to-Start.md`, `Development-SOPs/Hooks/Hook-Safety-Rules.md` |
| `src/ragtools/service/{pages.py,templates/,static/}` | (no page yet — propose `Reference/Admin-Panel.md` if the change is user-visible) |
| `src/ragtools/cli.py` | `Architecture/CLI-Dual-Mode.md`, `Development-SOPs/Commands/Add-a-New-Command.md` (and a future `Reference/CLI.md`) |
| `src/ragtools/integration/` | `Architecture/MCP-Proxy-Decision.md`, `Reference/MCP-Tools.md`, `Runbooks/MCP-Server-Fails-to-Load.md` |
| `src/ragtools/indexing/`, `chunking/`, `embedding/` | `Architecture/Indexing-Pipeline.md` |
| `src/ragtools/retrieval/` | `Core-Concepts/Confidence-Model.md`, `Reference/MCP-Tools.md` |
| `src/ragtools/watcher/`, `service/watcher_thread.py` | `Architecture/Watcher-Flow.md`, `Runbooks/Watcher-Permission-Denied.md` |
| `src/ragtools/ignore.py` | `Reference/Configuration-Keys.md`, `Architecture/Watcher-Flow.md` |
| `docs/decisions.md` | `Standards-and-Governance/Architecture-Decisions.md` — **update the summary table only**; the source-of-truth for ADRs is `docs/decisions.md`. If a change warrants a new ADR that does not yet exist, stop and surface it. |
| `RELEASING.md`, `installer.iss`, `pyproject.toml`, `.github/workflows/release.yml`, `scripts/build.py` | `Development-SOPs/Release/Release-Checklist.md`, `Change-History/Changelog.md` |
| `tests/` | `Development-SOPs/Testing/Install-Upgrade-Repair-Test-Matrix.md` (module inventory), `Standards-and-Governance/Testing-Standards.md` |
| `docs/wiki-src/**`, `scripts/publish_wiki.ps1`, `.github/workflows/publish-wiki.yml`, `docs-freshness.yml` | meta: the wiki updating itself — no user-facing wiki edit unless publish semantics changed |
| `.claude/` | meta: not user-facing wiki content |

## Propose before editing

Present a plan:

- Baseline + reason.
- User direction applied (if any).
- Per-page: existing page → proposed change (edit, new section, table update, version bump, changelog entry).
- New pages to create (only if unavoidable — prefer extending existing pages).
- Any Open Questions (`docs/wiki-src/Development-SOPs/Documentation/Open-Questions.md`) that the change unblocks — propose resolving them.
- Anything ambiguous where you need the user's call.

**Wait for confirmation** before editing unless the user said "just do it" or "no review needed" in the invocation.

## Apply edits

For each page being updated:

1. Change the factual content (tables, signatures, flags, paths, versions).
2. Bump `| **Last validated against version** | X.Y.Z |` in the page's metadata table to the current `pyproject.toml` version.
3. If a claim depends on code, keep the `file:line` citation current (per `Standards-and-Governance/Documentation-Standards.md`).
4. Add a bullet to `docs/wiki-src/Change-History/Changelog.md` under `[Unreleased]` or the matching `[X.Y.Z]` section.
5. New pages: start from `docs/wiki-src/Templates/<type>-Template.md`, follow `Documentation-Standards.md`.

## Verify

- `scripts/publish_wiki.ps1 -DryRun` — slug transforms clean, file count sensible.
- Spot-check 2-3 edited pages: claims match code, links resolve to real slugs, metadata table intact.
- If any stub page got resolved, update its `Status:` field and remove the BLOCKED banner.

## Summarize

End with a tight report:

- Pages edited (count + list).
- Pages created (if any).
- Stubs unblocked / Open Questions resolved.
- `Last validated against version` bumped to `X.Y.Z`.
- Anything deferred and why.

## Hard rules

- **Never commit or push** without the user's explicit "yes commit/push" (per `C:\Users\ahmed\.claude\CLAUDE.md` GitHub Control).
- **Do not** edit `src/`, `tests/`, `docs/decisions.md`, `RELEASING.md`, or any non-wiki source. Wiki sync is strictly `docs/wiki-src/**` (plus `Change-History/Changelog.md`).
- **Do not** invent features or behavior. Anything not grounded in a diff or user statement is out of scope.
- **Do not** delete or rewrite wiki pages the user didn't ask about — apply the smallest change that captures the update.
- If the change needs a new ADR, stop and surface it; wiki sync does not author ADRs.

## Handy commands during the run

```
git -c safe.directory='*' tag --sort=-version:refname | head -1
git -c safe.directory='*' log <baseline>..HEAD --oneline
git -c safe.directory='*' log <baseline>..HEAD --name-only --pretty=format:
git -c safe.directory='*' show <sha> -- <file>    # see one file's diff at a commit
scripts/publish_wiki.ps1 -DryRun                  # validate slugs
```

## Optional arguments the user might pass

- `since v2.4.1` / `since <sha>` — explicit baseline.
- `pages X, Y, Z only` — scope to specific wiki pages.
- `skip changelog` — do not write to `Change-History/Changelog.md`.
- `just do it` — skip the propose-and-wait step.
- free-form narrative — treat as authoritative scope (see "Inputs" above).
