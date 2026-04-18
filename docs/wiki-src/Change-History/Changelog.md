# Change History: Changelog

| | |
|---|---|
| **Owner** | TBD (proposed: docs lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Format** | Loosely follows [Keep a Changelog](https://keepachangelog.com). Semantic versioning. |

> This page is the human-facing summary. The authoritative record is git tags plus `RELEASING.md` commit messages. Earlier versions (<v2.4.1) can be enumerated via `git tag --sort=-version:refname` once the repo's `safe.directory` issue is resolved — the entries below are what is known from code and existing documentation; older versions are placeholders until git history can be mined.

## [Unreleased]

Changes on `main` not yet tagged.

### Added
- `docs/wiki-src/` two-layer documentation source + publish script + CI dry-run workflow (Phase 1).
- Full wiki content across Start Here, Core Concepts, Architecture, Operational SOPs, Development SOPs, Runbooks, Reference, Standards & Governance, Templates, Change History (Phases 1-9).

### Changed
- Documentation ownership model codified in [Documentation Standards](Standards-and-Governance-Documentation-Standards).

### Outstanding
- Open questions Q-1..Q-8 — see [Open Questions](Development-SOPs-Documentation-Open-Questions).

---

## [2.4.2] — current

### Added
- Comprehensive admin-panel HTTP routes including `/api/projects/configured`, `/api/config` (with hot-reload vs restart-required field split), `/api/watcher/start|stop|status`, `/api/map/points|recompute`, `/api/mcp-config`, `/api/activity`, `/api/crash-history`, `/api/shutdown`.
- Scale warnings in `rag doctor` via `compute_scale_warning` — alerts when local-mode Qdrant points approach or exceed the single-process limit.
- `rag doctor` collection enumeration and crash-history surfacing.

### Changed
- MCP server logs strictly to stderr so stdout remains clean for MCP stdio transport.
- `compact` output mode on `/api/search` for token-efficient MCP responses.

### Notes
- **Dev vs installed port:** `_default_service_port` returns `21420` (installed) or `21421` (dev) to allow both modes to coexist on the same host.
- `rag index` is **service-required** — no direct-mode fallback. Other dual-mode commands (`rag search`, `rag status`, `rag projects`, `rag rebuild`) retain the full fallback.

---

## [2.4.1] — schema gate baseline

### Changed
- Qdrant collection schema and/or SQLite state DB schema changed in a way that makes the **new reset paths refuse to run on pre-v2.4.1 data** (see [Pre-v2.4.1 Reset Blocked](Runbooks-Pre-v2-4-1-Reset-Blocked)).

### Migration
- Upgrade from pre-v2.4.1 requires a clean-slate approach until the exact schema delta is documented (tracked as [Q-7](Development-SOPs-Documentation-Open-Questions)). See [Pre-v2.4.1 to Current migration guide](Change-History-Migration-Guides-Pre-v2-4-1-to-Current).

### Notes
- This version is the **reset gate** — v2.4.1+ is required to use `rag reset [--data | --nuclear]`.

---

## [< 2.4.1] — earlier history

Earlier versions are not yet enumerated here. To populate:

1. Resolve the `safe.directory` issue and run `git tag --sort=-version:refname`.
2. For each tag, summarize the delta from `RELEASING.md` commit messages and the changelog entry that was current at tag time.
3. Add one section per version above this line.

## Conventions for new entries

- New entries go under **[Unreleased]** at the top.
- At release time, [Release Checklist](Development-SOPs-Release-Release-Checklist) renames `[Unreleased]` to `[X.Y.Z]` with the date.
- Categories (where used): **Added**, **Changed**, **Deprecated**, **Removed**, **Fixed**, **Security**, **Migration**, **Notes**.
- Keep entries terse. Link to SOPs, runbooks, or ADRs for detail.

## Related

- [Release Checklist](Development-SOPs-Release-Release-Checklist).
- [Architecture Decisions](Standards-and-Governance-Architecture-Decisions) — the ADRs (ADR-1..15) locked 2026-04-06 predate this changelog format.
- `RELEASING.md` and `docs/RELEASE_LIFECYCLE.md` (in-repo).
