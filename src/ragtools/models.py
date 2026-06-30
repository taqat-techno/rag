"""Data models for RAG Tools."""

from datetime import datetime

from pydantic import BaseModel, Field


class Chunk(BaseModel):
    """A chunk of text extracted from a source, config, or documentation file."""

    chunk_id: str = Field(description="Deterministic ID: sha256(project_id + file_path + chunk_index)[:16]")
    project_id: str = Field(description="Project identifier for filtering")
    file_path: str = Field(description="Relative path to source file")
    chunk_index: int = Field(description="Sequential index within the file")
    text: str = Field(description="Chunk text with heading/symbol context prepended (embedded)")
    raw_text: str = Field(description="Original chunk text without context prefix")
    headings: list[str] = Field(default_factory=list, description="Heading/symbol hierarchy for this chunk")
    token_count: int = Field(default=0, description="Approximate token count")
    line_start: int = Field(default=0, description="1-based start line in the source file (0 = unknown)")
    line_end: int = Field(default=0, description="1-based end line in the source file (0 = unknown)")

    # --- Code/document metadata (Phase: code indexing) ---
    file_name: str = Field(default="", description="Base file name, e.g. 'searcher.py'")
    extension: str = Field(default="", description="File extension incl. dot, e.g. '.py'")
    language: str = Field(default="", description="Detected language, e.g. 'python', 'markdown'")
    chunk_type: str = Field(default="documentation", description="code | comment | config | documentation")
    source_class: str = Field(default="owned", description="owned | dependency | generated | secret")
    module: str = Field(default="", description="Project/module name this chunk belongs to")
    class_name: str | None = Field(default=None, description="Enclosing class name, if any")
    function_name: str | None = Field(default=None, description="Enclosing function/method name, if any")
    symbols: list[str] = Field(default_factory=list, description="Symbols defined in this chunk (classes, funcs, imports, ...)")
    imports: list[str] = Field(default_factory=list, description="Modules/names imported by this chunk")
    exports: list[str] = Field(default_factory=list, description="Public symbols this chunk defines/exposes")
    signature: str = Field(default="", description="Declaration line of the chunk's primary function/class")


class FileRecord(BaseModel):
    """Tracks the indexing state of a single file."""

    file_path: str
    project_id: str
    file_hash: str
    chunk_count: int
    last_indexed: datetime = Field(default_factory=datetime.now)


class SearchResult(BaseModel):
    """A single search result from the knowledge base."""

    chunk_id: str
    score: float
    text: str
    raw_text: str
    file_path: str
    project_id: str
    headings: list[str] = Field(default_factory=list)
    confidence: str = Field(description="HIGH (>=0.7), MODERATE (0.5-0.7), LOW (<0.5)")
    line_start: int = Field(default=0)
    line_end: int = Field(default=0)

    # --- Code/document metadata (mirrors Chunk; defaults keep old data working) ---
    language: str = Field(default="")
    chunk_type: str = Field(default="documentation")
    source_class: str = Field(default="owned")
    class_name: str | None = Field(default=None)
    function_name: str | None = Field(default=None)
    symbols: list[str] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    exports: list[str] = Field(default_factory=list)
    signature: str = Field(default="")
    # Rerank-adjusted score (set by the dev pipeline; None for plain search).
    adjusted_score: float | None = Field(default=None)
