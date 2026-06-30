"""Provenance line anchors (P1-C / W11).

Generic, chunker-agnostic: every chunk gets a line_start/line_end by locating its
raw text in the source file, so an agent can jump to file:line. Approximate
(discovery, not authority) but cheap and stack-independent.
"""

from ragtools.chunking.anchors import attribute_line_spans
from ragtools.chunking.dispatch import chunk_file
from ragtools.models import Chunk


def _c(raw):
    return Chunk(chunk_id="x", project_id="p", file_path="f", chunk_index=0,
                 text=raw, raw_text=raw)


def test_attribute_line_spans_basic(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("line one\nline two\nline three\nline four\n", encoding="utf-8")
    chunks = [_c("line two\nline three"), _c("line four")]
    attribute_line_spans(chunks, f)
    assert chunks[0].line_start == 2
    assert chunks[0].line_end == 3
    assert chunks[1].line_start == 4


def test_markdown_chunks_get_line_anchors(tmp_path):
    f = tmp_path / "m.md"
    f.write_text(
        "# Title\n\n"
        "Intro paragraph with enough meaningful words to be retained here.\n\n"
        "## Second\n\n"
        "Second section body that also has sufficient meaningful content words.\n",
        encoding="utf-8",
    )
    chunks = chunk_file(f, "p", "m.md")
    assert chunks, "expected chunks"
    assert all(c.line_start >= 1 for c in chunks)
    # later sections start at later lines
    starts = [c.line_start for c in chunks]
    assert starts == sorted(starts)


def test_code_chunks_get_line_anchors(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(
        "import os\n\n\n"
        "def hello():\n    return 'world'\n\n\n"
        "def bye():\n    return 'goodbye'\n",
        encoding="utf-8",
    )
    chunks = chunk_file(f, "p", "a.py")
    by_fn = {c.function_name: c for c in chunks if c.function_name}
    assert by_fn, "expected function chunks"
    if "hello" in by_fn and "bye" in by_fn:
        assert by_fn["bye"].line_start > by_fn["hello"].line_start


def test_formatter_shows_line_anchor_and_class_tag():
    from ragtools.models import SearchResult
    from ragtools.retrieval.formatter import format_context, format_context_compact

    owned = SearchResult(chunk_id="x", score=0.8, text="body", raw_text="body",
                         file_path="app.py", project_id="p", confidence="HIGH",
                         line_start=12, line_end=20, source_class="owned")
    assert "app.py:L12-20" in format_context([owned], "q")

    dep = SearchResult(chunk_id="y", score=0.8, text="body", raw_text="body",
                       file_path="vendor/lib.py", project_id="p", confidence="HIGH",
                       source_class="dependency")
    out = format_context_compact([dep], "q")
    assert "[dependency]" in out


def test_search_result_exposes_line_anchors(tmp_path):
    from ragtools.config import Settings
    from ragtools.embedding.encoder import Encoder
    from ragtools.indexing.indexer import ensure_collection, index_file
    from ragtools.retrieval.searcher import Searcher

    settings = Settings(state_db=str(tmp_path / "s.db"))
    client = Settings.get_memory_client()
    encoder = Encoder(settings.embedding_model)
    ensure_collection(client, settings.collection_name, encoder.dimension)

    f = tmp_path / "doc.md"
    f.write_text(
        "# Title\n\nFirst paragraph.\n\n## Webhook\n\n"
        "The webhook endpoint receives disaster notification events for processing.\n",
        encoding="utf-8",
    )
    index_file(client=client, encoder=encoder, collection_name=settings.collection_name,
               project_id="p", file_path=f, relative_path="p/doc.md")
    searcher = Searcher(client=client, encoder=encoder, settings=settings)
    results = searcher.search(query="webhook endpoint disaster notification", top_k=5, score_threshold=0.0)
    assert results
    assert any(r.line_start and r.line_start >= 1 for r in results)
