# Reg / ragtools GitHub Wiki — Implementation Plan

**Status:** Draft v1 (2026-04-18)
**Owner:** TBD (proposed: docs lead)
**Baseline architecture:** workflow-first, source-driven, two-layer (repo `docs/wiki-src/` → published GitHub Wiki)
**Target version of record:** `pyproject.toml` version at each phase exit (currently 2.4.2)

This plan turns the repo + decision history into a phased, executable wiki build. Every phase lists concrete deliverables, inputs, exit criteria, and evidence paths. Phases are ordered by **risk reduction and user value**, not by IA order.

---

## Section A — Repository & Sessions Analysis (Summary)

**What Reg actually is (from code, not marketing):**
A local-first, single-process, Markdown-only RAG system. Python 3.12 + Qdrant (local mode, one collection `markdown_kb`, project isolation via payload filter) + Sentence-Transformers (`all-MiniLM-L6-v2`, 384d, cosine, normalized). Shipped as:
- A Typer CLI (`rag`) with dual-mode (service-probe at `127.0.0.1:21420`, fall back to direct Qdrant)
- A FastAPI+Uvicorn long-running service + htmx/Jinja2 admin panel
- An MCP server (`rag-mcp`) with proxy/direct modes
- A Windows installer (Inno Setup) + Task Scheduler startup integration
- A packaged PyInstaller bundle in `dist/`

**Main technical domains:**
config resolution · chunking · embedding · indexing state (SQLite) · retrieval · ignore rules · service lifecycle · watcher · MCP proxy · CLI dual-mode · admin panel · Windows packaging/installer · Task Scheduler startup · reset escalation.

**Session/history evidence (what was built and why):**
- `docs/decisions.md` — 15 locked architectural decisions (dated 2026-04-06). Treat as immutable inputs to the wiki.
- `docs/workflows.md` — 11 Mermaid flowcharts (install, project setup, indexing, search, watcher, service lifecycle, startup, config, error recovery, release). Direct source for Architecture pages.
- `tasks/roadmap_v2.md` — 7-phase engineering roadmap (Phase 0 → Phase 7). Phases 0–5 largely complete, 6–7 in progress. Direct source for Change History and Architecture evolution.
- `doc.md` — 1000+ line operational knowledge base. Primary source for Operational SOPs.
- `RELEASING.md` — Release checklist and winget submission. Direct source for release SOP.
- `.claude/skills/rag:*` — 5 live skills (`rag-setup`, `rag-doctor`, `rag-projects`, `rag-reset`, `rag-config`). Each maps to a user-facing SOP.
- `.claude/agents/rag-log-monitor.md` — Catalogs 54 known issue classes and references a failure taxonomy (F-001..F-012, P-RULE, P-DEDUPE) — the definitions themselves are NOT in the repo.

**Unstable / risky / poorly documented areas (SOP priority candidates):**
1. **Windows packaging (PyInstaller + PyTorch)** — roadmap Phase 6 explicitly tagged "showstopper risk."
2. **Single-process Qdrant constraint** — the most fragile invariant; violation causes lock contention.
3. **Service start/stop + PID management on Windows** (no `fork`, `CREATE_NO_WINDOW`, detached).
4. **MCP proxy vs direct mode decision at startup** — mode is locked for the session, misconfig is silent.
5. **Port 21420 conflicts** — not handled gracefully.
6. **Pre-v2.4.1 version gate in `rag reset`** — schema migration undocumented.
7. **`.ragignore` reload semantics during live watcher sessions.**

**High-priority SOP candidates (in order):**
Install → Repair → Configuration precedence → Service lifecycle → Installed-vs-Standalone → Reset escalation → MCP integration → Add-a-Command → Release → Testing matrix.

---

## Section B — Documentation Inventory

**Existing sources (load-bearing):**
| Source | Maps to |
|---|---|
| `README.md` | Home, Start Here, Install Quick Path |
| `docs/decisions.md` | Architecture, Standards & Governance |
| `docs/workflows.md` | Architecture diagrams (11 ready-to-port Mermaid charts) |
| `doc.md` | Operational SOPs bulk source |
| `RELEASING.md` | Development-SOPs/Release |
| `tasks/roadmap_v2.md` | Change History, Architecture rationale |
| `CLAUDE.md` | Core Concepts, Standards |
| `pyproject.toml` + `src/ragtools/config.py` | Reference/Configuration-Keys, Reference/Entry-Points |
| `.claude/skills/rag:*` | Operational SOPs (one SOP per skill) |
| `src/ragtools/cli.py` | Reference/CLI |
| `src/ragtools/service/routes.py` | Reference/HTTP-API |
| `src/ragtools/integration/mcp_server.py` | Architecture/MCP, Operational-SOPs/MCP-Setup |
| `installer.iss`, `scripts/build.py` | Development-SOPs/Build-&-Package |
| `tests/` (19 modules, 253 tests) | Development-SOPs/Testing-Matrix |

**Missing — will be authored in this plan:**
Home, Start Here, Core Concepts glossary, unified CLI reference, unified HTTP API reference, SOP template, runbook template, sidebar (`_Sidebar.md`), footer (`_Footer.md`), wiki publish workflow, ownership table, change-history page, standards page.

**Blocked on stakeholder input (tracked in §E Open Questions):**
F-001..F-012 failure catalog · P-RULE / P-DEDUPE definitions · "Barometer-vs-Non-Barometer" runtime semantics · Plugin system scope (implement vs defer) · Official wiki publish mechanism (GitHub Wiki .wiki.git vs Pages).

---

## Section C — Wiki Build Strategy

**Two-layer model (confirmed):**
- **Layer A — Source (authoritative):** `docs/wiki-src/` inside the main repo. All edits happen here. PR-reviewed. Versioned with code.
- **Layer B — Published:** GitHub Wiki (`<repo>.wiki.git`). Read-only for humans; written by automation only.

**Recommended `docs/wiki-src/` folder structure:**
```
docs/wiki-src/
├── Home.md
├── _Sidebar.md
├── _Footer.md
├── Start-Here/
│   ├── Prerequisites.md
│   ├── Quick-Install-Dev.md
│   └── Quick-Install-Packaged.md
├── Core-Concepts/
│   ├── Glossary.md
│   ├── Project-Model.md
│   ├── Single-Process-Invariant.md
│   └── Confidence-Model.md
├── Architecture/
│   ├── System-Overview.md
│   ├── Configuration-Resolution-Flow.md
│   ├── Install-Decision-Tree.md
│   ├── Service-Lifecycle.md
│   ├── MCP-Proxy-Decision.md
│   ├── Indexing-Pipeline.md
│   ├── Watcher-Flow.md
│   ├── CLI-Dual-Mode.md
│   └── Reset-Escalation.md
├── Operational-SOPs/
│   ├── Installation/
│   │   ├── Fresh-Install-Dev.md
│   │   └── Fresh-Install-Packaged.md
│   ├── Configuration/
│   │   └── Configuration-Precedence.md
│   ├── Runtime/
│   │   ├── Installed-App-vs-Standalone-Behavior.md
│   │   └── Barometer-vs-Non-Barometer-Behavior.md  # STUB until defined
│   ├── Service/
│   │   ├── Start-and-Stop-Service.md
│   │   ├── Install-Startup-Task.md
│   │   └── Port-Conflict-Resolution.md
│   ├── Projects/
│   │   ├── Add-a-Project.md
│   │   └── Enable-Disable-Rebuild.md
│   └── Repair/
│       ├── Repair-Broken-Installation.md
│       ├── Soft-Reset.md
│       ├── Data-Reset.md
│       └── Nuclear-Reset.md
├── Development-SOPs/
│   ├── Commands/
│   │   └── Add-a-New-Command.md
│   ├── Skills/
│   │   └── Add-a-New-Slash-Command.md
│   ├── HTTP-API/
│   │   └── Add-a-New-Endpoint.md
│   ├── Hooks/
│   │   └── Hook-Safety-Rules.md
│   ├── Plugins/
│   │   └── Add-a-New-Plugin.md  # STUB until scope decided
│   ├── Testing/
│   │   └── Install-Upgrade-Repair-Test-Matrix.md
│   ├── Release/
│   │   ├── Release-Checklist.md
│   │   └── Winget-Submission.md
│   └── Documentation/
│       └── Wiki-Publishing-Process.md
├── Runbooks/
│   ├── Service-Fails-to-Start.md
│   ├── MCP-Server-Fails-to-Load.md
│   ├── Qdrant-Lock-Contention.md
│   ├── Port-21420-In-Use.md
│   ├── Watcher-Permission-Denied.md
│   └── Pre-v2.4.1-Reset-Blocked.md
├── Reference/
│   ├── CLI.md
│   ├── HTTP-API.md
│   ├── MCP-Tools.md
│   ├── Configuration-Keys.md
│   ├── Environment-Variables.md
│   ├── File-Layout.md
│   └── Known-Failure-Codes.md  # STUB until F-001..F-012 provided
├── Standards-and-Governance/
│   ├── Architecture-Decisions.md        # rendered view of docs/decisions.md
│   ├── Coding-Standards.md
│   ├── Testing-Standards.md
│   ├── Review-and-Merge-Standards.md
│   └── Documentation-Standards.md
├── Templates/
│   ├── SOP-Template.md
│   ├── Runbook-Template.md
│   ├── Architecture-Page-Template.md
│   └── Reference-Page-Template.md
└── Change-History/
    ├── Changelog.md
    └── Migration-Guides/
        └── Pre-v2.4.1-to-Current.md
```

**Publish workflow (recommended):**
A GitHub Actions workflow `publish-wiki.yml` triggers on push to `main` when `docs/wiki-src/**` changes. It:
1. Clones `<repo>.wiki.git` into a temp dir.
2. Rsync-copies `docs/wiki-src/` into the wiki clone, normalizing `/` paths to wiki's flat filename scheme (e.g. `Operational-SOPs/Installation/Fresh-Install-Dev.md` → `Operational-SOPs-Installation-Fresh-Install-Dev.md`), plus a slug map for intra-wiki links.
3. Runs a link checker (`lychee`) over the rewritten output.
4. Runs a Mermaid syntax check (`mmdc --dry-run` or `markdown-link-check`).
5. Commits + pushes to the wiki repo with message `docs(wiki): sync from <sha>`.

**Alternative (lighter):** a manual PowerShell script `scripts/publish_wiki.ps1` for first iteration, promote to CI in Phase 10.

**Ownership model (proposed):**
| Domain | Primary owner | Review required from |
|---|---|---|
| Architecture/, Standards-and-Governance/ | Eng lead | N/A (owner decides) |
| Operational-SOPs/ | Ops / support lead | Eng lead for code-linked claims |
| Development-SOPs/ | Eng lead | Docs lead |
| Runbooks/ | On-call / support lead | Eng lead |
| Reference/ | Generated/maintained by Eng lead | Docs lead |
| Templates/, Change-History/ | Docs lead | Eng lead |

Every SOP page carries `Owner:` and `Last validated against version:` in frontmatter.

---

## Section D — 10-Phase Delivery Plan

Each phase uses the fixed template:
`Objective · Why it matters · Inputs required · Repo areas to inspect · Documentation outputs · Specific pages · Mermaid diagrams · Dependencies · Validation method · Exit criteria`.

---

### Phase 1 — Foundation: Scaffolding, Templates, Sidebar, Publish Skeleton

**Objective:** Establish the two-layer structure, lock templates, produce a working (even if manual) sync path from `docs/wiki-src/` to the GitHub Wiki.

**Why it matters:** Every subsequent page plugs into this scaffold. Without rigid templates and a publish path, content drifts and links rot immediately.

**Inputs required:** Confirmation of publish target (GitHub Wiki `.wiki.git` assumed); owner list; version of record (2.4.2).

**Repo areas to inspect:** `docs/`, `.github/workflows/`, `scripts/`, `README.md`.

**Documentation outputs:**
- `docs/wiki-src/` tree (all folders created with `.gitkeep` where empty).
- `docs/wiki-src/Templates/SOP-Template.md` using the rigid 13-field SOP template (Purpose, Scope, Trigger, Preconditions, Inputs, Steps, Validation, Failure modes, Recovery/rollback, Related code paths, Related commands, Last validated against version, Owner).
- `docs/wiki-src/Templates/Runbook-Template.md` (Symptom, Quick check, Diagnostic commands, Root causes, Fix procedure, Verification, Escalation, Related failure codes).
- `docs/wiki-src/Templates/Architecture-Page-Template.md` (Context, Decision link, Diagram, Walkthrough, Code paths, Edge cases).
- `docs/wiki-src/Templates/Reference-Page-Template.md` (Index, Table, Examples, Deprecations).
- `docs/wiki-src/_Sidebar.md` — opinionated navigation (see Section E).
- `docs/wiki-src/_Footer.md` — version + "source of truth" link back to `docs/wiki-src/`.
- `scripts/publish_wiki.ps1` (manual publish, idempotent).
- `.github/workflows/publish-wiki.yml` (dry-run-only in Phase 1, activate in Phase 10).
- `docs/wiki-src/Development-SOPs/Documentation/Wiki-Publishing-Process.md` (initial draft).

**Specific pages to create:** Templates (×4), `_Sidebar.md`, `_Footer.md`, `Wiki-Publishing-Process.md` (draft).

**Mermaid diagrams:** Docs publish flow (`docs/wiki-src/` → PR → CI → wiki.git).

**Dependencies:** None.

**Validation method:**
1. Run `scripts/publish_wiki.ps1 --dry-run` locally — prints the file list it would sync.
2. Confirm sidebar renders correctly by manually copying `_Sidebar.md` to a fork of the wiki.
3. Lint all templates with `markdownlint`.

**Exit criteria:**
- All four templates committed and reviewed.
- `_Sidebar.md` covers every top-level IA area.
- Publish script runs end-to-end (dry-run) without errors.
- Ownership table documented in `Standards-and-Governance/Documentation-Standards.md` (stub).

---

### Phase 2 — Home, Start Here, Core Concepts (Orientation Layer)

**Objective:** Give a first-time reader a working mental model in <10 minutes. Define every term used later in SOPs/Architecture.

**Why it matters:** Every downstream page assumes shared vocabulary (project, chunk, collection, confidence band, service, proxy mode, installed vs standalone). Defining these once prevents per-page digressions.

**Inputs required:** `README.md`, `CLAUDE.md`, `docs/decisions.md`, `src/ragtools/models.py`, `src/ragtools/retrieval/searcher.py`.

**Repo areas to inspect:** top-level README, `CLAUDE.md`, `src/ragtools/models.py`, `src/ragtools/retrieval/formatter.py` (confidence bands), `src/ragtools/config.py` (dev vs packaged detection).

**Documentation outputs:**
- `Home.md` — elevator summary, "read these 3 pages first" links, status badges (version, tests).
- `Start-Here/Prerequisites.md` — Python 3.12, Windows 11 / macOS arm64, disk/RAM, no Docker.
- `Start-Here/Quick-Install-Dev.md` — `pip install -e ".[dev]"` flow.
- `Start-Here/Quick-Install-Packaged.md` — installer `.exe` flow.
- `Core-Concepts/Glossary.md` — 20–30 terms (project, chunk, collection, payload filter, confidence HIGH/MODERATE/LOW, service, proxy mode, direct mode, dual-mode CLI, `.ragignore`, content root, data dir, installed vs standalone).
- `Core-Concepts/Project-Model.md` — "one collection, payload-filtered projects" explanation.
- `Core-Concepts/Single-Process-Invariant.md` — the Qdrant lock rule (Decision 1).
- `Core-Concepts/Confidence-Model.md` — score threshold 0.3, bands ≥0.7 / 0.5–0.7 / <0.5.

**Specific pages:** Home + 3 Start-Here + 4 Core-Concepts = 8 pages.

**Mermaid diagrams:**
- System overview (CLI / MCP / Service / Admin Panel all pointing at one Qdrant collection).
- Confidence band decision (score → HIGH/MODERATE/LOW → user messaging).

**Dependencies:** Phase 1 (templates, sidebar).

**Validation method:**
1. Read each page cold as a new user; verify no undefined term appears.
2. Grep all later pages for terms — every term used elsewhere must exist in `Glossary.md`.
3. Cross-link check: every Core-Concepts page linked from Home; glossary linked from every SOP front-matter.

**Exit criteria:**
- All 8 pages drafted and reviewed.
- Glossary covers ≥95% of domain-specific nouns used anywhere else in `docs/wiki-src/`.
- Home renders correctly in GitHub Wiki preview.

---

### Phase 3 — Architecture Backbone (Flows, Diagrams, Invariants)

**Objective:** Publish the canonical architecture view, grounded in `docs/decisions.md` + `docs/workflows.md`. Every SOP and runbook will link into these pages instead of re-explaining.

**Why it matters:** This is the highest-leverage phase for drift prevention. Once Architecture is authoritative, operational pages can stay short and behavioral.

**Inputs required:** `docs/decisions.md`, `docs/workflows.md`, `src/ragtools/config.py`, `src/ragtools/service/*.py`, `src/ragtools/integration/mcp_server.py`, `src/ragtools/cli.py`, `src/ragtools/indexing/*.py`, `src/ragtools/watcher/*.py`, `src/ragtools/service/watcher_thread.py`.

**Repo areas to inspect:** whole `src/ragtools/`, focusing on entry paths and lifecycle code.

**Documentation outputs:**
- `Architecture/System-Overview.md` — component map + single-process invariant restated.
- `Architecture/Configuration-Resolution-Flow.md` — env > `%LOCALAPPDATA%\RAGTools\config.toml` > `./ragtools.toml` > defaults, with exact function refs (`config.py:_find_config_path`, `config.py:get_data_dir`, `config.py:is_packaged`).
- `Architecture/Install-Decision-Tree.md` — installed vs dev detection via `sys.frozen`.
- `Architecture/Service-Lifecycle.md` — start → load encoder → open Qdrant → FastAPI ready → watcher start → PID file; shutdown sequence.
- `Architecture/MCP-Proxy-Decision.md` — startup probe to `/health`, mode lock-in, no mid-session switching.
- `Architecture/Indexing-Pipeline.md` — scanner → chunker → state check → encoder → indexer; heading-enriched embedding text; UUID derivation.
- `Architecture/Watcher-Flow.md` — `watchfiles` → debounce → `.ragignore` reload → per-file incremental.
- `Architecture/CLI-Dual-Mode.md` — probe → forward vs fallback pattern.
- `Architecture/Reset-Escalation.md` — soft / data / nuclear and their side effects.

**Specific pages:** 9 architecture pages.

**Mermaid diagrams (port or rebuild from `docs/workflows.md`):**
1. Install decision tree.
2. Config resolution flow.
3. Service lifecycle (start + shutdown).
4. MCP proxy/direct decision.
5. Indexing pipeline.
6. Watcher event flow.
7. CLI dual-mode probe.
8. Reset escalation tree.

**Dependencies:** Phase 2 (glossary / core concepts referenced everywhere).

**Validation method:**
1. Every architecture page includes a **Code paths** section with `file:line` references that a reader can open.
2. Claim-to-code audit: pick 5 statements per page at random; verify each against source.
3. Re-render all Mermaid blocks in GitHub preview; no syntax errors.

**Exit criteria:**
- 9 architecture pages drafted, reviewed, and merged.
- All 8 Mermaid diagrams render cleanly.
- Every architecture page links back to the relevant Decision in `Standards-and-Governance/Architecture-Decisions.md`.

---

### Phase 4 — Operational SOPs: Installation & Configuration

**Objective:** Cover the two highest-frequency user journeys — getting installed and configuring the tool — as rigid SOPs.

**Why it matters:** Install and config problems dominate support load per `rag-log-monitor.md`'s issue catalog. These must be the most polished SOPs in the wiki.

**Inputs required:** `README.md`, `doc.md` sections on install/config, `installer.iss`, `scripts/build.py`, `src/ragtools/config.py`, `ragtools.toml`.

**Repo areas to inspect:** `installer.iss`, `scripts/build.py`, `scripts/verify_setup.py`, `config.py`, `ragtools.toml`, `.github/workflows/release.yml`.

**Documentation outputs:**
- `Operational-SOPs/Installation/Fresh-Install-Dev.md` — full SOP template.
- `Operational-SOPs/Installation/Fresh-Install-Packaged.md` — full SOP template.
- `Operational-SOPs/Configuration/Configuration-Precedence.md` — operational SOP view of the architecture flow.
- `Reference/Configuration-Keys.md` — table generated/extracted from Pydantic `Settings` model in `config.py`.
- `Reference/Environment-Variables.md` — every `RAG_*` env var, default, override semantics.
- `Reference/File-Layout.md` — dev (`./data/`) vs installed (`%LOCALAPPDATA%\RAGTools\`) file trees.

**Specific pages:** 6 pages.

**Mermaid diagrams:**
- Fresh install decision flow (Dev vs Packaged).
- Config precedence decision (linked from Architecture but restated operationally).

**Dependencies:** Phase 3 (Architecture pages for backlinks).

**Validation method:**
1. Run both install SOPs end-to-end on clean VM/sandbox (dev install + installer `.exe`). Every step executes exactly as written.
2. For `Configuration-Keys.md`, diff the page against `Settings` fields to confirm 100% coverage.
3. `rag doctor` after each SOP exits with status `OK`.

**Exit criteria:**
- Both install SOPs reproduce a working Reg on clean machines without ad-hoc steps.
- Config-key table matches Pydantic `Settings` model field-for-field.
- SOPs link to the matching Architecture pages and to `rag:rag-setup` skill.

---

### Phase 5 — Operational SOPs: Runtime, Service, Repair

**Objective:** Cover service operation and the repair escalation ladder. Map every `/rag-*` skill to an Operational SOP.

**Why it matters:** Service lifecycle and repair are where a working install becomes an unworking one. This is the second-heaviest support load.

**Inputs required:** `src/ragtools/service/*.py` (run, routes, owner, watcher_thread, startup, process), `.claude/skills/rag:rag-doctor`, `.claude/skills/rag:rag-reset`, `.claude/skills/rag:rag-projects`, `doc.md` sections on repair, `tasks/roadmap_v2.md` phase 2+5.

**Repo areas to inspect:** `src/ragtools/service/`, `src/ragtools/cli.py` (service + reset commands), `.claude/skills/rag:*`, `installer.iss` (startup task registration).

**Documentation outputs:**
- `Operational-SOPs/Runtime/Installed-App-vs-Standalone-Behavior.md` — the installed-vs-dev decision surface, what changes (data dir, config path, startup task).
- `Operational-SOPs/Runtime/Barometer-vs-Non-Barometer-Behavior.md` — **STUB** with `> BLOCKED: definition required. Owner: <TBD>. See Open Questions Q-1.` Page placeholder committed so sidebar is complete.
- `Operational-SOPs/Service/Start-and-Stop-Service.md`.
- `Operational-SOPs/Service/Install-Startup-Task.md`.
- `Operational-SOPs/Service/Port-Conflict-Resolution.md` — port 21420 in-use diagnosis.
- `Operational-SOPs/Projects/Add-a-Project.md`.
- `Operational-SOPs/Projects/Enable-Disable-Rebuild.md`.
- `Operational-SOPs/Repair/Repair-Broken-Installation.md` — orchestrator SOP pointing at escalation levels.
- `Operational-SOPs/Repair/Soft-Reset.md`, `Data-Reset.md`, `Nuclear-Reset.md` — one SOP per level with exact `DELETE`-confirmation copy.

**Specific pages:** 10 pages (1 stub).

**Mermaid diagrams:**
- Service start/stop state machine.
- Repair escalation decision tree (symptom → level).

**Dependencies:** Phase 3 (Architecture/Service-Lifecycle, Architecture/Reset-Escalation).

**Validation method:**
1. Every SOP references the relevant `/rag-*` skill by exact name and shows both CLI and skill paths.
2. Run each repair SOP against an intentionally-broken install (corrupt SQLite, stale PID, missing collection). Each SOP resolves its target symptom.
3. Port-conflict SOP tested by binding 21420 before `rag service start`.

**Exit criteria:**
- All `/rag-*` skills have a matching Operational SOP page.
- Repair ladder SOPs tested against induced failures.
- Barometer stub page exists with a visible BLOCKED banner and points at Q-1.

---

### Phase 6 — Development SOPs: Commands, Skills, Testing, Release

**Objective:** Make it possible for a new contributor to add a CLI command, a slash command, and a test, then cut a release — all from the wiki alone.

**Why it matters:** Contributor onboarding is the slowest wiki-negligent loop. Codifying it converts tribal knowledge into reproducible practice.

**Inputs required:** `src/ragtools/cli.py` (dual-mode pattern), `src/ragtools/service/routes.py`, `.claude/skills/rag:*` structures, `tests/` layout, `tests/conftest.py`, `RELEASING.md`, `.github/workflows/*.yml`, `pyproject.toml`.

**Repo areas to inspect:** Typer app registration, HTTP route registration, skill frontmatter conventions, test fixtures, release automation.

**Documentation outputs:**
- `Development-SOPs/Commands/Add-a-New-Command.md` — Typer subcommand + dual-mode probe + tests.
- `Development-SOPs/Skills/Add-a-New-Slash-Command.md` — `.claude/skills/rag:*` authoring conventions.
- `Development-SOPs/HTTP-API/Add-a-New-Endpoint.md` — FastAPI route + dual-mode CLI integration.
- `Development-SOPs/Testing/Install-Upgrade-Repair-Test-Matrix.md` — full matrix (install method × platform × mode × test category).
- `Development-SOPs/Release/Release-Checklist.md` — port of `RELEASING.md` into SOP template.
- `Development-SOPs/Release/Winget-Submission.md`.

**Specific pages:** 6 pages.

**Mermaid diagrams:**
- Command execution flow (CLI → dual-mode probe → service or direct).
- Release flow (version bump → tag → Actions → artifacts → winget).

**Dependencies:** Phases 3 & 4 (architecture + config reference).

**Validation method:**
1. A contributor (or simulated fresh context) follows `Add-a-New-Command.md` and produces a passing command + test.
2. `Release-Checklist.md` matches the last successful release's actual steps (git log + workflow run comparison).
3. Test matrix compared against `tests/` module names for coverage gaps.

**Exit criteria:**
- All 6 pages reviewed and merged.
- A "hello" command successfully authored following the SOP (can be thrown away, but must exist in a branch for proof).
- Test matrix reflects every test module.

---

### Phase 7 — Development SOPs: Integration Surfaces (MCP, HTTP API, Hooks, Plugins)

**Objective:** Document every extension point, marking planned-but-unbuilt surfaces clearly so readers aren't misled.

**Why it matters:** The repo has multiple near-plugin extension surfaces (MCP tools, HTTP API, Claude Code skills, hook observability). Without a single place to orient, contributors reinvent or pick the wrong one.

**Inputs required:** `src/ragtools/integration/mcp_server.py`, `src/ragtools/service/routes.py`, `.claude/skills/rag:rag-config` (hook references), `tasks/roadmap_v2.md` (Phase 7 plugin vision).

**Repo areas to inspect:** MCP tool definitions, FastAPI routers, any hook scaffolding.

**Documentation outputs:**
- `Reference/MCP-Tools.md` — `search_knowledge_base`, `list_projects`, `index_status` signatures and semantics.
- `Reference/HTTP-API.md` — every route under `/api/*`, method, request, response.
- `Development-SOPs/Hooks/Hook-Safety-Rules.md` — UserPromptSubmit hook + any other observability hooks; scope, side-effect rules, opt-in controls. **Marked as "integration is partial; see Q-4".**
- `Development-SOPs/Plugins/Add-a-New-Plugin.md` — **STUB**: "Plugin system is not implemented as of v2.4.2. See Q-5 for scoping. Current extension surfaces: MCP tools (§Reference/MCP-Tools), HTTP API (§Reference/HTTP-API), Skills (§Development-SOPs/Skills)."

**Specific pages:** 4 pages (1 full stub, 1 partial).

**Mermaid diagrams:**
- MCP proxy/direct mode decision (re-linked from Architecture but extension-surface framing).
- Hook execution flow (placeholder if implementation minimal).

**Dependencies:** Phases 3 & 6.

**Validation method:**
1. MCP tool table matches `mcp_server.py` tool registrations exactly.
2. HTTP API table diffed against `routes.py` — no missing or renamed routes.
3. Every stub carries a banner pointing at the corresponding Open Question.

**Exit criteria:**
- MCP and HTTP API references are 100% accurate and generated from code.
- Plugin page exists but does not pretend to describe unbuilt functionality.

---

### Phase 8 — Runbooks + Failure Catalog Integration

**Objective:** Publish runbooks for the concrete failures we can document today. Reserve F-001..F-012 for later pull-through when stakeholders provide definitions.

**Why it matters:** Runbooks are the other end of SOPs — what to do when SOPs fail. `rag-log-monitor.md` catalogs 54 issue types; we can ship runbooks for the ones visible in code, and stub the taxonomy page.

**Inputs required:** `.claude/agents/rag-log-monitor.md`, `doc.md` repair section, `src/ragtools/service/run.py` errors, `tests/test_service.py` + `test_owner.py`, Q-2 (F-001..F-012), Q-3 (P-RULE/P-DEDUPE).

**Repo areas to inspect:** error paths in `service/`, `integration/mcp_server.py`, `watcher/`, `cli.py`; log message catalog in `rag-log-monitor.md`.

**Documentation outputs:**
- `Runbooks/Service-Fails-to-Start.md`.
- `Runbooks/MCP-Server-Fails-to-Load.md` (replaces target's "Plugin-Fails-to-Load" with a runbook for an extension surface that actually exists; note in body).
- `Runbooks/Qdrant-Lock-Contention.md`.
- `Runbooks/Port-21420-In-Use.md`.
- `Runbooks/Watcher-Permission-Denied.md`.
- `Runbooks/Pre-v2.4.1-Reset-Blocked.md`.
- `Reference/Known-Failure-Codes.md` — **STUB** with prefilled scaffolding for F-001..F-012 and P-RULE/P-DEDUPE; rows empty until Q-2/Q-3 resolve.

**Specific pages:** 7 pages (1 scaffolded stub).

**Mermaid diagrams:**
- Repair decision flow (symptom → runbook → SOP).

**Dependencies:** Phases 3 (Architecture) + 5 (Repair SOPs).

**Validation method:**
1. Induce each documented failure on a test machine; runbook resolves it.
2. Cross-link check: every runbook links to the matching Operational SOP and to relevant Reference tables.
3. `rag-log-monitor` agent's 54 issue types reviewed; each is either (a) covered by a runbook, (b) covered by a Reference entry, or (c) explicitly deferred with a reason.

**Exit criteria:**
- All 6 concrete runbooks authored and tested.
- Known-Failure-Codes page exists with scaffolding ready for F-001..F-012 insertion.

---

### Phase 9 — Standards, Governance, Templates, Change History

**Objective:** Make the wiki self-maintaining: coding standards, review rules, documentation rules, changelog, migration guides.

**Why it matters:** Without codified standards, Phase 10's maintenance loop degrades in months.

**Inputs required:** `docs/decisions.md`, `CLAUDE.md`, `pyproject.toml` (ruff/black/flake8 config), `tasks/roadmap_v2.md`, `RELEASING.md`, git log since v0.1.0.

**Repo areas to inspect:** linting/formatting config, CI workflows, commit history for version progression.

**Documentation outputs:**
- `Standards-and-Governance/Architecture-Decisions.md` — rendered index of `docs/decisions.md` decisions (1 per row) with link to full decision text.
- `Standards-and-Governance/Coding-Standards.md` — Python style, type hints, error handling, logging conventions.
- `Standards-and-Governance/Testing-Standards.md` — `QdrantClient(":memory:")` rule, fixture conventions, what every PR must test.
- `Standards-and-Governance/Review-and-Merge-Standards.md` — PR checklist, required reviews, merge rules.
- `Standards-and-Governance/Documentation-Standards.md` — wiki-src rule, SOP template usage, ownership model, "last validated" freshness rule.
- `Change-History/Changelog.md` — version-by-version summary.
- `Change-History/Migration-Guides/Pre-v2.4.1-to-Current.md` — specifically the reset-block gate and schema change.
- Re-audit `Templates/*.md` against how they ended up being used in Phases 4–8; tighten where drift appeared.

**Specific pages:** 7 pages.

**Mermaid diagrams:**
- PR lifecycle (draft → CI → review → merge → wiki publish).

**Dependencies:** All prior content phases (so standards are grounded in what was actually produced).

**Validation method:**
1. Standards pages are each linked from every SOP/runbook page that would violate them if ignored.
2. Documentation-Standards defines the exact `Owner:` and `Last validated:` frontmatter every SOP carries.
3. Changelog reconciles with git tags.

**Exit criteria:**
- Standards pages reviewed and merged.
- Every existing SOP/Runbook/Architecture page has `Owner:` and `Last validated against version:` frontmatter — bulk update in this phase.

---

### Phase 10 — Publish Automation, Validation, Maintenance Loop

**Objective:** Move from "we wrote pages" to "the wiki is a living system": CI-based publish, link/diagram validation, SOP freshness audits, open-question tracking.

**Why it matters:** Docs that require human effort to publish don't get published. Docs that aren't validated drift silently.

**Inputs required:** All prior phases. `scripts/publish_wiki.ps1`, `.github/workflows/publish-wiki.yml`.

**Repo areas to inspect:** `.github/workflows/`, any existing doc CI, `scripts/`.

**Documentation outputs:**
- Activated `.github/workflows/publish-wiki.yml`: trigger on `push` to `main` when `docs/wiki-src/**` changes; sync to wiki.git; run `lychee` link check; run `markdown-link-check`; run Mermaid syntax check.
- `.github/workflows/docs-freshness.yml` — scheduled monthly; flags any SOP whose `Last validated against version` lags the current `pyproject.toml` version by more than N minor versions.
- `docs/wiki-src/Development-SOPs/Documentation/Wiki-Publishing-Process.md` — promoted from Phase 1 draft to full SOP.
- `docs/wiki-src/Development-SOPs/Documentation/Open-Questions.md` — single page tracking Q-1..Q-N with owner and resolution state; every stub page links here.
- Ownership assignments filled in for every page (replacing `Owner: TBD`).

**Specific pages:** 2 new + 1 promoted + all-page ownership backfill.

**Mermaid diagrams:**
- End-to-end publish flow (edit → PR → merge → Actions → wiki.git).
- Freshness audit flow.

**Dependencies:** Phases 1–9.

**Validation method:**
1. Trigger `publish-wiki.yml` on a trivial wiki-src change; confirm the wiki updates and link check passes.
2. Deliberately break a link; confirm CI fails.
3. Deliberately introduce an invalid Mermaid block; confirm CI fails.
4. Run freshness workflow manually; confirm it reports the expected SOPs.

**Exit criteria:**
- Any merge to `main` touching `docs/wiki-src/` auto-publishes to the GitHub Wiki.
- Link and Mermaid validation blocks merges on failure.
- Every page has a named owner and a "last validated" version.
- Open-Questions page exists and is linked from every stub (Barometer, Plugin, Failure-Codes, Hooks).

---

## Section E — Execution Readiness

### Exact files/folders to create first (Phase 1, day 1)
- `docs/wiki-src/` (directory + `.gitkeep` in each subfolder listed in Section C).
- `docs/wiki-src/Templates/SOP-Template.md`
- `docs/wiki-src/Templates/Runbook-Template.md`
- `docs/wiki-src/Templates/Architecture-Page-Template.md`
- `docs/wiki-src/Templates/Reference-Page-Template.md`
- `docs/wiki-src/_Sidebar.md`
- `docs/wiki-src/_Footer.md`
- `docs/wiki-src/Home.md` (placeholder)
- `scripts/publish_wiki.ps1`
- `.github/workflows/publish-wiki.yml` (dry-run mode)

### First wiki pages to draft (after Phase 1)
Highest-leverage order:
1. `Architecture/Configuration-Resolution-Flow.md`
2. `Architecture/Install-Decision-Tree.md`
3. `Architecture/Service-Lifecycle.md`
4. `Operational-SOPs/Installation/Fresh-Install-Dev.md`
5. `Operational-SOPs/Installation/Fresh-Install-Packaged.md`
6. `Operational-SOPs/Configuration/Configuration-Precedence.md`
7. `Operational-SOPs/Repair/Repair-Broken-Installation.md`
8. `Reference/Configuration-Keys.md`
9. `Reference/CLI.md`
10. `Core-Concepts/Glossary.md`

### Mermaid diagrams to produce early (Phases 1–3)
1. Docs publish flow (edit → PR → CI → wiki.git).
2. Install decision tree (dev vs packaged).
3. Configuration resolution flow.
4. Service lifecycle (start + shutdown state machine).
5. MCP proxy/direct decision.
6. Indexing pipeline.
7. Watcher event flow.
8. CLI dual-mode probe.
9. Repair escalation decision tree.
10. Release flow.

### Proposed opinionated sidebar (`_Sidebar.md`)
```
**Start Here**
- Home
- Prerequisites
- Quick Install (Dev)
- Quick Install (Packaged)

**Core Concepts**
- Glossary
- Project Model
- Single-Process Invariant
- Confidence Model

**Architecture**
- System Overview
- Configuration Resolution
- Install Decision Tree
- Service Lifecycle
- MCP Proxy Decision
- Indexing Pipeline
- Watcher Flow
- CLI Dual-Mode
- Reset Escalation

**Operational SOPs**
- Installation
- Configuration
- Runtime
- Service
- Projects
- Repair

**Development SOPs**
- Commands
- Skills
- HTTP API
- Hooks
- Plugins (planned)
- Testing
- Release
- Documentation

**Runbooks**
- Service Fails to Start
- MCP Server Fails to Load
- Qdrant Lock Contention
- Port 21420 In Use
- Watcher Permission Denied
- Pre-v2.4.1 Reset Blocked

**Reference**
- CLI
- HTTP API
- MCP Tools
- Configuration Keys
- Environment Variables
- File Layout
- Known Failure Codes

**Standards & Governance**
- Architecture Decisions
- Coding Standards
- Testing Standards
- Review & Merge Standards
- Documentation Standards

**Templates**
- SOP / Runbook / Architecture / Reference

**Change History**
- Changelog
- Migration Guides
```

### Open questions that block accuracy
| ID | Question | Blocks | Owner |
|---|---|---|---|
| **Q-1** | What is "Barometer" mode? Not referenced anywhere in code, docs, decisions, or roadmap. | `Operational-SOPs/Runtime/Barometer-vs-Non-Barometer-Behavior.md` | Stakeholder / product owner |
| **Q-2** | What are F-001..F-012? Referenced in `rag-log-monitor.md` and `/rag-doctor` skill but not defined in repo. | `Reference/Known-Failure-Codes.md`, comprehensive runbook coverage | Maintainer of `rag-log-monitor` agent |
| **Q-3** | What do P-RULE and P-DEDUPE classify? Referenced alongside F-001..F-012. | `Reference/Known-Failure-Codes.md`, doctor-skill docs | Same as Q-2 |
| **Q-4** | UserPromptSubmit hook — implementation status, scope, data written, opt-in flow. | `Development-SOPs/Hooks/Hook-Safety-Rules.md` | Eng lead |
| **Q-5** | Plugin system scope: document intended design (roadmap Phase 7) or defer entirely until implemented? | `Development-SOPs/Plugins/Add-a-New-Plugin.md`, `Architecture/Plugin-Load-Flow.md` | Eng lead / product |
| **Q-6** | Confirmed publish target: GitHub Wiki (`.wiki.git`) or GitHub Pages? | Phases 1 and 10 | Docs lead |
| **Q-7** | Pre-v2.4.1 collection/state schema change — what changed, which releases, migration path. | `Change-History/Migration-Guides/Pre-v2.4.1-to-Current.md`, `Runbooks/Pre-v2.4.1-Reset-Blocked.md` | Eng lead |
| **Q-8** | Ownership assignments per domain (names, not roles). | All SOP frontmatter (`Owner:`), Phase 10 backfill | Project lead |

All Open Questions tracked in `docs/wiki-src/Development-SOPs/Documentation/Open-Questions.md` once Phase 10 creates it; stub pages link there until resolution.

---

## Execution Notes

- **Do not start Phase 2 before Phase 1** — templates and sidebar constrain every later page's structure.
- **Phase 3 is the single biggest leverage phase.** If time is tight, ship Phases 1–3 + Phase 4's install SOPs + Phase 10's publish workflow first; everything else compounds from there.
- **Treat `docs/decisions.md` as read-only.** New decisions get new Decision entries in that file, then surfaced in `Standards-and-Governance/Architecture-Decisions.md`.
- **Never inline Mermaid in multiple pages.** If a diagram appears twice, promote it to an Architecture page and link.
- **Every SOP must cite code.** If it can't, convert the operational claim into an Open Question instead of guessing.
