"""Data models for RAG Tools."""

from datetime import datetime

from pydantic import BaseModel, Field


class Chunk(BaseModel):
    """A chunk of text extracted from a Markdown file."""

    chunk_id: str = Field(description="Deterministic ID: sha256(project_id + file_path + chunk_index)[:16]")
    project_id: str = Field(description="Project identifier for filtering")
    file_path: str = Field(description="Relative path to source Markdown file")
    chunk_index: int = Field(description="Sequential index within the file")
    text: str = Field(description="Chunk text with heading hierarchy prepended")
    raw_text: str = Field(description="Original chunk text without headings")
    headings: list[str] = Field(default_factory=list, description="Heading hierarchy for this chunk")
    token_count: int = Field(default=0, description="Approximate token count")


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
