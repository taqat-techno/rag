# Product Workflow Reference

> **Living document.** This file captures the core user and system workflows for RAG Tools by TaqaTechno.
> Update this file as workflows evolve. Diagrams use Mermaid syntax.

---

## Table of Contents

1. [Install and First Launch](#1-install-and-first-launch)
2. [First Project Setup](#2-first-project-setup)
3. [Multi-Project Management](#3-multi-project-management)
4. [Indexing](#4-indexing)
5. [Search](#5-search)
6. [Watcher](#6-watcher)
7. [Service Lifecycle](#7-service-lifecycle)
8. [Startup and Auto-Registration](#8-startup-and-auto-registration)
9. [Settings and Configuration](#9-settings-and-configuration)
10. [Error Recovery](#10-error-recovery)
11. [Release and Distribution](#11-release-and-distribution)

---

## 1. Install and First Launch

**Purpose:** Get the product running on a fresh Windows machine.

```mermaid
flowchart TD
    A[Download Installer] --> B[Run RAGTools-Setup.exe]
    B --> C[Files installed to Program Files]
    C --> D[PATH registered]
    B --> E[Data dir created at %LOCALAPPDATA%/RAGTools]

    E --> F["rag service start"]
    F --> G[Load encoder model ~10s]
    G --> H[Open Qdrant local storage]
    H --> I[Service ready on localhost:21420]

    I --> J[Auto-register Windows startup task]
    I --> K[Start file watcher]
    I --> L{Open browser configured?}
    L -->|Yes| M[Browser opens admin panel]
    L -->|No| N[Silent background operation]

    M --> O{Projects configured?}
    O -->|No| P[Dashboard: 'Add Your First Project']
    O -->|Yes| Q[Dashboard with stats + search]
```

**Notes:**
- Installer is user-level (no admin required)
- Data directory is separate from install directory (survives upgrades)
- First startup takes 5-10 seconds for encoder model loading
- The Windows startup task auto-registers on first successful service start (no manual action needed)
- Watcher always starts automatically — not a configurable option

---

## 2. First Project Setup

**Purpose:** Go from empty knowledge base to first searchable content.

```mermaid
flowchart TD
    A[Open Admin Panel] --> B[Dashboard shows empty state]
    B --> C["Click 'Add Your First Project'"]
    C --> D[Navigate to Projects page]
    D --> E["Click '+ New Project'"]
    E --> F[Fill: ID, Name, Path]
    F --> G{Path valid?}
    G -->|No| H[Error: Path not found]
    H --> F
    G -->|Yes| I[Project saved to TOML config]
    I --> J[Background auto-index starts]
    J --> K[Watcher restarts to include new path]
    K --> L[Files indexed, stats appear on Dashboard]
    L --> M[Search is now available]
```

**CLI alternative:**
```
rag project add --name "My Docs" --path C:\path\to\docs
rag index                  # requires service running
rag search "my query"
```

**Notes:**
- Project ID is auto-generated from name (lowercase, hyphens) or manually specified
- Adding a project triggers auto-indexing in a background thread (non-blocking)
- Per-project ignore patterns are optional (Advanced section in the add form)
- The watcher automatically picks up the new project path

---

## 3. Multi-Project Management

**Purpose:** Organize multiple knowledge sources as separate projects.

```mermaid
flowchart TD
    A[Projects Page] --> B[View configured projects table]
    B --> C{Action?}
    C -->|Add| D[Fill add form - Save]
    C -->|Edit| E[Inline edit - Change name/path/ignore - Save]
    C -->|Disable| F[Project excluded from indexing and watching]
    C -->|Enable| G[Project re-included in indexing and watching]
    C -->|Remove| H[Confirm dialog]

    D --> I[Auto-index in background]
    H --> J[Delete indexed data from Qdrant]
    J --> K[Remove from config]

    I --> L[Watcher restarts with updated paths]
    K --> L
    F --> L
    G --> L
```

**Notes:**
- Each project has: id, name, path, enabled, ignore_patterns
- Disabling a project excludes it from indexing/watching but keeps existing indexed data in Qdrant
- Removing a project deletes all its indexed data from Qdrant and the state DB
- Watcher auto-restarts whenever the project list changes

---

## 4. Indexing

**Purpose:** Convert Markdown files into searchable vector embeddings.

```mermaid
flowchart TD
    A{Trigger} -->|Auto: Project added| B[Background thread index]
    A -->|Auto: Watcher detects change| C[Incremental index]
    A -->|Manual: CLI 'rag index'| C
    A -->|Manual: Settings - Rebuild| D[Drop all data - Full rebuild]

    C --> E[Scan enabled projects]
    B --> E
    D --> F[Recreate Qdrant collection]
    F --> E

    E --> G[Compare file hashes with state DB]
    G --> H{Changed?}
    H -->|No| I[Skip]
    H -->|Yes| J[Chunk - Embed - Upsert]

    J --> K[SQLite state updated]
    J --> L[Qdrant vectors stored]
    J --> M[Map cache invalidated]
    J --> N[Activity log: index complete]
```

**Per-file pipeline:**
```mermaid
flowchart LR
    A[.md file] --> B[Heading-aware chunking]
    B --> C[SentenceTransformer encode]
    C --> D[Qdrant upsert with project_id]
    D --> E[State DB: hash + chunk count]
```

**Notes:**
- Incremental mode skips unchanged files (SHA256 hash comparison via SQLite)
- Chunk size: 400 tokens, overlap: 100 tokens (configurable in Settings)
- Each chunk gets a deterministic UUID: `sha256(project_id::file_path::chunk_index)`
- Ignore rules applied during scanning: built-in defaults, per-project patterns, .ragignore files
- Indexing holds the QdrantOwner RLock (search is blocked during indexing)
- CLI `rag index` requires the service to be running

---

## 5. Search

**Purpose:** Find relevant content across the knowledge base.

```mermaid
flowchart TD
    A{Entry point} -->|Dashboard quick search| B[Navigate to Search page with query]
    A -->|Search page| C[Enter query directly]
    A -->|CLI: rag search| D[HTTP to service]

    B --> E[Auto-fill and auto-submit]
    C --> E
    D --> E

    E --> F[Encode query via SentenceTransformer]
    F --> G{Project filter?}
    G -->|All projects| H[Query Qdrant: no filter]
    G -->|Specific project| I[Query Qdrant: project_id filter]
    H --> J[Score + rank results]
    I --> J

    J --> K{Results above threshold?}
    K -->|Yes| L[Display result cards]
    K -->|No| M[No results found]

    L --> N["Each card: confidence badge, score, file path, headings, text preview"]
```

**Notes:**
- Dashboard has a quick search bar that navigates to the Search page with the query pre-filled
- Score threshold: 0.3 (results below this are excluded)
- Confidence labels: HIGH (>=0.7), MODERATE (0.5-0.7), LOW (<0.5)
- Default top_k: 10 results
- Search page shows a helpful empty state before first query
- Project dropdown shows only enabled configured projects

---

## 6. Watcher

**Purpose:** Keep the knowledge base current as files change on disk.

```mermaid
flowchart TD
    A[Service starts] --> B[Watcher always starts automatically]
    B --> C[Get enabled project paths]
    C --> D{Any paths?}
    D -->|No| E[Log warning: no projects to watch]
    D -->|Yes| F["watchfiles monitors all project directories"]

    F --> G{File change detected}
    G --> H{File type?}
    H -->|.ragignore| I[Reload ignore rules cache]
    H -->|.md file| J{Ignored by rules?}
    J -->|Yes| K[Skip]
    J -->|No| L[Debounce 3s]
    L --> M[Identify affected project]
    M --> N[Incremental index for that project]
    N --> O[Activity log updated]
```

**Notes:**
- Watcher is always-on — starts automatically with the service, not configurable
- Uses `watchfiles` (Rust-based, low CPU overhead)
- Debounce: 3000ms (waits for file changes to settle before indexing)
- Watches all enabled project paths simultaneously
- Per-project ignore rules applied in the watch filter
- Watcher auto-restarts when projects are added, removed, enabled, or disabled

---

## 7. Service Lifecycle

**Purpose:** How the service starts, runs, and stops.

```mermaid
flowchart TD
    A["rag service start"] --> B[Spawn detached subprocess]
    B --> C[Write PID file]
    C --> D[Setup rotating file logger]
    D --> E[Load Settings from TOML + env vars]
    E --> F[Load SentenceTransformer encoder]
    F --> G[Open Qdrant local storage]
    G --> H[Create QdrantOwner singleton]
    H --> I[Start FastAPI + Uvicorn]
    I --> J[Service ready on port 21420]

    J --> K[Post-startup thread]
    K --> K1[Start file watcher]
    K --> K2[Auto-register startup task]
    K --> K3[Open browser if configured]

    J --> L[Serve API + Admin UI]

    M["rag service stop"] --> N[POST /api/shutdown]
    N --> O[Stop watcher thread]
    O --> P[Close Qdrant client]
    P --> Q[Exit process + delete PID file]
```

**Architecture:**
- Single process owns Qdrant exclusively (QdrantOwner with RLock)
- Watcher runs as a daemon thread inside the service process
- CLI commands route through HTTP when service is running
- MCP server uses per-request Qdrant access (releases lock between queries)

---

## 8. Startup and Auto-Registration

**Purpose:** Service starts automatically when user logs into Windows.

```mermaid
flowchart TD
    A[Service starts for the first time] --> B{Windows platform?}
    B -->|No| C[Skip registration]
    B -->|Yes| D{Startup task exists?}
    D -->|Yes| E[Skip - already registered]
    D -->|No| F[Register Windows Task Scheduler task]
    F --> G{Success?}
    G -->|Yes| H[Log: startup task registered]
    G -->|No| I[Log warning - service continues normally]

    J[Next Windows login] --> K[Task Scheduler triggers after delay]
    K --> L[Service process starts]
    L --> M[Full startup flow from Section 7]
```

**Notes:**
- Auto-registration is idempotent (only registers once, checks first)
- Registration failure is non-fatal (logged as warning, service continues)
- Default delay: 30 seconds after login (configurable in Settings)
- Browser open on startup is optional (configurable in Settings)
- Manual control: `rag service install` / `rag service uninstall` CLI commands

---

## 9. Settings and Configuration

**Purpose:** How settings are managed and applied.

```mermaid
flowchart TD
    A[Settings Page] --> B[Load current values from /api/config]
    B --> C[User edits fields]
    C --> D[Click Save Settings]
    D --> E{What changed?}

    E -->|Indexing/Retrieval| F[Hot-reload: applied immediately]
    E -->|Port/Log level| G[Saved but requires service restart]
    E -->|Startup options| H[Saved to TOML startup section]

    F --> I[QdrantOwner settings updated in memory]
    G --> J[Badge: Restart required]
    H --> K[Applied on next service start]

    L[Danger Zone: Rebuild] --> M[Confirm dialog]
    M --> N[Drop all Qdrant data]
    N --> O[Full re-index from all projects]
```

**Settings available in UI:**

| Section | Fields | Behavior |
|---------|--------|----------|
| Indexing | Chunk size, Chunk overlap | Hot-reload |
| Retrieval | Top K, Score threshold | Hot-reload |
| Service & Startup | Port, Log level | Restart required |
| Service & Startup | Open browser, Startup delay | Applied on next start |
| Danger Zone | Rebuild Knowledge Base | Destructive (confirm dialog) |

**Config sources (priority order):**
1. Environment variables (`RAG_*`)
2. TOML config file (`ragtools.toml`)
3. Built-in defaults

---

## 10. Error Recovery

**Purpose:** Diagnose and fix common issues.

```mermaid
flowchart TD
    A[Problem detected] --> B{What kind?}

    B -->|Service won't start| C[Check: rag doctor]
    C --> C1{Qdrant lock?}
    C1 -->|Yes| C2[Kill stale Python processes]
    C1 -->|No| C3[Check logs: data/logs/service.log]

    B -->|No search results| D{Projects indexed?}
    D -->|No| D1[Add project on Projects page]
    D -->|Yes| D2[Check score threshold in Settings]

    B -->|Corrupt index| E[Settings - Danger Zone - Rebuild]
    E --> E1[Drops all data and re-indexes]

    B -->|Config issues| F[Check ragtools.toml]
    F --> F1[Verify project paths exist]
    F --> F2[Verify TOML syntax]
```

**Diagnostic commands:**
```
rag doctor          # System health check
rag status          # Index statistics
rag service status  # Service running?
rag project list    # Configured projects
```

---

## 11. Release and Distribution

**Purpose:** How a new version gets from code to user.

```mermaid
flowchart TD
    A[Update version in 3 files] --> B[Run tests: pytest]
    B --> C[Local build verify]
    C --> D[Commit + Tag]
    D --> E[Push to GitHub]
    E --> F[GitHub Actions triggered]

    F --> G[Build PyInstaller bundle]
    G --> H[Build Inno Setup installer]
    H --> I[Create GitHub Release]
    I --> J[Upload installer + portable zip]

    J --> K[Compute SHA256 of installer]
    K --> L[Update winget manifest]
    L --> M[Submit PR to microsoft/winget-pkgs]
```

**Version locations:**
- `pyproject.toml` -> `version = "X.Y.Z"`
- `src/ragtools/__init__.py` -> `__version__ = "X.Y.Z"`
- `installer.iss` -> `#define MyAppVersion "X.Y.Z"`
