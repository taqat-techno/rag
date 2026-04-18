# Runbook: <Symptom>

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Related failure codes** | TBD — see [Known Failure Codes](Reference-Known-Failure-Codes) |
| **Status** | Draft / Active / Deprecated |

## Symptom
Observable behavior the user or operator is reporting. Quote exact error messages where possible.

## Quick check
Single commands to run first to confirm this runbook applies.

```
...
```

## Diagnostic commands
Deeper commands to narrow the root cause.

| Command | What it tells you |
|---|---|
| `rag doctor` | ... |
| ... | ... |

## Root causes
Known causes that produce this symptom. Order by frequency.

1. **<cause>** — how to confirm.
2. ...

## Fix procedure
Numbered imperative steps. Group by root cause if procedures differ.

### If cause = <cause-1>
1. ...

### If cause = <cause-2>
1. ...

## Verification
How to confirm the fix stuck. Should leave the system in a known-good state verifiable by `rag doctor` or an equivalent command.

## Escalation
When to stop and hand off. Include who to hand off to and what state to capture first (logs, config, version).

## Related
- SOP: [...](...)
- Architecture: [...](...)
- Failure code: ...
