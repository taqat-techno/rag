"""Generated-artifact exclusion + search-time drop (W3 / P0-2).

Generic: build/coverage mirrors (which embed real source) must never be indexed
by default, and must never displace or outrank owned source in results — on any
stack. Defense in depth: built-in ignore for the dir + search-time drop for any
that are already in the index (works without re-index).
"""

from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.indexing.indexer import ensure_collection, index_file
from ragtools.retrieval.searcher import Searcher


def test_coverage_dir_ignored_by_builtin(tmp_path):
    from ragtools.ignore import IgnoreRules
    rules = IgnoreRules(content_root=tmp_path)
    assert rules.is_ignored(tmp_path / "coverage" / "app.ts.html")
    assert rules.is_ignored(tmp_path / "coverage" / "lcov-report" / "index.html")
    assert not rules.is_ignored(tmp_path / "src" / "app.ts")


def _setup(tmp_path):
    settings = Settings(state_db=str(tmp_path / "s.db"))
    client = Settings.get_memory_client()
    encoder = Encoder(settings.embedding_model)
    ensure_collection(client, settings.collection_name, encoder.dimension)
    body = "export function reserveSlot() { return checkAvailability(); }\n"
    owned = tmp_path / "svc.ts"
    owned.write_text(body, encoding="utf-8")
    index_file(client=client, encoder=encoder, collection_name=settings.collection_name,
               project_id="p", file_path=owned, relative_path="p/services/svc.ts")
    gen = tmp_path / "svc.ts.html"
    gen.write_text("<html><body>" + body + "</body></html>\n", encoding="utf-8")
    index_file(client=client, encoder=encoder, collection_name=settings.collection_name,
               project_id="p", file_path=gen, relative_path="p/coverage/services/svc.ts.html")
    return Searcher(client=client, encoder=encoder, settings=settings)


def test_searcher_drops_generated_results(tmp_path):
    searcher = _setup(tmp_path)
    paths = [r.file_path for r in searcher.search(
        query="reserve slot availability", top_k=10, score_threshold=0.0)]
    assert "p/services/svc.ts" in paths
    assert all("coverage" not in p for p in paths), paths


def test_searcher_can_opt_in_to_generated(tmp_path):
    searcher = _setup(tmp_path)
    paths = [r.file_path for r in searcher.search(
        query="reserve slot availability", top_k=10, score_threshold=0.0, exclude_generated=False)]
    assert any("coverage" in p for p in paths)
