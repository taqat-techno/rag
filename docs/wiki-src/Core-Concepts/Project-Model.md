# Project Model

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |

## The rule

**One Qdrant collection. Many projects. Isolation via payload filter.**

## How projects are discovered

Each **immediate subdirectory** of the content root becomes a project. Its directory name is the `project_id`.

- Content root resolves from `RAG_CONTENT_ROOT` (default `.`).
- Directory names starting with `.` or `_` are skipped.
- Nested directories below the first level are part of the containing project, not separate projects.

Example layout:

```
content_root/
  docs-internal/        <- project_id = "docs-internal"
    guides/             <- part of docs-internal
    runbooks/           <- part of docs-internal
  notes/                <- project_id = "notes"
  .archive/             <- skipped
  _drafts/              <- skipped
```

## How isolation works

Every chunk is upserted as a Qdrant point whose payload carries:

- `project_id` — indexed, keyword type. The project this chunk belongs to.
- `file_path` — relative path from the project root.
- `headings` — heading hierarchy for the chunk.
- `text` — the raw chunk text.
- `chunk_index` — position within the file.

Search applies a payload filter on `project_id` when a project is specified:

- `rag search "query"` — searches across all projects.
- `rag search "query" -p docs-internal` — filters to `docs-internal` only.
- `search_knowledge_base(query=..., project="docs-internal")` — same, via MCP.

## Why not one collection per project?

Locked in [Architecture Decisions](Standards-and-Governance-Architecture-Decisions):

- A single collection keeps cross-project search trivial — no query fan-out.
- Payload filtering on an indexed keyword field is fast.
- Qdrant's per-collection overhead (schema, WAL, segments) multiplies with project count; one collection avoids that.

## Related

- [Architecture Decisions](Standards-and-Governance-Architecture-Decisions).
- [Configuration Keys](Reference-Configuration-Keys) — `RAG_CONTENT_ROOT`.
- [Add a Project](Operational-SOPs-Projects-Add-a-Project).

## Code paths

- `src/ragtools/indexing/scanner.py` — `discover_markdown_files`, project enumeration.
- `src/ragtools/retrieval/searcher.py` — `project_id` payload filter.
- `src/ragtools/indexing/indexer.py` — PointStruct payload construction.
