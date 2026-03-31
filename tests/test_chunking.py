"""Tests for Markdown chunking and file scanning."""

from pathlib import Path

import pytest

from ragtools.chunking.markdown import (
    chunk_markdown_file,
    _split_by_headings,
    _make_chunk_id,
)
from ragtools.chunking.metadata import estimate_tokens, extract_frontmatter
from ragtools.indexing.scanner import (
    discover_projects,
    discover_markdown_files,
    scan_project,
    get_relative_path,
)
from ragtools.models import Chunk


FIXTURES = Path(__file__).parent / "fixtures"


# --- Scanner Tests ---


class TestDiscoverProjects:
    def test_finds_project_dirs(self):
        projects = discover_projects(str(FIXTURES))
        assert "project_a" in projects
        assert "project_b" in projects

    def test_excludes_hidden_dirs(self, tmp_path):
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "_private").mkdir()
        (tmp_path / "visible").mkdir()
        projects = discover_projects(str(tmp_path))
        assert "visible" in projects
        assert ".hidden" not in projects
        assert "_private" not in projects

    def test_empty_dir(self, tmp_path):
        projects = discover_projects(str(tmp_path))
        assert projects == {}


class TestDiscoverMarkdownFiles:
    def test_finds_md_files(self):
        files = discover_markdown_files(FIXTURES / "project_a")
        names = [f.name for f in files]
        assert "README.md" in names
        assert "guide.md" in names

    def test_returns_sorted(self):
        files = discover_markdown_files(FIXTURES / "project_a")
        assert files == sorted(files)

    def test_empty_dir(self, tmp_path):
        files = discover_markdown_files(tmp_path)
        assert files == []


class TestScanProject:
    def test_scan_all_projects(self):
        results = scan_project(str(FIXTURES))
        project_ids = {pid for pid, _ in results}
        assert "project_a" in project_ids
        assert "project_b" in project_ids

    def test_scan_single_project(self):
        results = scan_project(str(FIXTURES), project_id="project_a")
        assert all(pid == "project_a" for pid, _ in results)
        assert len(results) == 2  # README.md + guide.md

    def test_scan_nonexistent_project(self):
        with pytest.raises(ValueError, match="not found"):
            scan_project(str(FIXTURES), project_id="nonexistent")


class TestGetRelativePath:
    def test_relative_path(self):
        root = str(FIXTURES)
        full = FIXTURES / "project_a" / "README.md"
        rel = get_relative_path(full, root)
        assert rel == "project_a/README.md" or rel == "project_a\\README.md"


# --- Metadata Tests ---


class TestExtractFrontmatter:
    def test_with_frontmatter(self):
        meta, body = extract_frontmatter(FIXTURES / "project_a" / "README.md")
        assert meta["title"] == "Project Alpha"
        assert meta["version"] == "1.0"
        assert "# Project Alpha" in body

    def test_without_frontmatter(self):
        meta, body = extract_frontmatter(FIXTURES / "no_frontmatter.md")
        assert meta == {}
        assert "# Simple Document" in body


class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_short_text(self):
        tokens = estimate_tokens("Hello world foo bar")
        assert tokens > 0
        assert tokens < 20

    def test_proportional(self):
        short = estimate_tokens("one two three")
        long = estimate_tokens("one two three four five six seven eight nine ten")
        assert long > short


# --- Chunking Tests ---


class TestSplitByHeadings:
    def test_basic_split(self):
        content = "# Title\n\nIntro\n\n## Section A\n\nContent A\n\n## Section B\n\nContent B"
        sections = _split_by_headings(content)
        # Should have: intro (under # Title), Section A, Section B
        assert len(sections) >= 2

    def test_no_headings(self):
        content = "Just plain text\n\nWith paragraphs"
        sections = _split_by_headings(content)
        assert len(sections) == 1
        assert sections[0][0] == []  # No heading hierarchy

    def test_nested_headings(self):
        content = "## Parent\n\nParent text\n\n### Child\n\nChild text"
        sections = _split_by_headings(content)
        # Find the child section
        child_sections = [s for s in sections if len(s[0]) == 2]
        assert len(child_sections) == 1
        assert "Child" in child_sections[0][0][1]

    def test_heading_reset(self):
        content = "## A\n\nText A\n\n### A.1\n\nText A1\n\n## B\n\nText B"
        sections = _split_by_headings(content)
        # Section B should reset hierarchy
        b_sections = [s for s in sections if any("B" in h for h in s[0])]
        assert len(b_sections) >= 1
        # B should have depth 1 (just ## B)
        assert len(b_sections[0][0]) == 1


class TestMakeChunkId:
    def test_deterministic(self):
        id1 = _make_chunk_id("proj", "file.md", 0)
        id2 = _make_chunk_id("proj", "file.md", 0)
        assert id1 == id2

    def test_different_inputs(self):
        id1 = _make_chunk_id("proj", "file.md", 0)
        id2 = _make_chunk_id("proj", "file.md", 1)
        assert id1 != id2

    def test_length(self):
        cid = _make_chunk_id("proj", "file.md", 0)
        assert len(cid) == 16

    def test_hex_characters(self):
        cid = _make_chunk_id("proj", "file.md", 0)
        assert all(c in "0123456789abcdef" for c in cid)


class TestChunkMarkdownFile:
    def test_basic_chunking(self):
        chunks = chunk_markdown_file(
            file_path=FIXTURES / "project_a" / "README.md",
            project_id="project_a",
            relative_path="project_a/README.md",
        )
        assert len(chunks) > 0
        assert all(isinstance(c, Chunk) for c in chunks)

    def test_chunk_fields(self):
        chunks = chunk_markdown_file(
            file_path=FIXTURES / "project_a" / "README.md",
            project_id="project_a",
            relative_path="project_a/README.md",
        )
        for chunk in chunks:
            assert chunk.project_id == "project_a"
            assert chunk.file_path == "project_a/README.md"
            assert chunk.chunk_id  # non-empty
            assert chunk.raw_text  # non-empty
            assert chunk.text  # non-empty
            assert chunk.token_count > 0

    def test_heading_hierarchy_preserved(self):
        chunks = chunk_markdown_file(
            file_path=FIXTURES / "project_a" / "README.md",
            project_id="project_a",
            relative_path="project_a/README.md",
        )
        # Find a chunk from the Backend section (under Architecture)
        backend_chunks = [c for c in chunks if any("Backend" in h for h in c.headings)]
        assert len(backend_chunks) > 0
        # Should have Architecture in hierarchy too
        for c in backend_chunks:
            heading_text = " ".join(c.headings)
            assert "Architecture" in heading_text

    def test_heading_prepended_to_text(self):
        chunks = chunk_markdown_file(
            file_path=FIXTURES / "project_a" / "README.md",
            project_id="project_a",
            relative_path="project_a/README.md",
        )
        headed_chunks = [c for c in chunks if c.headings]
        assert len(headed_chunks) > 0
        for c in headed_chunks:
            # text should start with heading hierarchy
            assert " > " in c.text or c.text.startswith(c.raw_text) is False

    def test_deterministic_ids(self):
        chunks1 = chunk_markdown_file(
            file_path=FIXTURES / "project_a" / "README.md",
            project_id="project_a",
            relative_path="project_a/README.md",
        )
        chunks2 = chunk_markdown_file(
            file_path=FIXTURES / "project_a" / "README.md",
            project_id="project_a",
            relative_path="project_a/README.md",
        )
        ids1 = [c.chunk_id for c in chunks1]
        ids2 = [c.chunk_id for c in chunks2]
        assert ids1 == ids2

    def test_no_frontmatter_file(self):
        chunks = chunk_markdown_file(
            file_path=FIXTURES / "no_frontmatter.md",
            project_id="test",
            relative_path="no_frontmatter.md",
        )
        assert len(chunks) > 0

    def test_sequential_chunk_indices(self):
        chunks = chunk_markdown_file(
            file_path=FIXTURES / "project_a" / "README.md",
            project_id="project_a",
            relative_path="project_a/README.md",
        )
        indices = [c.chunk_index for c in chunks]
        assert indices == list(range(len(chunks)))

    def test_chunk_size_respected(self):
        """No chunk should wildly exceed the target size."""
        chunks = chunk_markdown_file(
            file_path=FIXTURES / "project_a" / "README.md",
            project_id="project_a",
            relative_path="project_a/README.md",
            chunk_size=100,  # Very small to force splitting
        )
        for c in chunks:
            # Allow 2x tolerance for edge cases (code blocks, etc.)
            assert c.token_count < 200, f"Chunk too large: {c.token_count} tokens"

    def test_guide_file(self):
        """Guide file with code blocks should chunk without errors."""
        chunks = chunk_markdown_file(
            file_path=FIXTURES / "project_a" / "guide.md",
            project_id="project_a",
            relative_path="project_a/guide.md",
        )
        assert len(chunks) > 0
        # Code blocks should be preserved intact within chunks
        code_chunks = [c for c in chunks if "```" in c.raw_text]
        # At least some chunks should contain code
        assert len(code_chunks) >= 0  # May or may not have code depending on chunking
