# Reg / ragtools Wiki

Local-first, Markdown-only RAG system. Claude CLI searches a local Qdrant knowledge base first, then completes answers using its own reasoning.

**No Docker. No cloud. No containers.** Python 3.12 · Qdrant (local mode) · Sentence-Transformers · Claude CLI (MCP).

## Read these first

1. [Prerequisites](Start-Here-Prerequisites)
2. [Quick Install (Dev)](Start-Here-Quick-Install-Dev) or [Quick Install (Packaged)](Start-Here-Quick-Install-Packaged)
3. [Glossary](Core-Concepts-Glossary) — one-page domain vocabulary

## I need to...

| Goal | Start at |
|---|---|
| Install from source | [Fresh Install (Dev)](Operational-SOPs-Installation-Fresh-Install-Dev) |
| Install on Windows | [Fresh Install (Packaged)](Operational-SOPs-Installation-Fresh-Install-Packaged) |
| Fix a broken install | [Repair Broken Installation](Operational-SOPs-Repair-Repair-Broken-Installation) |
| Start or stop the service | [Start and Stop Service](Operational-SOPs-Service-Start-and-Stop-Service) |
| Understand why my config isn't taking effect | [Configuration Precedence](Operational-SOPs-Configuration-Configuration-Precedence) |
| Add a new CLI command | [Add a New Command](Development-SOPs-Commands-Add-a-New-Command) |
| Cut a release | [Release Checklist](Development-SOPs-Release-Release-Checklist) |

## Orientation

- [System Overview](Architecture-System-Overview) — what runs where.
- [Single-Process Invariant](Core-Concepts-Single-Process-Invariant) — the most important rule.
- [Architecture Decisions](Standards-and-Governance-Architecture-Decisions) — the locked design.
- [Changelog](Change-History-Changelog) — what shipped when.

## Status

- **Current version:** 2.4.2
- **Platforms:** Windows 11, macOS (arm64)
- **Source of truth:** `docs/wiki-src/` in the main repo. Edits made directly in the GitHub Wiki UI are overwritten on next publish.
