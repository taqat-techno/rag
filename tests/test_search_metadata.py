"""Search-result metadata contract (W4): /api/search and /api/dev-search results
must carry source_class + line anchors + chunk_type/language, not just 6 fields.

Generic: any agent reading search output needs to tell owned vs generated and
jump to file:line. The data is already on the SearchResult — this proves the
serializers expose it.
"""

import tempfile
from pathlib import Path

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.service.owner import QdrantOwner

_REQUIRED = {"source_class", "line_start", "line_end", "chunk_type"}


@pytest.fixture(scope="module")
def owner():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        (root / "app.ts").write_text(
            "export function authenticateUser(name: string) {\n"
            "  // verify the user credentials and return a session token\n"
            "  return createSession(name);\n}\n",
            encoding="utf-8",
        )
        (root / "README.md").write_text(
            "# Auth\n\nThe service authenticates users and returns a session token.\n",
            encoding="utf-8",
        )
        settings = Settings(
            state_db=str(Path(tmp) / "s.db"),
            projects=[ProjectConfig(id="p", path=str(root), mode="general")],
        )
        o = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        o.run_full_index()
        yield o


def test_search_formatted_results_include_metadata(owner):
    res = owner.search_formatted("authenticate user session token", project_id="p", top_k=5)
    assert res["results"], "expected results"
    keys = set(res["results"][0].keys())
    assert _REQUIRED <= keys, f"missing {_REQUIRED - keys}"


def test_dev_search_results_include_metadata(owner):
    res = owner.search_project_context("authenticate user session token", project_id="p", top_k=5)
    assert res["results"], "expected results"
    keys = set(res["results"][0].keys())
    assert _REQUIRED <= keys, f"missing {_REQUIRED - keys}"
