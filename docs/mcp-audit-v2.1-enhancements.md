# MCP RAG Tools — v2.1 Enhancement Plan

> Based on the MCP audit conducted on 2026-04-09.
> These are targeted improvements to token efficiency, retrieval quality, and configuration hygiene.
>
> **Core principle:** Keep `top_k=10` for full coverage. Fix the **output size per result** instead of reducing result count. The RAG exists to save Claude from reading all files — the output must be lean, not the coverage.

---

## High Priority

### 1. Truncate long chunks in search results

**Problem:** Raw Markdown tables in results consume 300-800 tokens per chunk. A single table with 12 rows and 7 columns is ~500 tokens even though only 1-2 rows may be relevant. With `top_k=10`, a single search call can return 4,000-5,000 tokens of raw text.

**Fix:** In `src/ragtools/retrieval/formatter.py`, truncate chunk text:
```python
MAX_CHUNK_CHARS = 600  # ~150 tokens

def _truncate(text: str, max_chars: int = 600) -> str:
    if len(text) <= max_chars:
        return text
    # Cut at last sentence boundary within limit
    cut = text[:max_chars].rfind('. ')
    if cut > max_chars // 2:
        return text[:cut + 1] + " [...]"
    return text[:max_chars] + " [...]"
```

Apply in the `format_context()` function before building the output string.

**Impact:** ~60% token reduction per search. A 500-token table becomes ~150 tokens with the key information preserved.

**Why this matters:** The RAG concept saves Claude from reading 80,000+ tokens of raw files. But if each search returns 4,000 tokens of raw Markdown, the savings are undermined. Truncation keeps coverage high (10 results) while making each result lean.

**File:** `src/ragtools/retrieval/formatter.py`

---

### 2. Deduplicate results from v1/v2 document versions

**Problem:** When both `proposal.md` and `proposal_v2.md` exist, search returns near-identical chunks from both. The team allocation table appears 3 times in results with slightly different numbers. This wastes tokens on duplicate information and confuses Claude about which version is current.

**Fix:** In the formatter or searcher, group results by heading section. If two results share the same section heading pattern (ignoring version suffixes), keep only the higher-scoring one.

```python
import re

def _deduplicate(results: list) -> list:
    seen = {}
    for r in results:
        # Normalize: strip version suffixes like _v2, _v3
        key = re.sub(r'_v\d+', '', r.file_path) + "::" + (r.headings[0] if r.headings else "")
        if key not in seen or r.score > seen[key].score:
            seen[key] = r
    return sorted(seen.values(), key=lambda r: r.score, reverse=True)
```

**Impact:** ~30% fewer redundant results. Claude gets the best version of each piece of information, not three versions of the same table.

**File:** `src/ragtools/retrieval/searcher.py` or `formatter.py`

---

### 3. Compact output format for MCP

**Problem:** The current formatted output was designed for the admin UI search page where users want full detail. MCP calls from Claude need lean output to conserve context window.

**Current format per result (~200 tokens):**
```
[RAG CONTEXT — LOW CONFIDENCE] Retrieved 10 chunks for query: 'team allocation'
Top score: 0.476. Use retrieved content as source of truth for project-specific facts.
---
[1] Source: alaqraboon_planning/alaqraboon_planning/technical_proposal_v2.md | Section: # Technical and Financial Proposal — Al-Aqraboon Digital Transformation > ## Team & Resources | Score: 0.476 (LOW)
| # | Role | Phases | Duration |
| --- | --- | --- | --- |
| 1 | Project Manager | P1-P4 | 4m |
... (full table)
```

**Target format per result (~60 tokens):**
```
[1] technical_proposal_v2.md > Team & Resources (0.48): PM P1-P4 4m, SA/DevOps P1-P3 3.5m, 2x Full-Stack P1-P4 4m, Mobile P2-P4 3m, AI/ML P2-P3 2m, UI/UX P1-P2 2m, QA P2-P4 3m [...]
```

**Fix:** Add a compact formatter used by default in MCP, full formatter used by admin UI:
- One-line source + heading + score header
- Truncated content (600 chars max)
- No `[RAG CONTEXT — X CONFIDENCE]` wrapper (the scores speak for themselves)
- Confidence header only if top score < 0.3

**Impact:** ~70% token reduction per search call when combined with truncation.

**File:** `src/ragtools/retrieval/formatter.py`, `src/ragtools/integration/mcp_server.py`

---

### 4. Fix consumer project MCP configs

**Problem:** Consumer projects (like Alaqraboon) have hardcoded Python paths and dev scripts in three separate config files. Not portable.

**Fix for each consumer project:**
- Use single `.mcp.json` with generic command:
  ```json
  {
    "mcpServers": {
      "ragtools": {
        "command": "rag-mcp",
        "args": []
      }
    }
  }
  ```
- Remove MCP entries from `.claude/settings.json` and `.claude/settings.local.json`

**Impact:** Portable config, works on any machine with ragtools installed.

---

### 5. Add RAG usage instruction to consumer project CLAUDE.md files

**Problem:** The RAG search-first instruction lives only in the RAG tool's own CLAUDE.md. Consumer projects have no instruction telling Claude to use the knowledge base.

**Fix:** Add to each consumer project's CLAUDE.md:
```markdown
## RAG Knowledge Base

Before answering project-specific questions, search the local knowledge base first.

- Use `search_knowledge_base(query)` for project facts
- Use `list_projects()` to see available project IDs
- If results show LOW CONFIDENCE, note this in your answer
- Cite sources: [Source: project/file | Section: heading]
```

**Impact:** Claude actually uses the RAG tools when working in consumer projects.

---

## Medium Priority

### 6. Evaluate better embedding model

**Problem:** `all-MiniLM-L6-v2` (384 dims) scores consistently LOW (0.4-0.5) on structured planning documents with tables, acronyms, and domain terms (QC, AQ, SPOC, PM).

**Candidates:**
- `bge-base-en-v1.5` (768 dims) — better on structured content
- `e5-base-v2` (768 dims) — strong on query-document matching
- `all-mpnet-base-v2` (768 dims) — same family, higher quality

**Tradeoff:** Larger model = slower first load (~15s vs ~5s), larger Qdrant storage, requires full rebuild.

**Evaluation approach:** Run the eval harness (`scripts/eval_retrieval.py`) with test questions against each model and compare scores.

**File:** `src/ragtools/config.py` (embedding_model default), requires full rebuild

---

### 7. Add query-result caching

**Problem:** Same query run twice does full embedding + Qdrant search both times. In a conversation where Claude refines a search, this wastes compute.

**Fix:** Simple LRU cache on the search function:
```python
from functools import lru_cache

@lru_cache(maxsize=32)
def _cached_search(query_hash: str, project: str, top_k: int) -> list:
    ...
```

Cache key: `sha256(query + project + top_k)`. Invalidate on index changes.

**Impact:** Instant repeat queries, saves ~100ms per cached hit.

**File:** `src/ragtools/retrieval/searcher.py`

---

### 8. Optimize list_projects in direct mode

**Problem:** Direct mode `list_projects()` scrolls the entire Qdrant collection to extract unique `project_id` values. This is O(n) where n = total points.

**Fix:** Read project list from the SQLite state DB instead:
```python
state = IndexState(settings.state_db)
projects = state.get_project_summary()  # GROUP BY project_id
```

**Impact:** O(1) instead of O(n), matters for large collections.

**File:** `src/ragtools/integration/mcp_server.py`, `src/ragtools/indexing/state.py`

---

## Token Budget Analysis

**The RAG value proposition:**

| Approach | Tokens per question | Coverage |
|----------|-------------------|----------|
| Claude reads all project files | 80,000+ | Complete but slow |
| RAG current (top_k=10, raw output) | 4,000 | Good but wasteful |
| RAG v2.1 (top_k=10, truncated + deduped + compact) | 800 | Same coverage, 80% leaner |

**Per-improvement impact (keeping top_k=10):**

| Improvement | Tokens per search | Savings vs current | Effort |
|-------------|------------------|--------------------|--------|
| Current (no changes) | ~4,000 | — | — |
| + Truncation only | ~1,600 | 60% | Small function |
| + Deduplication | ~1,100 | 72% | Medium logic |
| + Compact format | ~800 | 80% | Format refactor |
| **All three combined** | **~800** | **80%** | **1-2 sessions** |

**Key insight:** Don't reduce coverage (top_k). Reduce **noise per result**. 10 lean results are better than 5 fat ones.

---

## Known Bugs (Fixed)

### PyInstaller frozen exe: service fails to start (v2.0.0)

**Severity:** Critical — entire admin panel and service non-functional after installation.

**Symptom:** `rag service start` reports a PID but the service crashes immediately. Port 21420 never opens. Service log shows:
```
Usage: rag.exe [OPTIONS] COMMAND [ARGS]...
Error: No such option: -m
```

**Root cause:** `src/ragtools/service/process.py` (line 69-73) spawns the service subprocess using:
```python
cmd = [sys.executable, "-m", "ragtools.service.run", "--host", ..., "--port", ...]
```

When running from source, `sys.executable` is the Python interpreter and `-m` works. When running from the PyInstaller-bundled `rag.exe`, `sys.executable` resolves to `rag.exe` — a Typer CLI app that doesn't understand the `-m` flag.

**Fix applied:** Added frozen-exe detection using `sys.frozen` (standard PyInstaller attribute):
```python
if getattr(sys, "frozen", False):
    # Frozen exe: use CLI subcommand
    cmd = [sys.executable, "service", "run", "--host", ..., "--port", ...]
else:
    # Source/dev: use Python module
    cmd = [sys.executable, "-m", "ragtools.service.run", "--host", ..., "--port", ...]
```

**Scope:** Affects all users running the PyInstaller-bundled installer (v2.0.0). Fixed in source, requires exe rebuild and new installer for distribution.

**Verification:**
```
$ rag service start
Service started (PID 24108)

$ curl http://127.0.0.1:21420/health
{"status":"ready","collection":"markdown_kb"}
```

**File:** `src/ragtools/service/process.py`
