"""Smoke tests — verify the package installs and basic imports work."""


def test_package_importable():
    """ragtools package can be imported."""
    import ragtools

    assert hasattr(ragtools, "__version__")


def test_version_is_string():
    """Version is a non-empty string."""
    from ragtools import __version__

    assert isinstance(__version__, str)
    assert len(__version__) > 0


def test_settings_defaults(settings):
    """Settings instantiate with all defaults (no .env needed)."""
    assert settings.qdrant_path == "data/qdrant"
    assert settings.collection_name == "markdown_kb"
    assert settings.embedding_model == "all-MiniLM-L6-v2"
    assert settings.embedding_dim == 384
    assert settings.chunk_size == 400
    assert settings.chunk_overlap == 100
    assert settings.top_k == 10
    assert settings.score_threshold == 0.3
    assert settings.state_db == "data/index_state.db"


def test_chunk_model():
    """Chunk model can be instantiated."""
    from ragtools.models import Chunk

    chunk = Chunk(
        chunk_id="abc123",
        project_id="test",
        file_path="README.md",
        chunk_index=0,
        text="# Heading\n\nContent",
        raw_text="Content",
    )
    assert chunk.chunk_id == "abc123"
    assert chunk.headings == []


def test_file_record_model():
    """FileRecord model can be instantiated."""
    from ragtools.models import FileRecord

    record = FileRecord(
        file_path="README.md",
        project_id="test",
        file_hash="abcdef1234567890",
        chunk_count=5,
    )
    assert record.chunk_count == 5


def test_search_result_model():
    """SearchResult model can be instantiated."""
    from ragtools.models import SearchResult

    result = SearchResult(
        chunk_id="abc123",
        score=0.85,
        text="# Heading\n\nContent",
        raw_text="Content",
        file_path="README.md",
        project_id="test",
        confidence="HIGH",
    )
    assert result.confidence == "HIGH"


def test_subpackages_importable():
    """All subpackage __init__.py files are importable."""
    import ragtools.chunking
    import ragtools.embedding
    import ragtools.indexing
    import ragtools.retrieval
    import ragtools.integration
