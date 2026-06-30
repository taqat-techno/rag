# Markdown for RAG — authoring standard

| | |
|---|---|
| **Owner** | TBD (proposed: docs lead) |
| **Last validated against version** | 2.5.2 |
| **Last reviewed** | 2026-04-19 |
| **Status** | Active |

This page defines how to write Markdown that the ragtools chunker will turn into high-value chunks. Every rule below is grounded in the actual implementation; file:line references point to the code that enforces the behaviour.

If you're an agent writing `.md` from inside Claude Code, the [`rag-plugin` v0.7.0+ `markdown-authoring` skill](#how-the-plugin-automates-this) auto-loads this standard for you. Humans writing by hand follow the same rules and run `/rag:md-rag-enhance` before committing.

---

## Why this matters (one paragraph)

The ragtools chunker splits files at `#`…`######` headings, prepends the full heading chain to every chunk before embedding, and stores `raw_text` (without the heading prefix) in the Qdrant payload (`src/ragtools/chunking/markdown.py:43-80, 255-258`). The embedder is `all-MiniLM-L6-v2` with a **256-token** input window (`src/ragtools/embedding/encoder.py`). The MCP result formatter only shows the **deepest** heading as `project/file.md > Heading` (`src/ragtools/retrieval/formatter.py:78-83`). These three facts dictate every rule below.

---

## The 8 hard rules

1. **Never put knowledge before the first heading.** Content before the first `#`/`##` becomes an anchor-less chunk with an empty heading chain. Always open with `# Title` (or `## Title` if the file is a subsection of a larger doc). (`markdown.py:110-113`)
2. **Target 150–250 words per leaf section.** The whole section becomes one chunk and fits comfortably inside the 256-token embedding window.
3. **Hard cap: ~300 words per `###` section.** Past that, the paragraph splitter fires and the tail becomes a chunk that shares the heading with the head but covers an unrelated topic — classic mixed-topic noise. (`markdown.py:148-196`)
4. **Separate every paragraph with a blank line.** The splitter falls back to sentence-level chunking when there are no `\n\n` boundaries; sentence-split chunks are the weakest kind. (`markdown.py:199-241`)
5. **Headings are keyword-rich and specific.** `## Configuring the chunk overlap` beats `## Configuration`. The heading is **prepended to every chunk's embedding** — it's the biggest single lever you have. (`markdown.py:255-258`)
6. **One idea per heading.** If a section covers two topics, split it into two sibling headings. Mixed-topic chunks are the number-one retrieval killer.
7. **Leaf headings must be unique within the file.** The MCP formatter shows only the deepest heading; two sections both called `### Usage` appear identically in search results. (`formatter.py:78-83`)
8. **Do not rely on YAML frontmatter for retrieval.** Frontmatter is stripped by `extract_frontmatter` and never embedded or stored. Put tags/keywords in the body. (`src/ragtools/chunking/metadata.py:9-21`)

---

## Soft rules (strongly recommended)

- Code blocks under ~60 lines. Longer → split into `### Step 1 — install`, `### Step 2 — configure`.
- Tables under ~15 rows. Beyond that, split into multiple tables by category or move to a dedicated reference page.
- Introduce every code block in prose. `The following command stops the service:` before the fence carries the semantic signal the code alone can't.
- Prefer one prose sentence at the start of a section before falling into bullets.
- No pseudo-headings (bold text used as a section title). They don't match the heading regex and don't create chunk boundaries.

---

## Five page templates

Every template produces 3–6 clean heading-anchored chunks in the 150–250-word sweet spot.

### Concept page

```markdown
# <Concept name>

## What it is
<1 paragraph, 80–120 words. Ends with a definition sentence.>

## Why it exists
<1 paragraph. Problem it solves.>

## Key rules
<bulleted invariants, one complete sentence each>

## Related concepts
<links to sibling pages>
```

### SOP page

```markdown
# SOP: <action>

## Purpose
<1 paragraph>

## Preconditions
<bulleted checklist>

## Step 1 — <verb + noun>
<prose + code block if needed>

## Step 2 — <verb + noun>
<...>

## Validation
<observable success criteria>

## Failure modes
<table linking to runbooks>
```

### Reference page

```markdown
# <Area> reference

## <Table 1 — ~10 rows>
| Field | Type | Default | Notes |

## <Table 2 — ~10 rows>
| Field | Type | Default | Notes |
```

### Runbook page

```markdown
# Runbook: <symptom>

## Symptom
<user-visible evidence>

## Likely causes
<bulleted>

## Diagnosis
### Check <X>
### Check <Y>

## Recovery
### If <cause A>
### If <cause B>

## Prevention
<paragraph>
```

### Architecture page

```markdown
# <Component> architecture

## What it is
## How it fits in the system
## Key decisions
### Decision — <name>
### Decision — <name>
## Failure modes
## Code paths
```

---

## Anti-patterns (what the chunker punishes)

| Anti-pattern | Why it hurts |
|---|---|
| Long intro before any heading | Anchor-less chunk; empty heading chain in search results. |
| One giant `##` covering many topics | Paragraph splitter creates chunks that share a heading but cover different ideas. |
| Vague headings (`Overview`, `Notes`, `Details`) | Dilutes the primary embedding signal prepended to every chunk. |
| 100-line code blocks with no surrounding prose | Counts as a single paragraph; when oversize, the sentence splitter mangles it. |
| 20-row tables | Same as above — single paragraph; splits strand rows across chunks. |
| Semantic info in YAML frontmatter | Stripped, never searchable. |
| Identical leaf headings across files (`### Usage` everywhere) | MCP output gives agents no way to distinguish sources. |
| Duplicate file versions without first-heading differentiation | `formatter.py` dedup drops one silently. |
| Pseudo-headings (bold instead of `##`) | Regex doesn't match; no chunk boundary. |

---

## Pre-commit checklist

Run through this before committing Markdown under any indexed project. The `/rag:md-rag-enhance` command automates the first two; the rest need human judgment.

- [ ] File opens with a `#` heading — no knowledge in the intro
- [ ] Every `##`/`###` section ≤ 300 words
- [ ] Leaf headings unique within the file
- [ ] No knowledge in YAML frontmatter
- [ ] Code blocks ≤ 60 lines
- [ ] Tables ≤ 15 rows
- [ ] Every code block has a prose intro
- [ ] Headings are specific (not `Overview` / `Notes`)
- [ ] Blank lines around headings and code fences

---

## How the plugin automates this

The [rag-plugin](Development-SOPs-Plugins-Add-a-New-Plugin) v0.7.0+ ships two layers of automation for this standard:

### `markdown-authoring` skill — prevents bad Markdown at source

Auto-activates whenever Claude Code is asked to create a `.md` file (README, runbook, SOP, architecture page, reference, concept page). The skill loads:

- `references/rag-md-guidelines.md` — the full 359-line standard this page summarises
- `references/page-templates.md` — the 5 templates above as copy-paste scaffolds
- `references/examples.md` — before/after examples

Generated Markdown is chunk-optimal from line 1. No rewrite needed.

### `/rag:md-rag-enhance` command — safely improves existing Markdown

Enhances Markdown already on disk. Three invocations:

| Command | Scope |
|---|---|
| `/rag:md-rag-enhance` | Every `.md` in the current project |
| `/rag:md-rag-enhance path/to/file.md` | One file |
| `/rag:md-rag-enhance --verbose` | Full per-file report |
| `/rag:md-rag-enhance --no-backup` | Skip the `.bak-pre-md-rag-enhance` sibling (for git-clean working trees) |

**Auto-fixes (100% safe, applied in place):**
- Bold-as-heading (`**Heading**` alone on a line) → real `## Heading`
- Missing blank lines around headings and code fences

**Reports only — never auto-applies:**
- Content before first heading
- Oversized sections (>300 words)
- Vague headings
- Duplicate leaf headings
- Big code blocks (>60 lines)
- Big tables (>15 rows)
- Knowledge in YAML frontmatter
- Code blocks without prose intro
- Skipped heading levels (`##` → `####`)

**Safety invariants:**
- Never touches code-fence interiors (commands, URLs, paths, versions, numbers are untouchable)
- Atomic writes with `.bak-pre-md-rag-enhance` backup (opt-out with `--no-backup`)

### Install / update

```
/plugins → update taqat-techno-plugins → restart Claude Code
```

Plugin docs: https://github.com/taqat-techno/plugins/wiki/Rag-Plugin

---

## Cross-file unity

Three rules that make many files feel like one knowledge base:

1. **Reserved section vocabulary.** Pick a small set — `## Purpose`, `## Scope`, `## Preconditions`, `## Steps`, `## Validation`, `## Failure modes`, `## Related`, `## Change log` — and reuse them. Predictable section names make cross-file retrieval predictable.
2. **One topic per file.** If a file has five unrelated `##` sections, split into five files.
3. **Keyword hygiene.** Use the same term for the same concept across files. Maintain a project-local `glossary.md`; link instead of redefining.

See also: [Documentation Standards](Standards-and-Governance-Documentation-Standards) for ownership, metadata tables, and review cadence.

---

## Related

- [Add a New Plugin](Development-SOPs-Plugins-Add-a-New-Plugin) — rag-plugin v0.7.0 capability list
- [Documentation Standards](Standards-and-Governance-Documentation-Standards) — page metadata, ownership, review workflow
- [Indexing Pipeline](Architecture-Indexing-Pipeline) — the end-to-end code path this standard is derived from
- Authoritative source: `src/ragtools/chunking/markdown.py`

## Change log

| Date | Version | Change |
|---|---|---|
| 2026-04-19 | 2.5.2 | Initial page. Documents the 8 hard rules, 5 page templates, anti-patterns, and the `rag-plugin` v0.7.0 automation layer. |
