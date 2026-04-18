# Standards & Governance: Documentation Standards

| | |
|---|---|
| **Owner** | TBD (proposed: docs lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Active (replaces the Phase 1 stub) |

## Source of truth

**`docs/wiki-src/` in the main repo is the only authoritative source for wiki content.** The published GitHub Wiki (`<repo>.wiki.git`) is a read-only projection. Edits made in the GitHub Wiki UI are overwritten on the next publish. The publish path is documented at [Wiki Publishing Process](Development-SOPs-Documentation-Wiki-Publishing-Process).

## Page metadata

Every non-template wiki page must carry a metadata table at the top:

| Field | Required | Notes |
|---|---|---|
| `Owner` | Yes | Named individual (Phase 10 backfill per [Q-8](Development-SOPs-Documentation-Open-Questions)). Until then, `TBD (proposed: <role>)`. |
| `Last validated against version` | Yes | The `pyproject.toml` version when the page was last walked through end-to-end. |
| `Last reviewed` | Recommended | ISO date of the last content review. |
| `Status` | Yes | `Draft` / `Active` / `Deprecated` / `BLOCKED` / `STUB`. |

Use the plain 3-column markdown table format (no YAML frontmatter — GitHub Wiki does not render it):

```markdown
| | |
|---|---|
| **Owner** | TBD (proposed: ops lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |
```

## Template rule

SOPs, Runbooks, Architecture pages, and Reference pages must start from the matching template in [Templates](Templates-SOP-Template). Structural drift defeats the maintenance loop — reviewers should reject PRs that invent new sections for existing page types.

| Page type | Template |
|---|---|
| Operational SOP | [SOP Template](Templates-SOP-Template) |
| Development SOP | [SOP Template](Templates-SOP-Template) |
| Runbook | [Runbook Template](Templates-Runbook-Template) |
| Architecture | [Architecture Page Template](Templates-Architecture-Page-Template) |
| Reference | [Reference Page Template](Templates-Reference-Page-Template) |

## Ownership model

Domains and their primary owners. Every page in a domain inherits the domain owner unless overridden in the page's metadata table.

| Domain | Primary owner | Review required from |
|---|---|---|
| `Architecture/`, `Standards-and-Governance/` | Eng lead | Domain owner is final authority |
| `Operational-SOPs/` | Ops / support lead | Eng lead for code-linked claims |
| `Development-SOPs/` | Eng lead | Docs lead |
| `Runbooks/` | On-call / support lead | Eng lead |
| `Reference/` | Eng lead (extracts from code) | Docs lead |
| `Templates/`, `Change-History/`, `Start-Here/`, `Core-Concepts/` | Docs lead | Eng lead |

Specific names to be assigned in Phase 10 backfill — tracked as [Q-8](Development-SOPs-Documentation-Open-Questions).

## Claim-to-code rule

Any operational or architectural claim that depends on code behavior **must** cite a `file:line` reference in the page's "Related code paths" or "Code paths" section. If a claim cannot be grounded in code, convert it into an entry on the [Open Questions](Development-SOPs-Documentation-Open-Questions) page instead of guessing.

Reviewers should sample 2-3 claims per PR and open the referenced files to verify.

## Stub page rule

A page that cannot be written yet (blocked on a stakeholder decision or unbuilt functionality) is committed as a stub that:

1. Uses the real filename so it appears in `_Sidebar.md`.
2. Opens with a visible blockquote banner naming the Open Question.
3. Contains no speculative content. If concrete extension surfaces exist, the stub may route readers to them (e.g. [Plugins stub](Development-SOPs-Plugins-Add-a-New-Plugin) points at CLI / HTTP / Skill surfaces).

This keeps the sidebar complete and makes gaps auditable.

## Freshness rule

Every SOP, Runbook, and Architecture page must be re-validated end-to-end against a new version at least once per minor release. `.github/workflows/docs-freshness.yml` runs monthly (and on demand via workflow dispatch) — it flags pages whose `Last validated against version` lags the current `pyproject.toml` version by more than `N` minor versions (default `N = 1`).

### Freshness audit flow

```mermaid
flowchart TD
    Sched([Monthly schedule<br/>or workflow_dispatch]) --> Read[Read pyproject.toml version]
    Read --> Scan[Scan docs/wiki-src/*.md]
    Scan --> Loop{For each page}
    Loop --> Skip1{Template<br/>or _Sidebar/_Footer?}
    Skip1 -- yes --> Loop
    Skip1 -- no --> Meta{Has<br/>Last validated?}
    Meta -- no, Status=BLOCKED/STUB --> Loop
    Meta -- no, other --> Missing[Report as "missing metadata"]
    Meta -- yes --> Cmp{Major mismatch<br/>or lag &gt; threshold?}
    Cmp -- yes --> Stale[Report as STALE]
    Cmp -- no --> Loop
    Missing --> Loop
    Stale --> Loop
    Loop --> Summary[Write job summary<br/>+ annotation warning]
    Summary --> End([Maintainers review])
```

"Re-validated end-to-end" means:

- For an SOP: follow the steps on a test install and confirm each step still works as written.
- For a Runbook: confirm each fix procedure resolves the stated symptom.
- For an Architecture page: re-check every `file:line` reference and confirm the narrative still matches the code.
- For a Reference page: diff the tables against the source of truth.

After re-validation, bump the `Last validated against version` in the page's metadata table. That is the only signal the audit reads.

## When to add a new wiki page

| Change | Wiki action |
|---|---|
| New CLI command | Update `Reference/CLI` (Phase-7 area) + possibly an Operational SOP. |
| New HTTP endpoint | Update `Reference/HTTP-API` in the same PR. |
| New MCP tool | Update `Reference/MCP-Tools` in the same PR. |
| New failure class / runbook | Add a Runbook and route it from `Reference/Known-Failure-Codes`. |
| New architectural decision | Update `docs/decisions.md` (source) + `Standards-and-Governance/Architecture-Decisions`. |
| New user workflow | Add an Operational SOP. If it maps to a skill, also create the skill (see [Add a New Slash Command](Development-SOPs-Skills-Add-a-New-Slash-Command)). |

## Style

- Keep sentences tight. A reader scanning for information should find it in the first sentence of each paragraph.
- Use tables for structured data (commands, keys, codes). Use prose for rationale.
- Cross-link liberally. Every page should link to at least one Architecture or Reference page.
- Do not embed screenshots. Text + Mermaid is the house style.
- Do not add emojis.

## PR process for wiki-src edits

1. Branch off `main`.
2. Edit `docs/wiki-src/**` with the conventions on this page.
3. Run `scripts/publish_wiki.ps1 -DryRun` locally to confirm the slug transforms are clean.
4. Open a PR. CI runs `publish-wiki.yml` in dry-run mode (Phase 1-9 state) or in publish mode (Phase 10 state).
5. Reviewer per the ownership model above.
6. Merge → publish (once Phase 10 activates the publish workflow).

## Related

- [Wiki Publishing Process](Development-SOPs-Documentation-Wiki-Publishing-Process).
- [Templates](Templates-SOP-Template).
- [Open Questions](Development-SOPs-Documentation-Open-Questions).
- [Review and Merge Standards](Standards-and-Governance-Review-and-Merge-Standards).
