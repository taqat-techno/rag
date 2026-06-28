"""P3 increment 3 — first-class code metadata: imports / exports / signature.

- imports:   modules/names brought in by import statements.
- exports:   public symbols the chunk defines/exposes (heuristic, language-agnostic).
- signature: the declaration line of the chunk's primary function/class.
"""

from pathlib import Path

import pytest

from ragtools.chunking.dispatch import chunk_file
from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.indexing.indexer import ensure_collection, index_file
from ragtools.indexing.scanner import get_relative_path, scan_project
from ragtools.retrieval.searcher import Searcher

PY_SRC = '''"""Auth module."""
import os
from datetime import datetime


def issue_token(user: str) -> str:
    """Issue a token."""
    return os.urandom(8).hex()


class AuthService:
    """Validates tokens."""

    def validate(self, token: str) -> bool:
        return bool(token)
'''


def test_chunk_captures_imports_exports_signature(tmp_path):
    f = tmp_path / "auth.py"
    f.write_text(PY_SRC, encoding="utf-8")
    chunks = chunk_file(f, "p", "auth.py")

    all_imports = [i for c in chunks for i in c.imports]
    assert "os" in all_imports
    assert any("datetime" in i for i in all_imports)

    all_exports = [e for c in chunks for e in c.exports]
    assert "issue_token" in all_exports
    assert "AuthService" in all_exports

    all_sigs = " ".join(c.signature or "" for c in chunks)
    assert "def issue_token" in all_sigs or "class AuthService" in all_sigs


def test_metadata_round_trips_to_search_result(tmp_path):
    proj = tmp_path / "svc"
    proj.mkdir()
    (proj / "auth.py").write_text(PY_SRC, encoding="utf-8")

    settings = Settings(content_root=str(tmp_path), index_source_code=True)
    client = Settings.get_memory_client()
    encoder = Encoder(settings.embedding_model)
    ensure_collection(client, settings.collection_name, encoder.dimension)
    for pid, fp in scan_project(str(tmp_path), include_code=True):
        index_file(client=client, encoder=encoder, collection_name=settings.collection_name,
                   project_id=pid, file_path=fp, relative_path=get_relative_path(fp, str(tmp_path)))

    searcher = Searcher(client=client, encoder=encoder, settings=settings)
    results = searcher.search("issue an auth token", chunk_types=["code"])
    assert results
    # imports/exports/signature survive the Qdrant payload round-trip
    assert any(r.imports for r in results)
    assert any(r.exports for r in results)
