"""Where a search result came FROM, and whether every surface agrees.

Two results in one response can be the user's own code and a vendored
dependency's, and the difference decides what the reader does next: edit it, or
work around it. The label that carries this — ``scope`` / ``scope_source`` — was
derived from a hit's POSITION in the routed collection list:

    is_framework = collection.startswith("fw_") or (collection_scoped and index > 0)

That is true only for the single-project shape the router emits most often
(own collection first, linked corpora after). Search three projects explicitly
and the second and third are reported as vendored dependencies of the first,
with their raw ``proj_<32hex>`` store name shown as the dependency's identity —
a string that names nothing the user can open, look up, or scope a search to.

So this module asserts two things end to end:

1. provenance is a property of the collection that ANSWERED, not of where that
   collection sat in a list;
2. all five retrieval surfaces — the HTTP API, the CLI, MCP direct, MCP proxy
   and dev-search — say the same thing about the same hit. A per-surface answer
   is worse than a wrong one: the user has no way to tell which to believe.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.service.owner import QdrantOwner

#: The store name that must never reach a reader. It is the exact shape
#: ``identity.project_collection_name`` mints.
COLLECTION_LEAK_RE = re.compile(r"proj_[0-9a-f]{32}")

QUERY = "invoice reconciliation and the dunning ladder for overdue accounts"

PROJECT_IDS = ("alpha", "beta", "gamma")


# --- corpora ---------------------------------------------------------------


def _write_project(root: Path, name: str) -> Path:
    """A project whose docs all answer ``QUERY``, so every project is hit."""
    proj = root / name
    (proj / "docs").mkdir(parents=True, exist_ok=True)
    (proj / "docs" / "billing.md").write_text(
        f"# {name} billing\n\n"
        "Invoice reconciliation and the dunning ladder for overdue accounts.\n"
        "Each reminder step escalates until the invoice is settled.\n",
        encoding="utf-8")
    (proj / "docs" / "arch.md").write_text(
        f"# {name} architecture\n\n"
        "Deployment notes for the invoice reconciliation service.\n",
        encoding="utf-8")
    return proj


def _write_vendored_core(root: Path) -> Path:
    """A dependency tree, at a non-conventional path so it must be DECLARED.

    ``vendor/`` is a built-in ignore pattern, so a framework there is excluded
    by convention and the interesting case never arises.
    """
    core = root / "alpha" / "platform" / "odoo"
    (core / "odoo").mkdir(parents=True, exist_ok=True)
    (core / "odoo" / "release.py").write_text(
        "version_info = (19, 0, 0, 'final', 0, 'f')\n", encoding="utf-8")
    (core / "odoo-bin").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (core / "repos_heads").write_text("odoo aaa\n", encoding="utf-8")
    for i in range(3):
        d = core / "addons" / f"core_mod_{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "README.md").write_text(
            f"# Core module {i}\n\n"
            "Framework recordset caching and ORM cursor internals.\n",
            encoding="utf-8")
    return core


def _settings(root: Path, projects, strategy: str) -> Settings:
    return Settings(
        content_root=str(root),
        qdrant_path=str(root / "qdrant"),
        state_db=str(root / "state.db"),
        data_dir=str(root / "data"),
        collection_strategy=strategy,
        projects=projects,
        # Fixed low threshold so every surface keeps the same hits and the test
        # is about LABELS, not about which chunk squeaked past 0.3.
        score_threshold=0.05,
    )


@pytest.fixture(scope="module")
def three_projects():
    """Three sibling projects, per-project layout, no dependencies anywhere."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        projects = [
            ProjectConfig(id=name, path=str(_write_project(root, name)), mode="general")
            for name in PROJECT_IDS
        ]
        settings = _settings(root, projects, "per_project")
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        try:
            owner.run_full_index()
            yield owner, settings
        finally:
            owner.close()


@pytest.fixture(scope="module")
def project_with_dependency():
    """One project that vendors a framework it declares as a dependency."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj = _write_project(root, "alpha")
        _write_vendored_core(root)
        settings = _settings(
            root,
            [ProjectConfig(id="alpha", path=str(proj), mode="general",
                           dependency_paths=["platform/odoo"])],
            "per_project",
        )
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        try:
            owner.run_full_index()
            owner.sync_frameworks()
            yield owner, settings
        finally:
            owner.close()


@pytest.fixture(scope="module")
def shared_layout():
    """The legacy single-collection layout, which must not move."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        projects = [
            ProjectConfig(id=name, path=str(_write_project(root, name)), mode="general")
            for name in PROJECT_IDS
        ]
        settings = _settings(root, projects, "shared")
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        try:
            owner.run_full_index()
            yield owner, settings
        finally:
            owner.close()


# --- what a hit says about itself ------------------------------------------


def test_a_three_project_search_calls_all_three_hits_project(three_projects):
    """None of these projects vendors anything, so nothing is a framework hit.

    Pre-fix, the routed list ``[proj_alpha, proj_beta, proj_gamma]`` was read
    positionally: alpha's hits were ``project`` and beta's and gamma's were
    ``framework`` — the user told their own code came from a dependency they do
    not have.
    """
    owner, _ = three_projects
    payload = owner.search_formatted(QUERY, project_ids=list(PROJECT_IDS), top_k=30)
    results = payload["results"]

    assert results, "nothing came back to label"
    represented = {r["project_id"] for r in results}
    assert represented == set(PROJECT_IDS), (
        f"the corpus did not exercise every collection; got {sorted(represented)}"
    )

    mislabelled = {(r["project_id"], r["file_path"], r["scope"])
                   for r in results if r["scope"] != "project"}
    assert not mislabelled, (
        "a project's own content was attributed to a vendored dependency: "
        f"{sorted(mislabelled)}"
    )


def test_a_project_hit_names_the_project_not_the_store(three_projects):
    """``scope_source`` has to be something the reader can act on.

    The store name ``proj_<32hex>`` identifies a directory on disk; it cannot be
    looked up, opened, or passed back as ``project=``.
    """
    owner, _ = three_projects
    results = owner.search_formatted(QUERY, project_ids=list(PROJECT_IDS),
                                     top_k=30)["results"]

    assert results
    for r in results:
        assert r["scope_source"] == r["project_id"], (
            f"{r['file_path']} is attributed to {r['scope_source']!r}, "
            f"not to the project {r['project_id']!r} that owns it"
        )


def test_a_framework_hit_and_a_project_hit_are_told_apart(project_with_dependency):
    """The distinction the label exists for, with a real vendored corpus."""
    owner, _ = project_with_dependency

    own = owner.search_formatted("dunning ladder invoice reconciliation",
                                 project_id="alpha", top_k=30)["results"]
    project_hits = [r for r in own if r["project_id"] == "alpha"]
    assert project_hits, "the project's own content was unreachable"
    for r in project_hits:
        assert r["scope"] == "project", f"{r['file_path']} was called a framework hit"
        assert r["scope_source"] == "alpha"

    vendored = owner.search_formatted("recordset caching ORM cursor internals",
                                      project_id="alpha", top_k=30)["results"]
    framework_hits = [r for r in vendored if r["scope"] == "framework"]
    assert framework_hits, "the linked corpus was unreachable from the project"
    for r in framework_hits:
        assert r["scope_source"].startswith("fw_"), (
            "a framework hit must name WHICH dependency answered"
        )


def test_no_search_response_leaks_a_collection_identifier(three_projects):
    """A regex over the whole serialized response — every field, not the ones
    we remembered to check."""
    owner, _ = three_projects

    for payload in (
        owner.search_formatted(QUERY, project_ids=list(PROJECT_IDS), top_k=30),
        owner.search_project_context(QUERY, project_ids=list(PROJECT_IDS), top_k=30),
    ):
        blob = json.dumps(payload, default=str)
        leaked = sorted(set(COLLECTION_LEAK_RE.findall(blob)))
        assert not leaked, f"the response exposes internal store names: {leaked}"


def test_dev_search_labels_agree_with_search(three_projects):
    """Project Context Mode is a different pipeline over the same collections.

    It reranks and layers, but a chunk's provenance cannot depend on which
    pipeline fetched it.
    """
    owner, _ = three_projects
    flat = owner.search_formatted(QUERY, project_ids=list(PROJECT_IDS), top_k=30)
    dev = owner.search_project_context(QUERY, project_ids=list(PROJECT_IDS), top_k=30)

    assert dev["results"], "dev-search returned nothing to compare"
    by_file = {(r["project_id"], r["file_path"]): (r["scope"], r["scope_source"])
               for r in flat["results"]}
    for r in dev["results"]:
        key = (r["project_id"], r["file_path"])
        if key in by_file:
            assert (r["scope"], r["scope_source"]) == by_file[key], (
                f"dev-search and search disagree about {key}"
            )
        assert r["scope"] == "project"


# --- every surface, one answer ---------------------------------------------


def _provenance(results) -> dict:
    """``(project, file) -> (scope, scope_source)`` from a structured surface."""
    return {(r["project_id"], r["file_path"]): (r["scope"], r["scope_source"])
            for r in results}


@pytest.fixture
def surfaces(three_projects, monkeypatch):
    """The four externally reachable retrieval surfaces, wired to one index.

    Real code paths throughout: the API through Starlette's TestClient, MCP
    proxy through that same client (its `.get` is what the proxy calls), MCP
    direct and the CLI through their own module entry points. Only the two
    process boundaries are substituted — the Qdrant client and the service
    probe — because those are what a test cannot have.
    """
    from starlette.testclient import TestClient

    import ragtools.cli as cli_module
    from ragtools.integration import mcp_server
    from ragtools.service import app as app_module
    from ragtools.service.app import create_app

    owner, settings = three_projects

    monkeypatch.setattr(app_module, "_owner", owner)
    monkeypatch.setattr(app_module, "_settings", settings)

    # MCP direct: its own module state, plus the one boundary it cannot own.
    monkeypatch.setattr(mcp_server, "_settings", settings)
    monkeypatch.setattr(mcp_server, "_encoder", owner.encoder)
    monkeypatch.setattr(mcp_server, "_init_error", None)
    monkeypatch.setattr(mcp_server, "_get_direct_client", lambda: owner.client)

    # CLI: no service running, same store.
    monkeypatch.setattr(cli_module, "_get_settings", lambda: settings)
    monkeypatch.setattr(cli_module, "_probe_service", lambda *a, **k: False)
    monkeypatch.setattr(Settings, "get_qdrant_client", lambda self: owner.client)

    with TestClient(create_app(), raise_server_exceptions=True) as tc:
        yield tc, mcp_server, cli_module


def _api_search(tc):
    r = tc.get("/api/search", params={"query": QUERY,
                                      "projects": ",".join(PROJECT_IDS),
                                      "top_k": 30})
    assert r.status_code == 200, r.text
    return r.json()


def _cli_search(cli_module):
    from typer.testing import CliRunner

    result = CliRunner().invoke(cli_module.app, ["search", QUERY, "-k", "30"])
    assert result.exit_code == 0, result.output
    return result.output


def test_every_surface_answers_the_same_provenance(surfaces):
    """One query, four surfaces, one story.

    Pre-fix these disagreed twice over: MCP direct and the CLI built an unrouted
    Searcher and so found NOTHING on a per-project install, while the API and
    the proxy returned hits whose labels were positional.
    """
    tc, mcp_server, cli_module = surfaces

    api = _provenance(_api_search(tc)["results"])

    monkeypatched_mode = mcp_server._mode
    try:
        mcp_server._mode = "direct"
        direct = mcp_server.search_knowledge_base(
            QUERY, projects=list(PROJECT_IDS), top_k=30, structured=True)

        mcp_server._mode = "proxy"
        mcp_server._http_client = tc
        proxy = mcp_server.search_knowledge_base(
            QUERY, projects=list(PROJECT_IDS), top_k=30, structured=True)
    finally:
        mcp_server._mode = monkeypatched_mode
        mcp_server._http_client = None

    assert api, "the API returned nothing to compare"
    for label, payload in (("MCP direct", direct), ("MCP proxy", proxy)):
        assert isinstance(payload, dict), f"{label} did not return a structured payload"
        assert payload["results"], (
            f"{label} returned no results while the API returned {len(api)} — "
            "the surfaces are reading different collections"
        )
        for key, provenance in _provenance(payload["results"]).items():
            assert key in api, f"{label} returned {key}, which the API did not"
            assert provenance == api[key], (
                f"{label} says {key} is {provenance}; the API says {api[key]}"
            )

    # The CLI is a text surface: it must still tell the two apart, and here
    # nothing is vendored, so nothing may be tagged as a dependency.
    output = _cli_search(cli_module)
    assert "No results found" not in output, (
        "the CLI found nothing while the API returned "
        f"{len(api)} results from the same store"
    )
    assert "[framework:" not in output, (
        "the CLI attributed a project's own file to a vendored dependency"
    )


def test_no_surface_prints_a_store_name(surfaces):
    """The leak check, applied to what each surface actually hands a reader."""
    tc, mcp_server, cli_module = surfaces

    serialized = {"api": json.dumps(_api_search(tc), default=str)}

    monkeypatched_mode = mcp_server._mode
    try:
        mcp_server._mode = "direct"
        serialized["mcp direct (structured)"] = json.dumps(
            mcp_server.search_knowledge_base(QUERY, projects=list(PROJECT_IDS),
                                             top_k=30, structured=True), default=str)
        serialized["mcp direct (text)"] = str(
            mcp_server.search_knowledge_base(QUERY, projects=list(PROJECT_IDS),
                                             top_k=30))
        serialized["mcp direct (dev)"] = str(
            mcp_server.search_project_context(QUERY, projects=list(PROJECT_IDS),
                                              top_k=30))

        mcp_server._mode = "proxy"
        mcp_server._http_client = tc
        serialized["mcp proxy (structured)"] = json.dumps(
            mcp_server.search_knowledge_base(QUERY, projects=list(PROJECT_IDS),
                                             top_k=30, structured=True), default=str)
        serialized["mcp proxy (text)"] = str(
            mcp_server.search_knowledge_base(QUERY, projects=list(PROJECT_IDS),
                                             top_k=30))
    finally:
        mcp_server._mode = monkeypatched_mode
        mcp_server._http_client = None

    serialized["cli"] = _cli_search(cli_module)

    offenders = {name: sorted(set(COLLECTION_LEAK_RE.findall(blob)))
                 for name, blob in serialized.items()
                 if COLLECTION_LEAK_RE.search(blob)}
    assert not offenders, f"internal store names reached the reader: {offenders}"


# --- the layout that must not move -----------------------------------------


def test_the_shared_layout_still_labels_every_hit_project(shared_layout):
    """Regression guard, not a probe: ``shared`` is still a supported layout.

    One collection holds every project, so no hit is ever a framework hit and
    there is no store whose name could leak. This passed before the fix and has
    to keep passing after it.
    """
    owner, _ = shared_layout
    payload = owner.search_formatted(QUERY, project_ids=list(PROJECT_IDS), top_k=30)
    results = payload["results"]

    assert {r["project_id"] for r in results} == set(PROJECT_IDS)
    assert all(r["scope"] == "project" for r in results)
    assert all(r["scope_source"] == "" for r in results), (
        "under `shared` a chunk's owner comes from its payload, not from the "
        "collection — inventing a source here would be a claim the store "
        "cannot support"
    )
    assert not COLLECTION_LEAK_RE.search(json.dumps(payload, default=str))
