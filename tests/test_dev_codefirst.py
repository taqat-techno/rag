"""dev-search must be code-first by design, not gated on action-verb intent (W1/W2).

Live evidence: a descriptive code query ("SMS dispatch gateway service") scored
is_dev_request=False -> flat strategy -> byte-identical to /api/search, wiki on top.
The dedicated dev endpoint should ALWAYS prefer owned source and guarantee a code
quota, regardless of phrasing. Generic across stacks.
"""

from ragtools.config import Settings
from ragtools.models import SearchResult
from ragtools.retrieval.dev_pipeline import dev_search


def _sr(score, chunk_type, file_path):
    return SearchResult(
        chunk_id=f"{file_path}:{score}", score=score, text="t", raw_text="t",
        file_path=file_path, project_id="p", confidence="LOW", chunk_type=chunk_type,
    )


class _Layered:
    """Returns a high-scoring doc and a lower-scoring code file per layer."""
    def __init__(self, settings):
        self.settings = settings
    def search(self, query=None, project_id=None, project_ids=None, top_k=None,
               score_threshold=None, chunk_types=None, exclude_generated=True):
        if "code" in (chunk_types or []):
            return [_sr(0.50, "code", "services/sms.service.ts")]
        if "documentation" in (chunk_types or []):
            return [_sr(0.80, "documentation", "wiki/SMS-Notifications.md")]
        return []


def test_force_dev_lifts_code_over_higher_scoring_doc():
    out = dev_search(_Layered(Settings(score_threshold=0.3, top_k=5)),
                     "SMS dispatch gateway service", project_id="p", force_dev=True)
    paths = [r.file_path for r in out.results]
    assert "services/sms.service.ts" in paths
    assert paths.index("services/sms.service.ts") < paths.index("wiki/SMS-Notifications.md")
    assert out.is_dev_request is True


def test_descriptive_query_without_force_stays_flat():
    # No force + no action verb -> flat strategy -> the higher-scoring doc leads
    # (this is exactly the pre-fix behavior the dev endpoint must override).
    out = dev_search(_Layered(Settings(score_threshold=0.3, top_k=5)),
                     "SMS dispatch gateway service", project_id="p", force_dev=False)
    assert out.is_dev_request is False
    assert out.results[0].file_path == "wiki/SMS-Notifications.md"


def test_force_dev_guarantees_code_quota_amid_many_docs():
    class _ManyDocs:
        def __init__(self, settings): self.settings = settings
        def search(self, query=None, chunk_types=None, **k):
            if "code" in (chunk_types or []):
                return [_sr(0.45, "code", "services/a.ts"), _sr(0.44, "code", "services/b.ts")]
            if "documentation" in (chunk_types or []):
                return [_sr(0.85 - i * 0.01, "documentation", f"wiki/doc{i}.md") for i in range(10)]
            return []
    out = dev_search(_ManyDocs(Settings(score_threshold=0.3, top_k=6)),
                     "reservation flow capacity", project_id="p", force_dev=True)
    code = [r for r in out.results if r.chunk_type == "code"]
    assert len(code) >= 2  # both owned code files survive the doc flood
