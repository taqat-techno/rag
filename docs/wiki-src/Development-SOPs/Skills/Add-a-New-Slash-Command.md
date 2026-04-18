# SOP: Add a New Slash Command (Claude Code Skill)

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Purpose
Author a Claude Code skill invoked via `/<name>` — for workflows that chain multiple `rag` commands, interactive confirmations, or project-specific automation.

## Scope
Covers skills living in `.claude/skills/<name>/SKILL.md` (in-repo). The `rag:*` skills (`rag:rag-setup`, `rag:rag-doctor`, `rag:rag-projects`, `rag:rag-reset`, `rag:rag-config`) are shipped by the rag-plugin bundle and are not edited here — changes to those go in the plugin repo.

## Trigger
- An operator chains several `rag` commands repeatedly — candidate for a skill.
- Interactive flow requires a confirmation gate (e.g. `DELETE` prompt before reset).
- Claude CLI users need an orchestrated flow that maps to an existing Operational SOP.

## Preconditions
- [ ] A local `.claude/skills/` directory (Claude Code loads skills from there).
- [ ] The underlying operation is already available as CLI commands, HTTP API, or MCP tool — skills orchestrate; they don't implement.

## Inputs
- Skill name (lowercase, hyphens allowed).
- Trigger conditions (when the skill should be offered).
- The SOP the skill implements — every user-facing skill should map to a documented Operational SOP (see [Operational SOPs](Home#operational-sops)).

## Steps

1. **Create the skill directory:**
   ```
   mkdir -p .claude/skills/<name>
   ```

2. **Write `SKILL.md`.** First heading is the title; the opening blockquote is the description Claude shows to users. Use the structure from the local example `.claude/skills/macos_release/SKILL.md`:

   ```markdown
   # <Skill Title>

   > <One-sentence description of what this skill does.>

   ## Overview

   <2-3 sentences of context.>

   ## Steps

   1. ...
   2. ...
   ```

3. **Link to the backing SOP.** The skill body should include a cross-reference like:

   > Runs the flow documented at [Operational-SOPs/.../Page-Name](Operational-SOPs-...-Page-Name).

4. **Destructive operations: require `DELETE` confirmation.** The `rag:rag-reset` skill is the canonical example — follow its pattern. The skill *prompts* for `DELETE`; it does not bypass.

5. **Diagnostic operations: invoke `rag doctor` first.** Do not issue writes based on inferred state. `rag:rag-doctor` is the reference pattern.

6. **Test interactively.** Reload Claude Code and invoke `/<name>`. Verify:
   - The skill appears in the available-skills list.
   - The flow runs end to end.
   - Destructive steps actually halt without explicit confirmation.

## Mapping to Operational SOPs

Every local skill must map to (and link to) exactly one Operational SOP:

| Skill location | Backing SOP |
|---|---|
| `.claude/skills/<name>/SKILL.md` | `Operational-SOPs/.../<Name>.md` |

If the SOP does not exist yet, author the SOP first, then the skill. Skills without an SOP drift; SOPs without skills are fine.

## Validation / expected result

- Skill loads in Claude Code (`/<name>` autocompletes).
- Flow matches the linked SOP step-for-step.
- Destructive steps require explicit confirmation (`DELETE` verbatim where applicable).
- Idempotent: re-running the skill does not break state (or explicitly refuses).

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Skill not listed | Wrong path or missing `SKILL.md` | Must be at `.claude/skills/<name>/SKILL.md`. |
| Skill loads but does nothing | Unclear instructions | Skill content is a prompt for the assistant — make intent explicit. |
| Destructive operation ran without confirmation | Missing prompt logic in skill body | Add the `DELETE` gate. See `rag:rag-reset` bundled skill for the canonical pattern. |
| Skill orchestration drifts from the SOP it claims to run | SOP was updated; skill wasn't | Any change to an Operational SOP that has a backing skill requires a parallel update to the skill. |

## Recovery / rollback
- Delete `.claude/skills/<name>/` to remove.
- Re-author from scratch if the skill misbehaves — skill files are small.

## Related code paths
- `.claude/skills/macos_release/SKILL.md` — local example of SKILL.md format and length.
- `.claude/skills/windows_app_actions/SKILL.md`, `.claude/skills/macos_app_actions/SKILL.md` — additional format examples.

## Related commands
- `/<skill-name>` — invoke the skill.
- `/help` — list available skills.

## Notes on `rag:*` plugin skills

The `rag:rag-setup`, `rag:rag-doctor`, `rag:rag-projects`, `rag:rag-reset`, and `rag:rag-config` skills are shipped in the rag-plugin bundle, not in this repo. They are mirrored in Operational SOPs (see the [Skill → SOP table](Home)) but their source of truth is the plugin. To propose changes to those skills, open an issue against the plugin repository — not here.

## Change log
- 2026-04-18 — Initial draft.
