# RAG Tools — Local Markdown RAG System

Local-first, Markdown-only RAG system. Search a local Qdrant knowledge base from Claude CLI.

## Quick Start

```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/Scripts/activate  # Windows Git Bash
# or: .venv\Scripts\activate   # Windows CMD
# or: source .venv/bin/activate # macOS/Linux

# Install in development mode
pip install -e ".[dev]"

# Verify setup
python scripts/verify_setup.py

# Run tests
pytest

# Check system health
rag doctor

# Show version
rag version
```

## Architecture

- **Qdrant local mode** — embedded vector database, no server needed
- **Sentence Transformers** — local embedding with all-MiniLM-L6-v2
- **SQLite** — file state tracking
- **Typer** — CLI interface
- **Pydantic** — configuration and data models

## Configuration

Copy `.env.example` to `.env` and edit as needed. All settings have defaults.
Environment variables use the `RAG_` prefix.

## Project Structure

```
src/ragtools/         # Main package
  cli.py              # CLI commands
  config.py           # Settings (Pydantic)
  models.py           # Data models
  chunking/           # Markdown parsing (Stage 1)
  embedding/          # Sentence Transformers (Stage 2)
  indexing/            # File scanning and indexing (Stage 2)
  retrieval/           # Search and formatting (Stage 3)
  integration/         # MCP server for Claude (Stage 4)
tests/                # Test suite
scripts/              # Utility scripts
data/                 # Local state (gitignored)
```

## CLI Commands

```bash
# Index Markdown files (incremental — skips unchanged files)
rag index /path/to/docs

# Force full re-index
rag index /path/to/docs --full

# Index only one project
rag index /path/to/docs --project my_project

# Search the knowledge base
rag search "How does authentication work?"

# Search within a specific project
rag search "database schema" --project my_project --top-k 5

# Show indexing status
rag status

# List indexed projects with file/chunk counts
rag projects

# Check system health
rag doctor

# Drop everything and rebuild from scratch
rag rebuild /path/to/docs

# Start MCP server for Claude CLI
rag serve

# Show version
rag version
```

## Claude CLI Integration (MCP)

The system exposes a local MCP server that Claude CLI can use to search your knowledge base.

### Setup

1. Index your Markdown files:
   ```bash
   rag index /path/to/your/docs
   ```

2. The MCP server is configured in `.claude/settings.json`.
   Claude CLI starts it automatically when needed.

3. Ask Claude project-specific questions — it will use `search_knowledge_base` automatically.

### Manual Testing

```bash
# Start MCP server manually (for debugging)
rag serve

# Or directly:
python -m ragtools.integration.mcp_server
```

## Data

All persistent state lives in `data/`. Delete it to start fresh.
This directory is gitignored — it contains derived data that can be rebuilt from Markdown source.
