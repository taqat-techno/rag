# Local Markdown RAG System — Implementation Plan (Local Mode)

> Revised staged delivery plan — fully local, no Docker, no server processes.
> Qdrant runs embedded via Python client local mode. All data on local filesystem.

---

## 1. Revised Executive Summary

### Direction

This plan replaces the Docker-based Qdrant server with **Qdrant Python client in local mode** (`QdrantClient(path="./data/qdrant")`). There is no separate server process. Qdrant runs embedded inside your Python process, storing all data as files in a local directory. The embedding model also runs locally via Sentence Transformers. The entire system is a single Python package with zero infrastructure dependencies beyond Python itself.

### Why Local Mode Is the Right MVP Choice

1. **Zero infrastructure** — no Docker, no containers, no background services, no port management
2. **Single-process simplicity** — the Qdrant engine runs inside your Python process
3. **Portable** — copy the data directory to move the entire knowledge base
4. **Debuggable** — no network layer between your code and the vector store
5. **Fast startup** — no waiting for container boot; just import and use
6. **Same API** — Qdrant local mode uses the identical Python client API as server mode. Migration to server/Docker later requires changing one line of code.

### Delivery Sequence (Unchanged)

| Stage | Name | Focus | Est. Effort |
|-------|------|-------|-------------|
| **0** | Project scaffold | Structure, config, dependencies | 2-3 hours |
| **1** | Markdown chunking | Parse and chunk .md files | 4-6 hours |
| **2** | Embedding + Qdrant indexing | Encode chunks, store locally | 4-6 hours |
| **3** | Retrieval pipeline | Query → embed → search → rank | 3-4 hours |
| **4** | Claude CLI integration | MCP server | 6-8 hours |
| **5** | Incremental indexing | Only re-index changed files | 4-6 hours |
| **6** | CLI and maintenance | index, search, status, doctor, rebuild | 4-6 hours |
| **7** | Validation and tuning | Test retrieval quality, tune parameters | 4-6 hours |

**MVP = Stages 0–4.** Same boundary as before.

---

## 2. What Changed From the Original Plan

### Removed

| Item | Was | Now |
|------|-----|-----|
| `docker-compose.yml` | Required for Qdrant server | **Removed entirely** |
| Docker setup step | Stage 0 deliverable | **Removed** |
| Qdrant health check via HTTP | `curl localhost:6333/healthz` | **Replaced** with Python import check |
| Port configuration (6333, 6334) | Needed for server mode | **Not applicable** |
| Container restart/management | Operational concern | **Not applicable** |
| gRPC transport | Available in server mode | **Not available** in local mode |
| Web dashboard | `localhost:6333/dashboard` | **Not available** in local mode |
| Snapshots via REST API | Server-mode feature | **Replaced** with filesystem backup |
| Prometheus metrics endpoint | Server-mode feature | **Not available** — use Python-level timing |
| Replication/sharding | Server-mode distributed features | **Not applicable** for single-user local |

### Simplified

| Item | Was | Now |
|------|-----|-----|
| Qdrant connection | `QdrantClient(host="localhost", port=6333)` | `QdrantClient(path="./data/qdrant")` |
| Config | `qdrant_url`, port, API key | `qdrant_path` (local directory) |
| Backup | Snapshot API + download | `shutil.copytree("./data/qdrant", backup_path)` |
| Recovery | Restore snapshot via API | Delete data dir + re-index from Markdown source |
| Testing | In-memory client (already planned) | Same — `QdrantClient(":memory:")` |
| Health check | HTTP request | `try: client.get_collections()` |

### Added

| Item | Why |
|------|-----|
| `data/qdrant/` directory | Local Qdrant storage path |
| `.gitignore` entry for `data/` | Prevent committing vector data and model cache |
| File locking note | Local mode is single-process; warn against concurrent access |
| Filesystem backup guidance | Primary recovery strategy |

---

## 3. Updated Recommended Project Structure

```
rag/
├── pyproject.toml                    # Project config, dependencies, CLI entry points
├── README.md                         # Project documentation
├── .env                              # Local config (model name, paths, thresholds)
├── .gitignore                        # Exclude data/, .env, __pycache__
│
├── src/
│   └── ragtools/                     # Main Python package
│       ├── __init__.py
│       ├── cli.py                    # CLI entry point (typer)
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
│       │   └── encoder.py            # SentenceTransformer wrapper
│       │
│       ├── indexing/
│       │   ├── __init__.py
│       │   ├── indexer.py            # Full and incremental indexing orchestration
│       │   ├── state.py              # File hash tracking (SQLite)
│       │   └── scanner.py            # Walk directories, discover .md files
│       │
│       ├── retrieval/
│       │   ├── __init__.py
│       │   ├── searcher.py           # Query → Qdrant search → results
│       │   └── formatter.py          # Format results for Claude context
│       │
│       └── integration/
│           ├── __init__.py
│           └── mcp_server.py         # MCP server for Claude CLI
│
├── tests/
│   ├── conftest.py                   # Fixtures (in-memory Qdrant client)
│   ├── test_chunking.py
│   ├── test_indexing.py
│   ├── test_retrieval.py
│   └── fixtures/                     # Sample .md files
│       ├── project_a/
│       │   ├── README.md
│       │   └── guide.md
│       └── project_b/
│           └── notes.md
│
├── scripts/
│   └── eval_retrieval.py             # Retrieval quality evaluation
│
└── data/                             # ← ALL local state lives here
    ├── qdrant/                       # Qdrant local storage (auto-created)
    │   ├── collection/               # Collection data files
    │   │   └── markdown_kb/          # Our single collection
    │   └── ...                       # Qdrant internal files
    ├── index_state.db                # SQLite: file hashes, indexing metadata
    └── backups/                      # Manual backup copies
```

**Key difference:** The `data/` directory is the single source of all persistent state. Back it up to protect the index. Delete it to start fresh.

**.gitignore additions:**

```gitignore
# RAG local data (re-indexable from source)
data/qdrant/
data/index_state.db
data/backups/

# Model cache (auto-downloaded)
.cache/

# Environment
.env
```

---

## 4. Revised Delivery Stages

---

### Stage 0: Project Scaffold

**Objective:** Set up the project skeleton, dependencies, and local Qdrant initialization.

**Updated Deliverables:**
- [ ] `pyproject.toml` with all dependencies
- [ ] Project directory structure
- [ ] `config.py` with local-mode defaults
- [ ] Qdrant local client initializes and creates data directory
- [ ] `.gitignore` excludes `data/`
- [ ] Verification: import + create collection works

**What changed from Docker version:**
- ~~`docker-compose.yml`~~ — removed
- ~~Qdrant health check via curl~~ — replaced with Python client check
- ~~`qdrant_url` config~~ — replaced with `qdrant_path`
- Setup is now: `pip install -e .` and you're done

**Dependencies (pyproject.toml):**

```toml
[project]
name = "ragtools"
version = "0.1.0"
requires-python = ">=3.10"

dependencies = [
    "qdrant-client>=1.12.0",
    "sentence-transformers>=5.0.0",
    "typer[all]>=0.12.0",
    "pydantic>=2.0.0",
    "pydantic-settings>=2.0.0",
    "python-frontmatter>=1.0.0",
    "markdown-it-py>=3.0.0",
    "rich>=13.0.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0.0", "pytest-cov>=5.0.0"]

[project.scripts]
rag = "ragtools.cli:app"
```

> **Note:** `qdrant-client` in local mode uses an embedded Qdrant engine compiled from Rust. The PyPI wheel includes this — no separate Rust toolchain or server binary needed.

**Config (local mode):**

```python
# src/ragtools/config.py
from pathlib import Path
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Qdrant local mode — path to data directory
    qdrant_path: str = "data/qdrant"

    # Collection
    collection_name: str = "markdown_kb"

    # Embedding
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384

    # Chunking
    chunk_size: int = 400       # approximate token count
    chunk_overlap: int = 100

    # Content
    content_root: str = "."     # Root to scan for project subdirectories

    # Retrieval
    top_k: int = 10
    score_threshold: float = 0.3

    # State
    state_db: str = "data/index_state.db"

    model_config = {"env_prefix": "RAG_", "env_file": ".env"}

    def get_qdrant_client(self):
        """Create Qdrant client in local persistent mode."""
        from qdrant_client import QdrantClient
        Path(self.qdrant_path).mkdir(parents=True, exist_ok=True)
        return QdrantClient(path=self.qdrant_path)
```

**Qdrant client initialization — the key difference:**

```python
from qdrant_client import QdrantClient

# LOCAL MODE (this plan) — embedded engine, data on disk
client = QdrantClient(path="./data/qdrant")

# IN-MEMORY (for tests) — no persistence
client = QdrantClient(":memory:")

# SERVER MODE (future migration) — separate Qdrant process
# client = QdrantClient(host="localhost", port=6333)
```

All three use the **identical API** after initialization. Every `client.create_collection()`, `client.upsert()`, `client.query_points()` call works the same.

**Verification:**

```python
# scripts/verify_setup.py
from ragtools.config import Settings

settings = Settings()
client = settings.get_qdrant_client()
collections = client.get_collections()
print(f"Qdrant local mode OK. Collections: {len(collections.collections)}")
print(f"Data path: {settings.qdrant_path}")
```

**Acceptance Criteria:**
- `pip install -e .` succeeds
- `python scripts/verify_setup.py` prints OK
- `data/qdrant/` directory is created
- `pytest` runs (even with no tests)

**Risks:**
- Qdrant client wheel may not exist for uncommon platforms → fall back to `pip install qdrant-client --no-binary qdrant-client` (requires Rust compiler)
- First import of `sentence-transformers` downloads the model (~90MB for all-MiniLM-L6-v2) → may be slow on first run

---

### Stage 1: Markdown Chunking

**Objective:** Parse `.md` files into semantically meaningful chunks with metadata.

**What changed from Docker version:** Nothing. Chunking is independent of the storage backend.

**Deliverables:** Same as original plan.
- [ ] `chunking/markdown.py` — heading-based splitting
- [ ] `chunking/metadata.py` — frontmatter + heading extraction
- [ ] `models.py` — Chunk, FileRecord, SearchResult
- [ ] `indexing/scanner.py` — discover files, map to projects
- [ ] Unit tests with fixture files

**Data models, chunking strategy, and implementation notes:** Identical to original plan. See original Section 4, Stage 1.

**Acceptance Criteria:** Same as original.

---

### Stage 2: Embedding + Qdrant Indexing

**Objective:** Encode chunks into embeddings and store them in local Qdrant.

**What changed from Docker version:**
- Client initialization uses `path=` instead of `host=`/`port=`
- No health check via HTTP — use `client.get_collections()` instead
- No gRPC option — local mode uses direct in-process calls (faster than either REST or gRPC)
- No web dashboard — verify via `client.count()` and `client.get_collection()`

**Updated collection creation:**

```python
# src/ragtools/indexing/indexer.py
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PayloadSchemaType

def ensure_collection(client: QdrantClient, name: str, dim: int):
    """Create collection if it doesn't exist."""
    collections = [c.name for c in client.get_collections().collections]
    if name in collections:
        return  # Already exists

    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )
    # Payload indexes for filtering
    client.create_payload_index(name, "project_id", PayloadSchemaType.KEYWORD)
    client.create_payload_index(name, "file_path", PayloadSchemaType.KEYWORD)
```

> **Note on `is_tenant`:** The `is_tenant=True` parameter on payload indexes is a server-mode optimization that co-locates vectors by tenant in storage. In local mode with a single collection under ~100K points, the performance difference is negligible. Include it if the API supports it; omit if it causes errors in your client version.

**Encoder wrapper:** Identical to original plan.

**Indexing pipeline:** Identical to original plan — `client.upsert()` works the same.

**Acceptance Criteria:**
- `client.count("markdown_kb")` returns expected chunk count
- `client.get_collection("markdown_kb")` shows correct config
- Data files exist in `data/qdrant/collection/markdown_kb/`
- Re-running indexing produces identical results (idempotent)

**Local-mode specifics:**
- Qdrant local mode writes data to disk automatically — no flush needed
- Data is available immediately after `upsert()` (no eventual consistency)
- **Single-process only** — do not open the same `qdrant_path` from two Python processes simultaneously. This causes corruption.

---

### Stage 3: Retrieval Pipeline

**Objective:** Build the query → embed → search → format pipeline.

**What changed from Docker version:** Nothing in the API. `client.query_points()` works identically in local mode.

**Deliverables, implementation, acceptance criteria:** Identical to original plan. See original Section 4, Stage 3.

**Local-mode performance note:** Retrieval in local mode is actually faster than server mode for single-user workloads because there's no network round-trip. Expect <50ms for query_points on collections under 100K vectors.

---

### Stage 4: Claude CLI Integration

**Objective:** Connect the retrieval pipeline to Claude CLI via an MCP server.

**What changed from Docker version:** The MCP server creates a Qdrant local client internally instead of connecting to a server.

**Critical local-mode consideration:** The MCP server runs as a subprocess of Claude CLI. It opens the Qdrant local data directory. **No other process should have the same directory open simultaneously.** This means:

- Don't run `rag index` while Claude CLI is using the MCP server
- Don't run two Claude CLI sessions pointing at the same `qdrant_path`
- The CLI commands should detect if the data directory is locked and warn

**Updated MCP server initialization:**

```python
# src/ragtools/integration/mcp_server.py
from ragtools.config import Settings
from ragtools.retrieval.searcher import Searcher
from ragtools.embedding.encoder import Encoder

# Initialize on startup
settings = Settings()
client = settings.get_qdrant_client()  # Opens local data directory
encoder = Encoder(settings.embedding_model)
searcher = Searcher(client, encoder, settings)

# Tool: search_knowledge_base
# Parameters: query (str), project (str|None), top_k (int)
# Returns: formatted context string
```

**Claude CLI configuration:**

```json
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

**Acceptance Criteria:** Same as original plan — Claude connects, calls the tool, receives context.

**Additional acceptance criterion:** Verify that the MCP server correctly opens and reads from the local Qdrant data directory.

---

### Stage 5: Incremental Indexing

**Objective:** Only re-index files that have changed.

**What changed from Docker version:** State tracking uses SQLite instead of JSON (more robust for file-level operations, handles concurrent reads better if needed later).

**Updated state tracking:**

```python
# src/ragtools/indexing/state.py
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime

class IndexState:
    def __init__(self, db_path: str = "data/index_state.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS file_state (
                file_path TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                chunk_count INTEGER NOT NULL,
                last_indexed TEXT NOT NULL
            )
        """)
        self.conn.commit()

    def file_changed(self, file_path: str, current_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT file_hash FROM file_state WHERE file_path = ?", (file_path,)
        ).fetchone()
        return row is None or row[0] != current_hash

    def update(self, file_path: str, project_id: str, file_hash: str, chunk_count: int):
        self.conn.execute("""
            INSERT OR REPLACE INTO file_state
            (file_path, project_id, file_hash, chunk_count, last_indexed)
            VALUES (?, ?, ?, ?, ?)
        """, (file_path, project_id, file_hash, chunk_count, datetime.now().isoformat()))
        self.conn.commit()

    def remove(self, file_path: str):
        self.conn.execute("DELETE FROM file_state WHERE file_path = ?", (file_path,))
        self.conn.commit()

    def get_all_indexed_paths(self) -> set[str]:
        rows = self.conn.execute("SELECT file_path FROM file_state").fetchall()
        return {row[0] for row in rows}

    @staticmethod
    def hash_file(file_path: Path) -> str:
        return hashlib.sha256(file_path.read_bytes()).hexdigest()
```

**Why SQLite instead of JSON (original plan):**
- Atomic writes — no corruption from interrupted saves
- Query capability — useful for `status` and `doctor` commands
- Standard Python library — no extra dependency
- Handles the single-process constraint naturally (SQLite allows concurrent reads, serialized writes)

**Acceptance Criteria:** Same as original plan.

---

### Stage 6: CLI and Maintenance

**Objective:** Full CLI for all operations.

**What changed from Docker version:** Health check replaces HTTP ping with local client check. No container management commands.

**Updated `doctor` command:**

```python
@app.command()
def doctor():
    """Check system health."""
    console = Console()
    checks = []

    # 1. Qdrant local data directory
    qdrant_path = Path(settings.qdrant_path)
    if qdrant_path.exists():
        checks.append(("Qdrant data dir", "OK", str(qdrant_path)))
    else:
        checks.append(("Qdrant data dir", "MISSING", "Run 'rag index' to create"))

    # 2. Collection exists
    try:
        client = settings.get_qdrant_client()
        info = client.get_collection(settings.collection_name)
        checks.append(("Collection", "OK", f"{info.points_count} points"))
    except Exception as e:
        checks.append(("Collection", "ERROR", str(e)))

    # 3. Embedding model available
    try:
        from ragtools.embedding.encoder import Encoder
        enc = Encoder(settings.embedding_model)
        checks.append(("Embedding model", "OK", f"{enc.dimension}d"))
    except Exception as e:
        checks.append(("Embedding model", "ERROR", str(e)))

    # 4. SQLite state
    state_path = Path(settings.state_db)
    if state_path.exists():
        checks.append(("Index state DB", "OK", str(state_path)))
    else:
        checks.append(("Index state DB", "MISSING", "Run 'rag index' to create"))

    # 5. Content root
    content_root = Path(settings.content_root)
    md_count = len(list(content_root.rglob("*.md")))
    checks.append(("Markdown files", "OK", f"{md_count} files found"))

    # Print table
    table = Table(title="RAG System Health")
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Details")
    for name, status, details in checks:
        color = "green" if status == "OK" else "red"
        table.add_row(name, f"[{color}]{status}[/{color}]", details)
    console.print(table)
```

**Updated `rebuild` command:**

```python
@app.command()
def rebuild():
    """Drop all data and re-index from scratch."""
    import shutil
    qdrant_path = Path(settings.qdrant_path)
    state_path = Path(settings.state_db)

    console.print("[yellow]This will delete all indexed data and rebuild from Markdown source.[/yellow]")
    if not typer.confirm("Continue?"):
        raise typer.Abort()

    # Close any open client, delete data directory
    if qdrant_path.exists():
        shutil.rmtree(qdrant_path)
        console.print(f"Deleted {qdrant_path}")

    if state_path.exists():
        state_path.unlink()
        console.print(f"Deleted {state_path}")

    # Re-index
    console.print("[green]Rebuilding index...[/green]")
    _run_full_index()
```

**All CLI commands:** Same as original plan (index, search, status, doctor, rebuild, projects).

---

### Stage 7: Validation and Tuning

**Objective:** Verify retrieval quality and tune parameters.

**What changed from Docker version:** Nothing. Evaluation is independent of storage backend.

**Deliverables, acceptance criteria:** Same as original plan.

---

## 5. Local-Mode Constraints and Limitations

### What Local Mode Does Well

| Capability | Notes |
|-----------|-------|
| Full vector search | Same HNSW index, same query API |
| Payload filtering | All filter types work (keyword, range, etc.) |
| Payload indexes | Create and use indexes normally |
| Persistence | Data survives process restarts |
| Portability | Copy `data/qdrant/` to move the index |
| Fast for small-medium data | <100K vectors, <50ms search |
| Zero ops overhead | No server to monitor, restart, or upgrade |
| Testing | Switch to `":memory:"` for tests |

### What Local Mode Does NOT Provide

| Feature | Why Missing | Impact |
|---------|-------------|--------|
| **Web dashboard** | Server-only feature | Use CLI `status` command instead |
| **REST/gRPC API** | No server process | Cannot access from other processes or languages |
| **Prometheus metrics** | Server-only endpoint | Use Python-level timing (e.g., `time.perf_counter()`) |
| **Snapshots via API** | Server-only feature | Use filesystem backup instead |
| **Distributed mode** | Requires Raft consensus across nodes | Single-machine only |
| **Replication** | Requires multiple nodes | No HA — rebuild from source if corrupted |
| **Multi-process access** | Local mode locks the data directory | Only one process can open `qdrant_path` at a time |
| **Collection aliases** | Limited or absent in local mode | Use direct collection names |
| **Quantization** | May have limited support in local mode | Test before relying on it |

### What Requires Future Migration

| Feature | Migration Trigger |
|---------|-------------------|
| Multi-process access (CLI + MCP simultaneous) | When you need to index while Claude is searching |
| Shared access across multiple tools | When other services need the vector store |
| Monitoring and alerting | When you need observability |
| Snapshots for backup automation | When manual filesystem backup becomes tedious |
| Scale beyond ~500K vectors | When search latency exceeds acceptable limits |

---

## 6. Backup, Recovery, and Rebuild Strategy

### Backup Strategy (MVP)

**Primary approach: filesystem copy of the data directory.**

```python
# src/ragtools/cli.py — backup command (Stage 6)
@app.command()
def backup(
    dest: str = typer.Option("data/backups", help="Backup destination directory"),
):
    """Create a backup of the knowledge base."""
    import shutil
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(dest) / f"backup_{timestamp}"

    # IMPORTANT: Close Qdrant client before copying
    # In local mode, files may be locked during writes
    shutil.copytree(settings.qdrant_path, backup_dir / "qdrant")

    state_path = Path(settings.state_db)
    if state_path.exists():
        shutil.copy2(state_path, backup_dir / "index_state.db")

    console.print(f"[green]Backup saved to {backup_dir}[/green]")
```

**What to back up:**

| Path | Contents | Size (estimate) |
|------|----------|-----------------|
| `data/qdrant/` | Vector index, payloads, metadata | ~50MB per 10K chunks |
| `data/index_state.db` | File hashes, indexing state | <1MB |

**When to back up:**
- Before running `rag rebuild`
- Before upgrading the embedding model (changes all vectors)
- Before major Markdown content reorganization

### Recovery Strategy

**Scenario 1: Corrupted Qdrant data**

```bash
# Delete corrupted data
rm -rf data/qdrant/

# Re-index from Markdown source (source of truth)
rag rebuild
```

This always works because the Markdown files are the source of truth. The index is derived data.

**Scenario 2: Corrupted SQLite state**

```bash
# Delete state DB
rm data/index_state.db

# Force full re-index (will rebuild state)
rag index --full
```

**Scenario 3: Restore from backup**

```bash
# Stop any process using the data directory
# Copy backup back
cp -r data/backups/backup_20260331_120000/qdrant/ data/qdrant/
cp data/backups/backup_20260331_120000/index_state.db data/index_state.db
```

### When Full Rebuild Is the Safest Option

| Situation | Action |
|-----------|--------|
| Changed embedding model | **Must rebuild** — old vectors are incompatible |
| Changed chunk size or overlap | **Should rebuild** — old chunks have wrong boundaries |
| Qdrant data directory corrupted | **Must rebuild** — delete `data/qdrant/` and re-index |
| Upgraded `qdrant-client` major version | **Should rebuild** — storage format may have changed |
| Markdown files were reorganized (moved/renamed) | **Should rebuild** — old file paths are stale |

**Rebuild cost:** For a workspace with ~500 Markdown files totaling ~2MB of text, full rebuild takes approximately 30-60 seconds (chunking + embedding + indexing). This is fast enough that rebuild-from-source is a viable primary recovery strategy.

---

## 7. Future Migration Path

### When to Migrate from Local Mode

| Trigger | Why Migrate | Target |
|---------|-------------|--------|
| **Need CLI + MCP simultaneously** | Local mode is single-process | Docker/server mode |
| **Multiple tools need vector access** | Cannot share local client | Server mode with REST API |
| **>500K vectors** | Local mode performance degrades | Server mode with mmap/on-disk storage |
| **Need web dashboard** | Useful for debugging and monitoring | Server mode |
| **Need automated snapshots** | Filesystem backup is manual | Server mode snapshot API |
| **Team collaboration** | Multiple users querying same index | Server mode + network access |

### How to Migrate (One Line Change)

```python
# Before (local mode)
client = QdrantClient(path="./data/qdrant")

# After (server mode)
client = QdrantClient(host="localhost", port=6333)
```

**Migration steps:**

1. Start Qdrant server (Docker or binary)
2. Change `config.py` to use `qdrant_url` instead of `qdrant_path`
3. Run `rag rebuild` to populate the server
4. Update MCP server to use server-mode client
5. Done — all other code is unchanged

**Data migration:** There is no direct data migration from local mode to server mode. Re-index from Markdown source. Since Markdown is the source of truth and rebuild is fast, this is the simplest and safest approach.

---

## 8. Updated MVP vs. Post-MVP

### MVP (Stages 0–4) — Build Now

| Feature | Included | Local-Mode Impact |
|---------|----------|-------------------|
| Markdown chunking at heading boundaries | Yes | None |
| Embedding with all-MiniLM-L6-v2 | Yes | None |
| Qdrant local storage with project filtering | Yes | Uses `path=` instead of `host=` |
| Full indexing (re-index everything) | Yes | None |
| Search with score threshold | Yes | None |
| Formatted context for Claude with confidence labels | Yes | None |
| MCP server for Claude integration | Yes | Opens local Qdrant data dir |
| Basic CLI (index, search) | Yes | No Docker commands needed |

### Post-MVP — Build Later

| Feature | Why Defer | Local-Mode Note |
|---------|-----------|-----------------|
| Incremental indexing (Stage 5) | Full re-index is fast for <500 files | Same in local mode |
| Full CLI (Stage 6) | MVP works with basic commands | Doctor checks filesystem instead of HTTP |
| Retrieval evaluation (Stage 7) | Manual testing first | Same in local mode |
| Backup command | Manual `cp` suffices for MVP | Filesystem backup, no snapshot API |
| Cross-encoder reranking | Complexity, latency | Same in local mode |
| Hybrid search (dense + sparse) | Requires SPLADE setup | Same in local mode |
| watchfiles auto-indexing | Nice-to-have | Same in local mode |
| **Migration to server mode** | Not needed until triggers above | Keep code compatible |

---

## 9. Final Revised Backlog Table

| Stage | Task | Output | Acceptance Criteria | Local-Mode Notes |
|-------|------|--------|---------------------|------------------|
| **0** | Create pyproject.toml | `pyproject.toml` | `pip install -e .` succeeds | No Docker deps |
| **0** | Create project structure | All dirs/files | Structure matches spec | `data/` dir for all state |
| **0** | Implement config | `config.py` | Settings load, `get_qdrant_client()` returns local client | `qdrant_path` not `qdrant_url` |
| **0** | Verify setup | `scripts/verify_setup.py` | Script prints OK, `data/qdrant/` created | No HTTP health check |
| **0** | Add .gitignore | `.gitignore` | `data/` excluded | Prevent committing vectors |
| **1** | Implement Markdown chunking | `chunking/markdown.py` | Splits at headings, respects chunk_size | Unchanged |
| **1** | Implement metadata extraction | `chunking/metadata.py` | Extracts frontmatter, heading hierarchy | Unchanged |
| **1** | Implement file scanner | `indexing/scanner.py` | Discovers .md files, maps to projects | Unchanged |
| **1** | Define data models | `models.py` | Chunk, FileRecord, SearchResult | Unchanged |
| **1** | Write chunking tests | `tests/test_chunking.py` | All pass | Unchanged |
| **2** | Implement encoder | `embedding/encoder.py` | Encodes, normalizes, batches | Unchanged |
| **2** | Implement indexer | `indexing/indexer.py` | Creates collection, upserts chunks | `ensure_collection()` checks existence |
| **2** | Write indexing tests | `tests/test_indexing.py` | Pass with `QdrantClient(":memory:")` | In-memory for tests |
| **3** | Implement searcher | `retrieval/searcher.py` | Returns ranked, filtered results | Unchanged |
| **3** | Implement formatter | `retrieval/formatter.py` | Context block with confidence labels | Unchanged |
| **3** | Write retrieval tests | `tests/test_retrieval.py` | Pass end-to-end | Unchanged |
| **4** | Implement MCP server | `integration/mcp_server.py` | Claude CLI connects and calls tool | Opens local Qdrant path |
| **4** | Configure Claude CLI | Settings file | Claude uses knowledge base tool | Same |
| **4** | Write CLAUDE.md instructions | CLAUDE.md | Claude searches before answering | Same |
| **4** | **MVP COMPLETE** | Working local system | Index → search → Claude answers with context | No Docker anywhere |
| **5** | Implement state tracking | `indexing/state.py` | SQLite tracks file hashes | SQLite not JSON |
| **5** | Update indexer | `indexing/indexer.py` | Only re-indexes changed files | Same logic |
| **6** | Implement full CLI | `cli.py` | All commands: index, search, status, doctor, rebuild, projects, backup | Doctor checks filesystem |
| **7** | Create eval script | `scripts/eval_retrieval.py` | Produces recall, MRR metrics | Unchanged |
| **7** | Tune parameters | `.env` | Recall@10 ≥ 0.9 on test set | Unchanged |

---

## Appendix: Single-Process Constraint Workaround

The biggest operational constraint of local mode is single-process access. Here's how to handle the main conflict:

**Problem:** `rag index` and the MCP server (used by Claude) both need Qdrant access. They can't run simultaneously.

**MVP workaround:** Index before starting Claude sessions. The MCP server opens Qdrant in read-mostly mode. As long as you don't run `rag index` while Claude is active, there's no conflict.

**Practical workflow:**

```bash
# 1. Index your Markdown files
rag index .

# 2. Start using Claude (MCP server starts automatically)
claude

# 3. When you update Markdown files, exit Claude first
# Then re-index
rag index .

# 4. Resume Claude
claude
```

**Post-MVP solution:** Migrate to server mode when this becomes a pain point. Server-mode Qdrant handles concurrent reads and writes natively.

---

*Plan version: 2.0 (Local Mode) — March 2026*
*Target stack: Python 3.10+ / Qdrant local mode / Sentence Transformers / Claude CLI (MCP)*
*No Docker, no containers, no server processes.*
