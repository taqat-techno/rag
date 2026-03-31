# Local Markdown RAG System — Implementation Plan

> Staged delivery plan for a local-first, Markdown-only RAG system using Qdrant + Sentence Transformers + Python, integrated with Claude CLI.

---

## 1. Executive Summary

### System Goal

Build a local knowledge base where Claude CLI retrieves project-specific facts from indexed Markdown files before answering, using Qdrant for vector storage and Sentence Transformers for embeddings. Each subdirectory is treated as a separate project. Retrieved content is source of truth for project facts; Claude provides reasoning, explanation, and best practices from its own knowledge.

### Delivery Sequence

| Stage | Name | Focus | Est. Effort |
|-------|------|-------|-------------|
| **0** | Project scaffold | Structure, config, dependencies | 2-3 hours |
| **1** | Markdown chunking | Parse and chunk .md files | 4-6 hours |
| **2** | Embedding + Qdrant indexing | Encode chunks, store in Qdrant | 4-6 hours |
| **3** | Retrieval pipeline | Query → embed → search → rank | 3-4 hours |
| **4** | Claude CLI integration | MCP server or tool hook | 6-8 hours |
| **5** | Incremental indexing | Only re-index changed files | 4-6 hours |
| **6** | CLI and maintenance | index, search, status, doctor, rebuild | 4-6 hours |
| **7** | Validation and tuning | Test retrieval quality, tune parameters | 4-6 hours |

### MVP Boundary

**MVP = Stages 0–4.** A working system where you can index Markdown files, search them, and have Claude use retrieved context. Stages 5-7 are quality-of-life improvements that make the system production-grade for daily use.

---

## 2. Assumptions and Design Rules

### Assumptions

1. Input is `.md` files only (v1)
2. Each immediate subdirectory of the watched root is a "project"
3. Single user, single machine — no auth, no multi-tenancy
4. Qdrant runs locally via Docker (or in-memory for testing)
5. Sentence Transformers runs locally on CPU (GPU optional)
6. Claude CLI is available and supports MCP servers or tool hooks
7. Python 3.10+ is available
8. The workspace root is `c:\MY-WorkSpace\rag\` (this directory)

### Non-Goals for v1

- Cloud deployment
- Multi-user access control
- Non-Markdown file types (.pdf, .docx, .html)
- Web UI
- Automatic embedding model fine-tuning
- Hybrid search (dense + sparse) — defer to v2
- Cross-encoder reranking — defer to v2
- Real-time file watching (watchfiles) — defer to v2

### Design Rules

1. **One Qdrant collection** for all projects. Use `project_id` payload field with `is_tenant=True` for per-project filtering.
2. **One embedding model** for all content. Start with `all-MiniLM-L6-v2` (384 dims, fast, good quality).
3. **Chunk at heading boundaries** when possible, fall back to fixed-size with overlap.
4. **Store chunk text in payload** — enables retrieval without external file reads.
5. **Deterministic chunk IDs** — hash of (project_id + file_path + chunk_index) for idempotent upserts.
6. **File-level change tracking** — store file hash to detect changes for incremental indexing.
7. **Fail loudly** — if retrieval returns no results or low-confidence results, say so explicitly.

---

## 3. Recommended Project Structure

```
rag/
├── pyproject.toml                    # Project config, dependencies, CLI entry points
├── README.md                         # Project documentation
├── .env                              # Local config (Qdrant URL, model name, paths)
│
├── src/
│   └── ragtools/                     # Main Python package
│       ├── __init__.py
│       ├── cli.py                    # CLI entry point (click/typer)
│       ├── config.py                 # Configuration loading (.env, defaults)
│       ├── models.py                 # Data models (Chunk, IndexState, SearchResult)
│       │
│       ├── chunking/
│       │   ├── __init__.py
│       │   ├── markdown.py           # Markdown parsing and chunking logic
│       │   └── metadata.py           # Extract frontmatter, headings, file metadata
│       │
│       ├── embedding/
│       │   ├── __init__.py
│       │   └── encoder.py            # SentenceTransformer wrapper (encode, batch)
│       │
│       ├── indexing/
│       │   ├── __init__.py
│       │   ├── indexer.py            # Full and incremental indexing orchestration
│       │   ├── state.py              # File hash tracking (SQLite or JSON)
│       │   └── scanner.py            # Walk directories, discover .md files, detect projects
│       │
│       ├── retrieval/
│       │   ├── __init__.py
│       │   ├── searcher.py           # Query embedding + Qdrant search + result formatting
│       │   └── formatter.py          # Format search results for Claude context injection
│       │
│       └── integration/
│           ├── __init__.py
│           └── mcp_server.py         # MCP server for Claude CLI integration
│
├── tests/
│   ├── test_chunking.py
│   ├── test_indexing.py
│   ├── test_retrieval.py
│   └── fixtures/                     # Sample .md files for testing
│       ├── project_a/
│       │   ├── README.md
│       │   └── guide.md
│       └── project_b/
│           └── notes.md
│
├── scripts/
│   └── eval_retrieval.py             # Retrieval quality evaluation script
│
└── data/
    └── index_state.json              # Persisted file hashes for incremental indexing
```

### Module Responsibilities

| Module | Responsibility |
|--------|---------------|
| `cli.py` | Parse CLI commands (index, search, status, doctor, rebuild), dispatch to appropriate modules |
| `config.py` | Load `.env`, provide defaults for Qdrant URL, model name, chunk size, collection name |
| `models.py` | Pydantic models for `Chunk`, `FileRecord`, `SearchResult`, `IndexState` |
| `chunking/markdown.py` | Parse Markdown, split into chunks at heading boundaries with overlap |
| `chunking/metadata.py` | Extract YAML frontmatter, heading hierarchy, file path metadata |
| `embedding/encoder.py` | Thin wrapper around SentenceTransformer — encode single/batch, normalize |
| `indexing/indexer.py` | Orchestrate: scan → chunk → embed → upsert to Qdrant. Full and incremental modes |
| `indexing/state.py` | Track file hashes (SHA256) to detect changes. Persist to `data/index_state.json` |
| `indexing/scanner.py` | Walk directories, discover `.md` files, map files to projects |
| `retrieval/searcher.py` | Encode query → Qdrant search with optional project filter → return ranked results |
| `retrieval/formatter.py` | Format search results into a context block for Claude (with source citations) |
| `integration/mcp_server.py` | MCP server that exposes `search_knowledge_base` tool to Claude CLI |

---

## 4. Delivery Stages

---

### Stage 0: Project Scaffold

**Objective:** Set up the project skeleton, dependencies, and local development environment.

**Why it matters:** A clean scaffold prevents restructuring later. Getting Qdrant running early eliminates infra surprises.

**Deliverables:**
- [ ] `pyproject.toml` with all dependencies
- [ ] Project directory structure as specified above
- [ ] Docker Compose file for local Qdrant
- [ ] `.env` with defaults
- [ ] `config.py` loading config
- [ ] Qdrant health check works

**Recommended Tools and Libraries:**

| Library | Purpose | Install |
|---------|---------|---------|
| `qdrant-client` | Qdrant Python SDK | `pip install qdrant-client` |
| `sentence-transformers` | Embedding model | `pip install sentence-transformers` |
| `typer` | CLI framework | `pip install typer[all]` |
| `pydantic` | Data models | `pip install pydantic` |
| `pydantic-settings` | Config from .env | `pip install pydantic-settings` |
| `python-frontmatter` | YAML frontmatter parsing | `pip install python-frontmatter` |
| `mistune` or `markdown-it-py` | Markdown parsing | `pip install markdown-it-py` |
| `rich` | Pretty CLI output | `pip install rich` |
| `pytest` | Testing | `pip install pytest` |

**Docker Compose for Qdrant:**

```yaml
# docker-compose.yml
version: '3.8'
services:
  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - "6333:6333"
      - "6334:6334"
    volumes:
      - qdrant_data:/qdrant/storage
    restart: unless-stopped

volumes:
  qdrant_data:
```

**Config model:**

```python
# src/ragtools/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    qdrant_url: str = "http://localhost:6333"
    collection_name: str = "markdown_kb"
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384
    chunk_size: int = 400       # tokens
    chunk_overlap: int = 100    # tokens
    content_root: str = "."     # Root directory to scan for projects
    top_k: int = 10             # Default search results
    score_threshold: float = 0.3  # Minimum relevance score

    model_config = {"env_prefix": "RAG_", "env_file": ".env"}
```

**Acceptance Criteria:**
- `docker compose up -d` starts Qdrant; `curl localhost:6333/healthz` returns OK
- `python -c "from ragtools.config import Settings; print(Settings())"` prints config
- `pytest` runs (even if no tests yet)

**Risks:** Docker not installed → document manual Qdrant binary install as fallback. Rust compilation issues with sentence-transformers → use pre-built wheels.

---

### Stage 1: Markdown Chunking

**Objective:** Parse `.md` files into semantically meaningful chunks with metadata.

**Why it matters:** Chunk quality is the single biggest factor in retrieval quality. Bad chunking = bad RAG regardless of everything else.

**Deliverables:**
- [ ] `chunking/markdown.py` — split Markdown into chunks
- [ ] `chunking/metadata.py` — extract frontmatter, headings, file metadata
- [ ] `models.py` — `Chunk` and `FileRecord` data models
- [ ] `indexing/scanner.py` — discover files and projects
- [ ] Unit tests with fixture files

**Data Models:**

```python
# src/ragtools/models.py
from pydantic import BaseModel
from datetime import datetime

class Chunk(BaseModel):
    chunk_id: str              # Deterministic hash: sha256(project_id + file_path + chunk_index)
    project_id: str            # Directory name (e.g., "royal_preps")
    file_path: str             # Relative path from content root
    chunk_index: int           # Position within file
    text: str                  # The chunk content
    heading_hierarchy: list[str]  # ["## Architecture", "### Backend"]
    token_count: int           # Approximate token count
    file_hash: str             # SHA256 of the source file

class FileRecord(BaseModel):
    file_path: str
    project_id: str
    file_hash: str
    chunk_count: int
    last_indexed: datetime

class SearchResult(BaseModel):
    chunk: Chunk
    score: float
    rank: int
```

**Chunking Strategy:**

```python
# src/ragtools/chunking/markdown.py
"""
Chunking strategy:
1. Parse Markdown into an AST (using markdown-it-py)
2. Split at heading boundaries (##, ###, ####)
3. If a heading section exceeds chunk_size tokens, split further at paragraph boundaries
4. If a paragraph exceeds chunk_size, split at sentence boundaries with overlap
5. Preserve heading hierarchy as metadata on each chunk
6. Prepend the heading hierarchy to chunk text for embedding context

Output: list[Chunk]
"""
import hashlib
from ragtools.models import Chunk

def chunk_markdown(
    content: str,
    file_path: str,
    project_id: str,
    file_hash: str,
    chunk_size: int = 400,
    chunk_overlap: int = 100,
) -> list[Chunk]:
    # Implementation here
    ...

def _make_chunk_id(project_id: str, file_path: str, chunk_index: int) -> str:
    raw = f"{project_id}::{file_path}::{chunk_index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

**Why heading-based chunking:**
- Headings are natural semantic boundaries in Markdown
- The heading hierarchy provides context that improves retrieval (e.g., searching "backend architecture" matches chunks under `## Architecture > ### Backend`)
- Preserves document structure better than fixed-size chunking

**Suggested Existing Material:**

| Resource | Why It Helps |
|----------|-------------|
| `langchain_text_splitters.MarkdownHeaderTextSplitter` | Reference implementation for heading-based Markdown splitting. Study its approach but build your own simpler version to avoid the LangChain dependency. |
| `markdown-it-py` library | Battle-tested Markdown parser with AST access. Use it to identify headings, code blocks, and paragraphs reliably instead of regex. |
| `python-frontmatter` library | Parses YAML frontmatter from Markdown files. One function call: `frontmatter.load(file)`. |
| `tiktoken` library | OpenAI's fast tokenizer for estimating token counts. Useful for chunk size enforcement without loading a full model. Alternative: `len(text.split())` as rough word-count proxy. |

**Implementation Notes:**
- Prepend heading hierarchy to each chunk's text before embedding. Example: A chunk under `## API > ### Authentication` gets prepended with `"API > Authentication\n\n"`. This dramatically improves retrieval for queries like "how does authentication work?"
- Store the raw text (without prepended headings) in the payload for display, but embed the enriched version
- Use `tiktoken` or word count as proxy for token estimation — don't load the embedding model just for counting

**Acceptance Criteria:**
- Given a sample `.md` file, produces a list of `Chunk` objects
- Chunks respect heading boundaries
- No chunk exceeds `chunk_size` tokens (with tolerance)
- Heading hierarchy is correctly tracked
- Frontmatter is extracted and available
- Chunk IDs are deterministic (same input → same IDs)
- `scanner.py` discovers all `.md` files and maps to projects correctly

**Defer:** Handling of embedded images, tables-as-structured-data, code block special treatment.

---

### Stage 2: Embedding + Qdrant Indexing

**Objective:** Encode chunks into embeddings and store them in Qdrant with rich metadata.

**Why it matters:** This is the core storage layer. Getting the collection schema and indexing flow right avoids painful re-indexing later.

**Deliverables:**
- [ ] `embedding/encoder.py` — SentenceTransformer wrapper
- [ ] `indexing/indexer.py` — full indexing pipeline
- [ ] Qdrant collection created with correct config
- [ ] All test fixture files indexed successfully

**Encoder Wrapper:**

```python
# src/ragtools/embedding/encoder.py
from sentence_transformers import SentenceTransformer
import numpy as np

class Encoder:
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model = SentenceTransformer(model_name)
        self.dimension = self.model.get_sentence_embedding_dimension()

    def encode(self, texts: list[str], batch_size: int = 64) -> np.ndarray:
        return self.model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=len(texts) > 100,
            convert_to_numpy=True,
        )

    def encode_query(self, query: str) -> np.ndarray:
        return self.model.encode(
            query,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
```

**Collection Setup:**

```python
# In indexing/indexer.py
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PayloadSchemaType
)

def create_collection(client: QdrantClient, name: str, dim: int):
    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )
    # Create payload indexes for filtering
    client.create_payload_index(name, "project_id", PayloadSchemaType.KEYWORD, is_tenant=True)
    client.create_payload_index(name, "file_path", PayloadSchemaType.KEYWORD)
    client.create_payload_index(name, "heading_hierarchy", PayloadSchemaType.KEYWORD)
```

**Indexing Pipeline:**

```python
def index_project(
    client: QdrantClient,
    encoder: Encoder,
    collection: str,
    project_id: str,
    chunks: list[Chunk],
    batch_size: int = 100,
):
    # Prepare texts for embedding (with heading context prepended)
    texts = [_enrich_text(chunk) for chunk in chunks]

    # Encode in batches
    embeddings = encoder.encode(texts, batch_size=batch_size)

    # Build Qdrant points
    points = [
        PointStruct(
            id=chunk.chunk_id,
            vector=embedding.tolist(),
            payload={
                "project_id": chunk.project_id,
                "file_path": chunk.file_path,
                "chunk_index": chunk.chunk_index,
                "text": chunk.text,
                "heading_hierarchy": chunk.heading_hierarchy,
                "token_count": chunk.token_count,
                "file_hash": chunk.file_hash,
            },
        )
        for chunk, embedding in zip(chunks, embeddings)
    ]

    # Upsert in batches
    for i in range(0, len(points), batch_size):
        client.upsert(collection_name=collection, points=points[i:i+batch_size])

def _enrich_text(chunk: Chunk) -> str:
    """Prepend heading hierarchy to chunk text for better embedding context."""
    if chunk.heading_hierarchy:
        prefix = " > ".join(chunk.heading_hierarchy) + "\n\n"
        return prefix + chunk.text
    return chunk.text
```

**Why `is_tenant=True` on `project_id`:**
Qdrant co-locates vectors with the same tenant value in storage, making per-project filtered search significantly faster. Since each query will filter by project, this is a direct performance optimization.

**Acceptance Criteria:**
- Collection exists in Qdrant with correct vector dimension and indexes
- All chunks from fixture files are indexed
- `client.count("markdown_kb")` returns expected count
- Each point has all payload fields populated
- Chunk IDs are consistent across re-indexing (idempotent upserts)

**Defer:** Scalar quantization (enable when >100K chunks), on-disk storage.

---

### Stage 3: Retrieval Pipeline

**Objective:** Build the query → embed → search → format pipeline.

**Why it matters:** This is the runtime path. Latency and result quality directly affect the user experience.

**Deliverables:**
- [ ] `retrieval/searcher.py` — query Qdrant with optional project filter
- [ ] `retrieval/formatter.py` — format results for Claude context injection
- [ ] Testable end-to-end: question in → formatted context out

**Searcher:**

```python
# src/ragtools/retrieval/searcher.py
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from ragtools.embedding.encoder import Encoder
from ragtools.models import SearchResult, Chunk
from ragtools.config import Settings

class Searcher:
    def __init__(self, client: QdrantClient, encoder: Encoder, settings: Settings):
        self.client = client
        self.encoder = encoder
        self.settings = settings

    def search(
        self,
        query: str,
        project_id: str | None = None,
        top_k: int | None = None,
        score_threshold: float | None = None,
    ) -> list[SearchResult]:
        top_k = top_k or self.settings.top_k
        threshold = score_threshold or self.settings.score_threshold

        query_vector = self.encoder.encode_query(query)

        # Build filter
        query_filter = None
        if project_id:
            query_filter = Filter(must=[
                FieldCondition(key="project_id", match=MatchValue(value=project_id))
            ])

        results = self.client.query_points(
            collection_name=self.settings.collection_name,
            query=query_vector.tolist(),
            query_filter=query_filter,
            limit=top_k,
            with_payload=True,
            score_threshold=threshold,
        ).points

        return [
            SearchResult(
                chunk=Chunk(**{k: v for k, v in point.payload.items()}),
                score=point.score,
                rank=i + 1,
            )
            for i, point in enumerate(results)
        ]
```

**Formatter:**

```python
# src/ragtools/retrieval/formatter.py

def format_context(results: list[SearchResult], query: str) -> str:
    """Format search results into a context block for Claude."""
    if not results:
        return (
            f"[RAG NOTICE] No relevant local content found for: '{query}'. "
            "Answer based on general knowledge only, and note that no project-specific "
            "information was available."
        )

    max_score = results[0].score if results else 0
    if max_score < 0.5:
        confidence = "LOW CONFIDENCE"
    elif max_score < 0.7:
        confidence = "MODERATE CONFIDENCE"
    else:
        confidence = "HIGH CONFIDENCE"

    header = (
        f"[RAG CONTEXT — {confidence}] Retrieved {len(results)} chunks "
        f"for query: '{query}'\n"
        f"Top score: {max_score:.3f}. Use this as source of truth for project-specific facts.\n"
        "---\n"
    )

    chunks_text = []
    for r in results:
        source = f"[Source: {r.chunk.project_id}/{r.chunk.file_path} "
        source += f"| Section: {' > '.join(r.chunk.heading_hierarchy) if r.chunk.heading_hierarchy else 'N/A'} "
        source += f"| Score: {r.score:.3f}]"
        chunks_text.append(f"{source}\n{r.chunk.text}")

    return header + "\n\n---\n\n".join(chunks_text)
```

**Why explicit confidence levels:**
When retrieval scores are low, the system must tell Claude. Otherwise, Claude may treat irrelevant context as authoritative. The confidence label is a prompt-engineering safeguard.

**Acceptance Criteria:**
- `searcher.search("backend architecture", project_id="royal_preps")` returns relevant chunks
- Results are sorted by score descending
- Project filter correctly limits results to one project
- `format_context()` produces a clearly structured context block
- Empty results produce a "no content found" notice
- Low-score results produce a "LOW CONFIDENCE" label

**Defer:** Cross-encoder reranking, result deduplication across overlapping chunks.

---

### Stage 4: Claude CLI Integration

**Objective:** Connect the retrieval pipeline to Claude CLI so retrieval happens automatically before answer generation.

**Why it matters:** This is the user-facing integration. Without it, the system is a standalone search tool, not a RAG system.

**Deliverables:**
- [ ] MCP server that exposes a `search_knowledge_base` tool
- [ ] Claude CLI configuration to use the MCP server
- [ ] End-to-end test: ask Claude a project question → sees retrieved context

**Integration Approach: MCP Server**

The recommended approach is an MCP (Model Context Protocol) server. Claude CLI natively supports MCP servers, which expose tools that Claude can call during conversation.

```python
# src/ragtools/integration/mcp_server.py
"""
MCP server that exposes a search_knowledge_base tool.
Claude CLI calls this tool when it needs project-specific information.

Run: python -m ragtools.integration.mcp_server
Configure in Claude CLI: claude mcp add ragtools -- python -m ragtools.integration.mcp_server
"""
import json
import sys
from ragtools.config import Settings
from ragtools.retrieval.searcher import Searcher
from ragtools.embedding.encoder import Encoder
from qdrant_client import QdrantClient

# MCP server implementation using stdio transport
# Tool: search_knowledge_base
#   Parameters:
#     - query (string, required): The search query
#     - project (string, optional): Filter to a specific project
#     - top_k (integer, optional): Number of results (default 10)
#   Returns: Formatted context string with retrieved chunks
```

**Claude CLI Configuration:**

```bash
# Add the MCP server
claude mcp add ragtools -- python -m ragtools.integration.mcp_server

# Or in .claude/settings.json
{
  "mcpServers": {
    "ragtools": {
      "command": "python",
      "args": ["-m", "ragtools.integration.mcp_server"],
      "cwd": "c:\\MY-WorkSpace\\rag"
    }
  }
}
```

**CLAUDE.md Integration (alternative/complementary):**

Add instructions to the project's `CLAUDE.md`:

```markdown
## RAG Knowledge Base

Before answering project-specific questions, use the `search_knowledge_base` tool
to retrieve relevant context from the local knowledge base.

- For project-specific facts, always search first
- If retrieval returns LOW CONFIDENCE, say so and note that the answer is based on general knowledge
- Cite sources from retrieved chunks using [Source: project/file | Section: heading]
- If the user asks about a specific project, always pass the project parameter
```

**Suggested Existing Materials:**

| Resource | Why It Helps |
|----------|-------------|
| `@anthropics/mcp-python-sdk` (Python MCP SDK) | Official Python SDK for building MCP servers. Provides stdio transport, tool registration, and schema validation. |
| Claude Code MCP documentation | Explains how to configure MCP servers in Claude CLI settings. |
| `mcp-to-ai-sdk` pattern | Reference for how MCP tools are exposed to AI models. |

**Acceptance Criteria:**
- MCP server starts without errors
- Claude CLI connects to the MCP server
- Asking Claude a project question triggers `search_knowledge_base`
- Claude receives and uses retrieved context in its answer
- Low-confidence retrieval produces an explicit caveat in Claude's response

**Risks:**
- MCP SDK API may differ from documented — check latest `mcp` package on PyPI
- Claude CLI MCP configuration may need absolute paths
- The MCP server must stay running for the duration of the Claude session

**Defer:** Automatic project detection from current working directory, conversation-aware multi-turn retrieval.

---

### Stage 5: Incremental Indexing

**Objective:** Only re-index files that have changed since the last indexing run.

**Why it matters:** Full re-indexing is slow for large knowledge bases. Incremental indexing makes the system practical for daily use.

**Deliverables:**
- [ ] `indexing/state.py` — file hash tracking
- [ ] `indexing/indexer.py` updated with incremental mode
- [ ] `data/index_state.json` persistence

**State Tracking:**

```python
# src/ragtools/indexing/state.py
import hashlib
import json
from pathlib import Path
from ragtools.models import FileRecord

class IndexState:
    def __init__(self, state_file: str = "data/index_state.json"):
        self.state_file = Path(state_file)
        self.records: dict[str, FileRecord] = {}
        self._load()

    def _load(self):
        if self.state_file.exists():
            data = json.loads(self.state_file.read_text())
            self.records = {k: FileRecord(**v) for k, v in data.items()}

    def save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.model_dump(mode="json") for k, v in self.records.items()}
        self.state_file.write_text(json.dumps(data, indent=2))

    def file_changed(self, file_path: str, current_hash: str) -> bool:
        record = self.records.get(file_path)
        return record is None or record.file_hash != current_hash

    def update(self, record: FileRecord):
        self.records[record.file_path] = record

    def remove(self, file_path: str):
        self.records.pop(file_path, None)

    @staticmethod
    def hash_file(file_path: Path) -> str:
        return hashlib.sha256(file_path.read_bytes()).hexdigest()
```

**Incremental Indexing Logic:**

```
1. Scan all .md files in content root
2. For each file:
   a. Compute SHA256 hash
   b. If hash matches stored state → skip
   c. If hash differs or file is new → re-chunk, re-embed, upsert
   d. Update state record
3. For files in state but not on disk → delete chunks from Qdrant, remove from state
4. Save state
```

**Acceptance Criteria:**
- First run indexes everything
- Second run (no changes) indexes nothing
- Editing one file re-indexes only that file
- Deleting a file removes its chunks from Qdrant
- Adding a new file indexes it
- State persists across runs

---

### Stage 6: CLI and Maintenance

**Objective:** Build a user-friendly CLI for all operations.

**Why it matters:** The CLI is how you interact with the system outside of Claude. It must be intuitive and informative.

**Deliverables:**
- [ ] `cli.py` with all commands
- [ ] Pretty output with Rich

**CLI Commands:**

```python
# src/ragtools/cli.py
import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(name="rag", help="Local Markdown RAG tools")
console = Console()

@app.command()
def index(
    path: str = typer.Argument(".", help="Root directory to scan"),
    full: bool = typer.Option(False, "--full", help="Force full re-index"),
    project: str = typer.Option(None, "--project", help="Index only this project"),
):
    """Index or re-index Markdown files."""
    ...

@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    project: str = typer.Option(None, "--project", help="Filter to project"),
    top_k: int = typer.Option(10, "--top-k", help="Number of results"),
):
    """Search the knowledge base."""
    ...

@app.command()
def status():
    """Show indexing status: collection stats, per-project counts, last indexed time."""
    ...

@app.command()
def doctor():
    """Check system health: Qdrant connection, model loaded, index integrity."""
    ...

@app.command()
def rebuild():
    """Drop collection and re-index everything from scratch."""
    ...

@app.command()
def projects():
    """List all indexed projects with chunk counts."""
    ...
```

**Entry point in `pyproject.toml`:**

```toml
[project.scripts]
rag = "ragtools.cli:app"
```

**Acceptance Criteria:**
- `rag index .` indexes all Markdown files
- `rag search "backend architecture" --project royal_preps` returns results
- `rag status` shows collection stats
- `rag doctor` verifies Qdrant connection, model availability, collection existence
- `rag rebuild` drops and recreates the collection
- `rag projects` lists all projects with chunk counts

---

### Stage 7: Validation and Tuning

**Objective:** Verify retrieval quality and tune parameters for your actual content.

**Why it matters:** Default parameters work for demos but may not be optimal for your specific Markdown content and query patterns.

**Deliverables:**
- [ ] `scripts/eval_retrieval.py` — evaluation harness
- [ ] Test question set with expected results
- [ ] Tuned parameters documented

**Evaluation Approach:**

```python
# scripts/eval_retrieval.py
"""
Create a list of (query, expected_file, expected_section) triples.
Run search for each query.
Measure: recall@5, recall@10, MRR, and manual relevance judgment.
"""

test_questions = [
    {
        "query": "How does the backend authentication work?",
        "project": "royal_preps",
        "expected_file": "CLAUDE.md",
        "expected_section": "API",
    },
    {
        "query": "What is the branch strategy?",
        "project": "royal_preps",
        "expected_file": "CLAUDE.md",
        "expected_section": "Branch strategy",
    },
    # Add 20-30 questions covering your actual use cases
]
```

**Parameters to Tune:**

| Parameter | Default | Tune When |
|-----------|---------|-----------|
| `chunk_size` | 400 tokens | Results are too short (increase) or too noisy (decrease) |
| `chunk_overlap` | 100 tokens | Missing context at boundaries (increase) |
| `top_k` | 10 | Too many irrelevant results (decrease) or missing relevant ones (increase) |
| `score_threshold` | 0.3 | Too many low-quality results (increase) or missing valid results (decrease) |
| `embedding_model` | all-MiniLM-L6-v2 | Poor retrieval quality → try `all-mpnet-base-v2` or `BAAI/bge-base-en-v1.5` |

**Acceptance Criteria:**
- Evaluation script runs and produces metrics
- Recall@10 ≥ 0.8 on test questions
- MRR ≥ 0.5 on test questions
- No "obviously wrong" results in the top 5 for any test question
- Tuning decisions documented in README

---

## 5. Suggested Existing Materials to Reuse

### Libraries — Use Instead of Building

| Library | What It Replaces | Why |
|---------|-----------------|-----|
| `qdrant-client` | Vector storage | Battle-tested, same API we documented in qdrant_user_guide.md. Use `:memory:` mode for testing. |
| `sentence-transformers` | Embedding | Same API we documented in sentence_transformers_user_guide.md. Includes `.encode()` with normalization, batching, progress bars. |
| `markdown-it-py` | Markdown parsing | CommonMark-compliant parser with AST access. Better than regex for heading detection, code block boundaries, and nested structures. |
| `python-frontmatter` | YAML metadata extraction | One-liner: `post = frontmatter.load(file)` → `post.metadata`, `post.content`. |
| `typer` + `rich` | CLI framework | Typer provides argument parsing with type hints. Rich provides tables, progress bars, and color output. |
| `pydantic` + `pydantic-settings` | Data models + config | Type-safe models and .env config loading. You already use these in Royal Preps. |

### Reference Implementations — Study but Don't Copy

| Resource | What to Study | URL |
|----------|--------------|-----|
| **LangChain MarkdownHeaderTextSplitter** | Heading-based Markdown chunking logic. Study the approach; implement your own simpler version. | `langchain_text_splitters` package |
| **Qdrant examples repo** | End-to-end RAG examples with Qdrant + Sentence Transformers. The `rag-chatbot` example is closest to this project. | github.com/qdrant/examples |
| **sentence-transformers semantic search example** | Shows `util.semantic_search()` usage pattern. Good for validating your searcher against theirs. | sbert.net/examples/ |
| **FastMCP Python package** | Simplest way to build an MCP server in Python. Provides decorators for tool registration. | github.com/jlowin/fastmcp |
| **Claude Code MCP docs** | How to configure MCP servers for Claude CLI. Required reading for Stage 4. | docs.anthropic.com |

### Patterns — Apply Directly

| Pattern | Where to Apply |
|---------|---------------|
| **Deterministic IDs from content hash** | Chunk IDs. `sha256(project + path + index)[:16]`. Enables idempotent upserts. |
| **Qdrant `is_tenant=True` payload index** | Project filtering. Co-locates vectors by project for faster filtered search. |
| **Heading hierarchy prepended to chunk text** | Embedding enrichment. Dramatically improves retrieval for structural queries ("how does X work in section Y"). |
| **Explicit confidence labels in formatted context** | Prompt engineering. Tells Claude when to trust vs. caveat its answer. |
| **Blue-green collection swap via aliases** | Future: zero-downtime re-indexing. Build new collection → swap alias. |

---

## 6. MVP vs. Post-MVP

### MVP (Stages 0–4) — Build Now

| Feature | Included |
|---------|----------|
| Markdown chunking at heading boundaries | Yes |
| Embedding with all-MiniLM-L6-v2 | Yes |
| Qdrant storage with project filtering | Yes |
| Full indexing (re-index everything) | Yes |
| Search with score threshold | Yes |
| Formatted context for Claude | Yes |
| MCP server for Claude integration | Yes |
| Basic CLI (index, search) | Yes |

### Post-MVP — Build Later

| Feature | Why Defer | When to Add |
|---------|-----------|-------------|
| **Incremental indexing** (Stage 5) | Full re-index is fine for <10K chunks | When re-indexing takes >30 seconds |
| **Full CLI** (Stage 6) | MVP works with just `index` and `search` | When daily use reveals ergonomic needs |
| **Retrieval evaluation** (Stage 7) | Manual testing sufficient for MVP | When retrieval quality plateaus |
| **Cross-encoder reranking** | Adds latency and complexity | When top-10 results include >3 irrelevant chunks |
| **Hybrid search (dense + sparse)** | Requires SPLADE model setup | When keyword queries consistently fail |
| **Scalar quantization** | Only matters at >100K chunks | When RAM usage is a concern |
| **watchfiles auto-indexing** | Nice-to-have, not essential | When forgetting to re-index causes stale results |
| **Multi-collection per project** | One collection with payload filter is simpler | Never, unless projects have fundamentally different embedding needs |
| **Web UI** | CLI + Claude integration is sufficient | If non-CLI users need access |
| **Non-Markdown file types** | Scope creep; .md covers most knowledge bases | When you have critical .pdf or .docx content |

---

## 7. Validation and Testing Strategy

### Unit Tests

| Test | What It Validates |
|------|-------------------|
| `test_chunking.py` | Heading-based splitting, overlap behavior, metadata extraction, chunk ID determinism |
| `test_indexing.py` | Collection creation, point upsert, payload correctness, incremental detection |
| `test_retrieval.py` | Query encoding, filter construction, result formatting, confidence labels |

### Integration Tests

Use Qdrant in-memory mode (`QdrantClient(":memory:")`) for fast integration tests:

```python
# tests/conftest.py
import pytest
from qdrant_client import QdrantClient

@pytest.fixture
def qdrant_client():
    return QdrantClient(":memory:")
```

### Retrieval Quality Tests

Create 20-30 test questions from your actual Markdown content:

```python
test_cases = [
    # (query, project, expected to match file, expected to match section)
    ("What database does Royal Preps use?", "royal_preps", "CLAUDE.md", "Backend"),
    ("How are branches named?", "royal_preps", "CLAUDE.md", "Branch strategy"),
    ("What is the API base URL?", "royal_preps", "CLAUDE.md", "API"),
    # ... 20+ more
]
```

**Metrics to track:**
- **Recall@5**: Is the correct chunk in the top 5? Target: ≥ 0.8
- **Recall@10**: Is the correct chunk in the top 10? Target: ≥ 0.9
- **MRR**: How high is the first correct result? Target: ≥ 0.5
- **False positive rate**: How many top-5 results are completely irrelevant? Target: ≤ 1

### Failure Cases to Test

| Scenario | Expected Behavior |
|----------|-------------------|
| Query with no relevant content | Returns empty + "no content found" notice |
| Query matching wrong project | Project filter prevents cross-contamination |
| Very short query ("auth") | Returns reasonable results (may be low confidence) |
| Very long query (full paragraph) | Truncated to max_seq_length, still returns relevant results |
| Query about non-existent topic | Low scores, "LOW CONFIDENCE" label |
| Freshly edited file not re-indexed | Returns stale results (acceptable pre-Stage 5) |

---

## 8. Recommended Build Order

```
Stage 0: Scaffold          ← Do first — everything depends on it
    ↓
Stage 1: Chunking          ← Do second — indexing depends on chunks
    ↓
Stage 2: Embedding + Qdrant ← Do third — retrieval depends on indexed data
    ↓
Stage 3: Retrieval         ← Do fourth — Claude integration depends on retrieval
    ↓
Stage 4: Claude Integration ← Do fifth — this completes the MVP
    ↓
[MVP COMPLETE — system is usable]
    ↓
Stage 5: Incremental Index ← First quality-of-life improvement
    ↓
Stage 6: Full CLI          ← Second quality-of-life improvement
    ↓
Stage 7: Validation        ← Tune based on real usage data
```

**Why this order minimizes rework:**

1. **Scaffold first** — dependencies and structure locked in early; no rearranging later
2. **Chunking before embedding** — you can inspect and debug chunks without Qdrant running
3. **Embedding before retrieval** — you need data in Qdrant before you can search
4. **Retrieval before Claude** — you need to verify search quality before connecting to Claude
5. **Claude integration last in MVP** — it depends on everything else working
6. **Incremental indexing after MVP** — full re-index is acceptable for initial use
7. **CLI polish after MVP** — basic commands are enough to get started
8. **Validation last** — you need real usage patterns before meaningful tuning

---

## 9. Final Backlog Table

| Stage | Task | Output | Acceptance Criteria |
|-------|------|--------|---------------------|
| **0** | Create pyproject.toml with deps | `pyproject.toml` | `pip install -e .` succeeds |
| **0** | Create project directory structure | All dirs/files | Structure matches spec |
| **0** | Create docker-compose.yml for Qdrant | `docker-compose.yml` | `docker compose up -d` + health check pass |
| **0** | Implement config loading | `config.py` | Settings load from .env and defaults |
| **1** | Implement Markdown chunking | `chunking/markdown.py` | Splits at headings, respects chunk_size |
| **1** | Implement metadata extraction | `chunking/metadata.py` | Extracts frontmatter, heading hierarchy |
| **1** | Implement file scanner | `indexing/scanner.py` | Discovers all .md files, maps to projects |
| **1** | Define data models | `models.py` | Chunk, FileRecord, SearchResult models |
| **1** | Write chunking unit tests | `tests/test_chunking.py` | All tests pass |
| **2** | Implement encoder wrapper | `embedding/encoder.py` | Encodes text, returns normalized numpy arrays |
| **2** | Implement full indexer | `indexing/indexer.py` | Creates collection, upserts all chunks |
| **2** | Write indexing tests | `tests/test_indexing.py` | Tests pass with in-memory Qdrant |
| **3** | Implement searcher | `retrieval/searcher.py` | Returns ranked, filtered results |
| **3** | Implement formatter | `retrieval/formatter.py` | Produces context block with confidence |
| **3** | Write retrieval tests | `tests/test_retrieval.py` | Tests pass end-to-end |
| **4** | Implement MCP server | `integration/mcp_server.py` | Claude CLI connects and calls tool |
| **4** | Configure Claude CLI | `.claude/settings.json` | Claude uses knowledge base tool |
| **4** | Write CLAUDE.md instructions | `CLAUDE.md` additions | Claude searches before answering |
| **4** | **MVP COMPLETE** | Working system | Index → search → Claude answers with context |
| **5** | Implement state tracking | `indexing/state.py` | Detects changed/new/deleted files |
| **5** | Update indexer for incremental mode | `indexing/indexer.py` | Only re-indexes changed files |
| **6** | Implement full CLI | `cli.py` | All commands work: index, search, status, doctor, rebuild, projects |
| **7** | Create evaluation script | `scripts/eval_retrieval.py` | Produces recall, MRR metrics |
| **7** | Create test question set | Test data | 20+ questions with expected results |
| **7** | Tune parameters | Updated `.env` | Recall@10 ≥ 0.9 on test set |

---

*Plan version: 1.0 — March 2026*
*Target stack: Python 3.10+ / Qdrant (Docker) / Sentence Transformers / Claude CLI (MCP)*
