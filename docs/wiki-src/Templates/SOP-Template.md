# SOP: <Title>

<!-- Replace this header with the actual SOP title when you copy this template. -->

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | YYYY-MM-DD |
| **Status** | Draft / Active / Deprecated |

## Purpose
One sentence stating what this SOP accomplishes and for whom.

## Scope
What this SOP covers and — explicitly — what it does NOT cover. Link out for the excluded cases.

## Trigger
What prompts a reader to use this SOP. Be specific: user reports X, alert Y fires, scheduled action Z, etc.

## Preconditions
Independently verifiable checklist of required state before starting.

- [ ] ...
- [ ] ...

## Inputs
Data, credentials, or artifacts the operator needs on hand. Include paths and sources.

## Steps
Numbered, imperative. Each step is atomic and verifiable.

1. ...
2. ...

## Validation / expected result
How the operator knows the SOP succeeded. Must be observable — command output, file state, HTTP response, UI state.

## Failure modes
Known ways this SOP can fail and how to recognize them. Link to the matching Runbook.

| Symptom | Likely cause | Runbook |
|---|---|---|
| ... | ... | [link]() |

## Recovery / rollback
How to revert partial state if the SOP is abandoned mid-way. If no rollback is possible, state this explicitly.

## Related code paths
Files and line numbers that implement this behavior. A reader can open these to verify claims.

- `src/ragtools/...` — ...

## Related commands
CLI, HTTP, or skill names invoked by this SOP.

- `rag <command>`
- `/rag:<skill>`

## Change log
- YYYY-MM-DD — Initial version.
