# Open Questions

| | |
|---|---|
| **Owner** | TBD (proposed: docs lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Live — updated as items resolve |

Questions the repo alone cannot answer. Each one blocks a specific wiki page or section. Stub pages link here by ID.

## Tracked questions

| ID | Question | Blocks | Proposed owner | Status |
|---|---|---|---|---|
| **Q-1** | What is "Barometer" mode? Not referenced anywhere in code, docs, decisions, or roadmap. | `Operational-SOPs/Runtime/Barometer-vs-Non-Barometer-Behavior.md`, Glossary entry | Product / stakeholder | Open |
| **Q-2** | What are F-001..F-012? Referenced in `rag-log-monitor` agent and the `/rag-doctor` skill but not defined in the repo. | `Reference/Known-Failure-Codes.md`, full runbook coverage | Maintainer of `rag-log-monitor` | Open |
| **Q-3** | What do P-RULE and P-DEDUPE classify? Referenced alongside F-001..F-012. | `Reference/Known-Failure-Codes.md`, doctor-skill documentation | Same as Q-2 | Open |
| **Q-4** | `UserPromptSubmit` hook — implementation status, scope, data written, opt-in flow. | `Development-SOPs/Hooks/Hook-Safety-Rules.md` | Eng lead | Open |
| **Q-5** | Plugin system scope — document intended design (roadmap Phase 7) or defer entirely until implemented? | `Development-SOPs/Plugins/Add-a-New-Plugin.md`, `Architecture/Plugin-Load-Flow.md` | Eng lead / product | Open |
| **Q-6** | Confirmed publish target — GitHub Wiki (`.wiki.git`) or GitHub Pages? | `publish_wiki.ps1`, `.github/workflows/publish-wiki.yml` | Docs lead | Proposed: GitHub Wiki |
| **Q-7** | Pre-v2.4.1 collection/state schema change — what changed, which releases, migration path. | `Change-History/Migration-Guides/Pre-v2-4-1-to-Current.md`, `Runbooks/Pre-v2-4-1-Reset-Blocked.md` | Eng lead | Open |
| **Q-8** | Ownership assignments per domain — named individuals, not roles. | `Owner:` field in every page's metadata table | Project lead | Open |

## Conventions

- New questions append the next `Q-N`. Never renumber.
- When a question resolves, update its **Status**, leave it in the table, and add a short paragraph below describing the resolution and the pages that were updated. Resolved questions stay here as a source-level audit trail.
- Every stub page in the wiki links here by ID. When you resolve a question, grep the wiki-src for the ID and unblock the stubs in the same PR.

## Resolutions

*(none yet)*

## How to resolve Q-8 (ownership backfill)

When named owners are provided, run the backfill as a single PR:

1. **Produce the mapping table.** For each of the six domains in [Documentation Standards § Ownership model](Standards-and-Governance-Documentation-Standards#ownership-model), record the named individual (not the role).

2. **Find every page to update.** Every non-template wiki page carries `| **Owner** | TBD (proposed: <role>) |` in its metadata table.

   ```
   grep -rn "TBD (proposed" docs/wiki-src/
   ```

3. **Bulk-replace per domain.** Example (bash, dev host without the `safe.directory` constraint):

   ```
   # Architecture/, Standards-and-Governance/
   grep -rl "TBD (proposed: eng lead)" docs/wiki-src/Architecture \
     docs/wiki-src/Standards-and-Governance \
     | xargs sed -i 's/TBD (proposed: eng lead)/<Name>/g'

   # Operational-SOPs/
   grep -rl "TBD (proposed: ops lead)" docs/wiki-src/Operational-SOPs \
     | xargs sed -i 's/TBD (proposed: ops lead)/<Name>/g'

   # Repeat per domain: docs lead, on-call / support lead, etc.
   ```

   On Windows, use PowerShell `(Get-ChildItem ... -Recurse | ForEach-Object { (Get-Content $_) -replace ..., ... | Set-Content $_ })`.

4. **Review the diff** as a single PR — it should only change `Owner:` lines.

5. **Update Documentation Standards § Ownership model** with the named individuals.

6. **Mark Q-8 resolved** here: move its row from the `Tracked questions` table into `Resolutions` with a dated note.

7. **Update** `Standards-and-Governance/Review-and-Merge-Standards.md` § Reviewer assignment — same names should appear there.

