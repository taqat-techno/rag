"""S1 / A2 — Searcher enforces fail-closed scope end to end.

Regression guard for the live-reproduced leak: ``project_ids=[]`` /
``["",""]`` used to collapse to a global search. These prove the Searcher now
refuses a requested-but-empty scope *before embedding*, while an explicitly
unscoped call (allow_unscoped, the default at this primitive) still passes
through — the seam the router/profile layer tightens at the boundary.

Uses dummy client/encoder: a refusal must happen before either is touched.
"""

import pytest

from ragtools.config import Settings
from ragtools.retrieval.scope import ScopeUnresolvedError
from ragtools.retrieval.searcher import Searcher


class _NoEncoder:
    """Fails loudly if reached — proves scope is resolved before embedding."""

    def encode_query(self, query):  # noqa: D401 - test stub
        raise AssertionError("reached embedding: scope was not resolved first")


def _searcher() -> Searcher:
    return Searcher(client=object(), encoder=_NoEncoder(), settings=Settings())


def test_searcher_refuses_empty_project_ids_before_embedding():
    with pytest.raises(ScopeUnresolvedError):
        _searcher().search("q", project_ids=[])


def test_searcher_refuses_blank_only_project_ids():
    with pytest.raises(ScopeUnresolvedError):
        _searcher().search("q", project_ids=["", "  "])


def test_searcher_refuses_blank_project_id():
    with pytest.raises(ScopeUnresolvedError):
        _searcher().search("q", project_id="   ")


def test_searcher_unscoped_global_passes_scope_by_default():
    """both-None + default opt-in still reaches embedding (global preserved)."""
    with pytest.raises(AssertionError, match="reached embedding"):
        _searcher().search("q")


def test_searcher_fail_closed_when_optin_disabled():
    """The boundary seam: with opt-in off, an unscoped call refuses."""
    with pytest.raises(ScopeUnresolvedError):
        _searcher().search("q", allow_unscoped=False)
