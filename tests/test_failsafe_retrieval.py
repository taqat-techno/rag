"""Fail-safe retrieval (P0-B / W17): separate "not indexed" from "not found".

A code-intent search must never let an agent read an empty result as "the
feature does not exist". When code is not indexed (Docs mode) the agent is told
so explicitly; when code IS indexed, an empty result is an honest "not found".
"""

from ragtools.config import ProjectConfig, Settings
from ragtools.retrieval.dev_pipeline import dev_search
from ragtools.retrieval.formatter import format_dev_context


class _EmptySearcher:
    def __init__(self, settings):
        self.settings = settings
    def search(self, query=None, project_id=None, project_ids=None, top_k=None,
               score_threshold=None, chunk_types=None):
        return []


def test_docs_mode_empty_marks_not_indexed(tmp_path):
    s = Settings(projects=[ProjectConfig(id="d", path=str(tmp_path), mode="docs")])
    out = dev_search(_EmptySearcher(s), "implement the webhook handler", project_id="d")
    assert out.code_indexed is False
    text = format_dev_context(out.results, "implement the webhook handler",
                              out.triggers, out.warnings, out.code_indexed)
    assert "not indexed" in text.lower()
    assert "Docs mode" in text  # docs-mode warning surfaced


def test_code_mode_empty_marks_not_found(tmp_path):
    s = Settings(projects=[ProjectConfig(id="c", path=str(tmp_path), mode="code")])
    out = dev_search(_EmptySearcher(s), "implement the frobnicate handler", project_id="c")
    assert out.code_indexed is True
    assert out.warnings == []  # no docs-mode warning for a code project
    text = format_dev_context(out.results, "implement the frobnicate handler",
                              out.triggers, out.warnings, out.code_indexed)
    low = text.lower()
    assert "indexed" in low
    assert "not found" in low or "does not" in low or "likely" in low
    # An honest not-found must NOT claim the code isn't indexed.
    assert "not indexed" not in low


def test_general_mode_empty_marks_not_found(tmp_path):
    s = Settings(projects=[ProjectConfig(id="g", path=str(tmp_path), mode="general")])
    out = dev_search(_EmptySearcher(s), "implement the frobnicate handler", project_id="g")
    assert out.code_indexed is True


def test_unscoped_empty_is_unknown_coverage(tmp_path):
    s = Settings(projects=[ProjectConfig(id="a", path=str(tmp_path), mode="code")])
    out = dev_search(_EmptySearcher(s), "implement something", project_id=None)
    assert out.code_indexed is None  # can't assert coverage across all projects
