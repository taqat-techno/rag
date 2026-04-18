# SOP: Add a New Plugin

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | N/A |
| **Last reviewed** | 2026-04-18 |
| **Status** | STUB — plugin system not implemented as of v2.4.2 |

> **Plugin system is not implemented.** Reg has no plugin architecture in v2.4.2. Whether to design and build one — and in what shape — is tracked as **[Q-5 on the Open Questions page](Development-SOPs-Documentation-Open-Questions)**.
>
> This page exists so the sidebar is complete and so contributors looking for "how to extend Reg" land somewhere useful instead of a 404. The short version: the extension surfaces you can use **today** are below.

## What exists today

Reg has three documented extension surfaces. None are "plugins" in the sense of dynamic discovery or third-party packages — they are in-repo extension points:

| Surface | Use when | SOP |
|---|---|---|
| **CLI command** (Typer subcommand in `src/ragtools/cli.py`) | User needs a new `rag <subcommand>`. | [Add a New Command](Development-SOPs-Commands-Add-a-New-Command) |
| **HTTP endpoint** (FastAPI route in `src/ragtools/service/routes.py`) | User-facing behavior should be callable over HTTP, from the admin panel, or from the CLI via dual-mode. | [Add a New HTTP Endpoint](Development-SOPs-HTTP-API-Add-a-New-Endpoint) |
| **Claude Code skill** (`.claude/skills/<name>/SKILL.md`, plus the rag-plugin skill bundle) | User needs an orchestrated `/<name>` flow chaining existing commands/tools. | [Add a New Slash Command](Development-SOPs-Skills-Add-a-New-Slash-Command) |

Additionally, the **MCP tool surface** (`search_knowledge_base`, `list_projects`, `index_status`) is documented at [Reference/MCP-Tools](Reference-MCP-Tools). Adding a new MCP tool today is an in-repo change to `src/ragtools/integration/mcp_server.py` — not a pluggable extension.

## What "plugin" would mean

A plugin system would imply at minimum:
1. A discovery mechanism (entry points, manifest, or sidecar directory).
2. A stable contract for what plugins can register (new tools, new commands, new routes, new hooks, new indexers).
3. A lifecycle contract (load, unload, config, errors).
4. A trust model (third-party code running in-process).

Each of these is a design decision with compounding implications — especially (4) given Reg's [Single-Process Invariant](Core-Concepts-Single-Process-Invariant). The decision to invest here has not been made.

## If you want to propose a plugin system

1. Read the four bullets above and sketch a concrete shape for each.
2. File input on Q-5 via [Open Questions](Development-SOPs-Documentation-Open-Questions).
3. Do not ship a plugin loader without stakeholder sign-off — a half-built plugin system is worse than none.

## When this stub becomes a real SOP

When Q-5 resolves in favor of building a plugin system, this page will be rewritten to follow the SOP template: Purpose, Scope, Trigger, Preconditions, Inputs, Steps, Validation, Failure modes, Recovery, Code paths, Related commands, Change log.

## Related

- [Q-5 on Open Questions](Development-SOPs-Documentation-Open-Questions)
- [Add a New Command](Development-SOPs-Commands-Add-a-New-Command)
- [Add a New HTTP Endpoint](Development-SOPs-HTTP-API-Add-a-New-Endpoint)
- [Add a New Slash Command](Development-SOPs-Skills-Add-a-New-Slash-Command)
- [Reference: MCP Tools](Reference-MCP-Tools)
