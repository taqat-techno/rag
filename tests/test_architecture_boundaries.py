"""Architectural boundaries that v3.5 must not let regress.

Two invariants, both learned from shipped defects:

1. Per-collection iteration must not be written as if there were one
   collection. v3.4 shipped the same bug twice — a ``break`` that left the
   *collection* loop instead of the *scroll* loop — in the Semantic Map
   (13 of 15 projects silently absent) and in MCP ``list_projects`` (only the
   first collection enumerated). Both sites carried a comment asserting the
   opposite intent, so review did not catch it either time.

2. The legacy single-collection setting must not be read as if it were the
   index. ``collection_strategy="shared"`` is still a supported layout, so a
   site that *branches* on strategy is legitimate; a site that uses the name
   *unconditionally* is not. That distinction is the whole job here.

Parsed, not grepped — the same reasoning as
``test_platform_adapters.test_no_platform_branch_survives_outside_this_package``:
a regex version matches its own explanatory prose and turns the build red for
a comment, which teaches people to delete the comment.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "ragtools"


# --- 1. collection iteration ----------------------------------------------

#: Callables whose result is a list of collections. A ``for`` over one of these
#: is iterating the knowledge base, not a local list.
_COLLECTION_SOURCES = {
    "all_collections",
    "active_collections",
    "read_collections",
    "describe",
}


def _iterates_collections(node: ast.For) -> bool:
    """Whether this ``for`` walks the routed set of collections."""
    for sub in ast.walk(node.iter):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            if sub.func.attr in _COLLECTION_SOURCES:
                return True
        # `targets` is the established local name for the routed list in
        # searcher.py and map_data.py.
        if isinstance(sub, ast.Name) and sub.id in {"targets", "collections"}:
            return True
    return False


def _unnested_breaks(loop: ast.For) -> list[int]:
    """Line numbers of ``break``s that terminate *this* loop.

    A ``break`` nested inside an inner ``for``/``while`` belongs to that inner
    loop and is fine. One sitting directly in this loop's body abandons every
    remaining collection.
    """
    found: set[int] = set()

    def walk(body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.For, ast.While)):
                continue  # a break in there belongs to that loop
            if isinstance(stmt, ast.Break):
                found.add(stmt.lineno)
                continue
            # Only the statement-holding attributes, each exactly once. Walking
            # via iter_child_nodes AS WELL would visit every body twice and
            # report each offender twice.
            for attr in ("body", "orelse", "finalbody"):
                inner = getattr(stmt, attr, None)
                if isinstance(inner, list):
                    walk([s for s in inner if isinstance(s, ast.stmt)])
            for handler in getattr(stmt, "handlers", []) or []:
                walk(handler.body)

    walk(loop.body)
    return sorted(found)


def test_no_break_abandons_the_remaining_collections():
    """A loop over collections must finish them all.

    The failure this prevents is silent and total: the map reported "385 files
    across 2 projects" as fact while 5,383 files existed across 15, and
    ``list_projects`` reported one project on a 15-project install. Neither
    raised, logged, or degraded — the remaining collections were simply never
    queried.

    Skip a collection with ``continue`` (and record why). Never ``break``.
    """
    offenders: set[str] = set()

    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover - not ours to police
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.For) and _iterates_collections(node):
                for lineno in _unnested_breaks(node):
                    offenders.add(f"{rel}:{lineno}")

    assert not offenders, (
        "these `break`s abandon every remaining collection; use `continue` and "
        "record the reason instead: " + ", ".join(sorted(offenders))
    )


# --- 2. the legacy collection setting -------------------------------------

#: Modules permitted to read ``settings.collection_name``. Each entry is a
#: standing justification, not a grandfather clause:
#:
#: * ``collection_router.py`` — the seam itself. It owns the ``shared`` branch;
#:   being the one place that reads the setting is its entire purpose.
#: * ``upgrade/relayout.py`` — the deletion ALLOW-list. It must know the v2
#:   name to prove this installation created a collection before dropping it.
#:   It cannot ask the router, which deliberately omits the legacy name under
#:   ``per_project``.
#: * ``index_identity.py`` — the store fingerprint. Records the name beside
#:   ``collection_strategy`` so a layout flip invalidates the state DB.
#:   Structurally a label, never a query.
#: * ``config.py`` — the declaration.
#: * ``retrieval/searcher.py`` — the shared-strategy fallback. ``shared`` is
#:   still a supported layout and there the configured name IS the collection,
#:   so this read is correct compatibility. It is permitted ONLY because it is
#:   branched on ``collection_strategy``; under ``per_project`` the same
#:   function refuses rather than guessing. If that branch is ever removed,
#:   remove this entry with it.
_ALLOWED = {
    "collection_router.py",
    "upgrade/relayout.py",
    "index_identity.py",
    "config.py",
    "retrieval/searcher.py",
}

#: Receivers that make ``.collection_name`` the legacy SETTING rather than a
#: registry record's field. ``rec.collection_name`` / ``record.collection_name``
#: / ``fw.collection_name`` are the per-project name and are always fine.
_SETTINGS_RECEIVERS = {"settings", "_settings", "s"}


def _reads_legacy_setting(tree: ast.AST) -> list[int]:
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "collection_name":
            value = node.value
            # settings.collection_name / _settings.collection_name
            if isinstance(value, ast.Name) and value.id in _SETTINGS_RECEIVERS:
                hits.append(node.lineno)
            # self.settings.collection_name / owner._settings.collection_name
            elif isinstance(value, ast.Attribute) and value.attr in _SETTINGS_RECEIVERS:
                hits.append(node.lineno)
        # the bare literal is just as load-bearing
        if isinstance(node, ast.Constant) and node.value == "markdown_kb":
            hits.append(node.lineno)
    return hits


def test_the_legacy_collection_setting_is_read_only_where_it_is_justified():
    """Under ``per_project`` this name identifies nothing that exists.

    On the installed v3.4 machine there was no ``markdown_kb`` collection at
    all — 15 ``proj_<uuid>`` collections and no aliases — yet three production
    paths still queried it: job verification (so every purge reported
    "could not be verified"), ``rag doctor`` (which called a healthy
    15-collection install broken), and the relayout disk preflight (which
    sized an empty corpus). Reporting surfaces additionally showed the name to
    users as the knowledge base's identity.

    Ask ``CollectionRouter``. If you genuinely need the legacy name, add the
    module here with a reason.
    """
    offenders: list[str] = []

    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in _ALLOWED:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover - not ours to police
            continue
        for lineno in _reads_legacy_setting(tree):
            offenders.append(f"{rel}:{lineno}")

    assert not offenders, (
        "the legacy collection setting is read outside the routing authority; "
        "resolve it through CollectionRouter: " + ", ".join(offenders)
    )
