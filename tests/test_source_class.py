"""Source-class classification: project-owned vs external-dependency vs generated.

Generic (stack-agnostic) realization of the weakness-analysis "owned-vs-dependency"
axis (P0-A / W2/W3). Detection is generic: conventional dependency dirs, build
output, git submodules, and a per-project `dependency_paths` list a user/profile
declares — NEVER framework-specific rules in core.
"""

import pytest

from ragtools.source_class import (
    classify_source_class, dependency_spec, parse_gitmodules,
    OWNED, DEPENDENCY, GENERATED, SECRET,
)


def test_owned_is_default():
    assert classify_source_class("src/app/main.py") == OWNED
    assert classify_source_class("project-addons/relief/models/x.py") == OWNED


def test_conventional_dependency_dirs():
    assert classify_source_class("backend/node_modules/lib/x.js") == DEPENDENCY
    assert classify_source_class("x/site-packages/pkg/y.py") == DEPENDENCY
    assert classify_source_class("go/vendor/foo/bar.go") == DEPENDENCY
    assert classify_source_class("ios/Pods/Lib/a.swift") == DEPENDENCY


def test_generated_output():
    assert classify_source_class("frontend/dist/app.js") == GENERATED
    assert classify_source_class("web/app.min.js") == GENERATED
    assert classify_source_class("svc/target/classes/A.class") == GENERATED


def test_secret_takes_precedence():
    assert classify_source_class(".env") == SECRET
    assert classify_source_class("config/credentials.json") == SECRET


def test_declared_dependency_paths(tmp_path):
    # A co-located framework core (Odoo is one example) declared generically.
    spec = dependency_spec(tmp_path, ("odoo/", "addons/legacy/"))
    assert classify_source_class("odoo/addons/web/models/x.py", spec) == DEPENDENCY
    assert classify_source_class("addons/legacy/m/y.py", spec) == DEPENDENCY
    assert classify_source_class("project-addons/relief/models/x.py", spec) == OWNED


def test_gitmodules_paths_are_dependencies(tmp_path):
    (tmp_path / ".gitmodules").write_text(
        '[submodule "libs/ext"]\n\tpath = libs/ext\n\turl = https://x\n',
        encoding="utf-8",
    )
    assert "libs/ext/" in parse_gitmodules(tmp_path)
    spec = dependency_spec(tmp_path)  # auto-picks up .gitmodules
    assert classify_source_class("libs/ext/src/a.py", spec) == DEPENDENCY
    assert classify_source_class("libs/owned/src/a.py", spec) == OWNED


# --- scanner exclusion: owned-only default ----------------------------------

def _mk(root, files):
    for n, c in files.items():
        f = root / n
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(c, encoding="utf-8")


def test_scanner_excludes_declared_dependency_roots(tmp_path):
    from ragtools.config import ProjectConfig
    from ragtools.indexing.scanner import scan_configured_projects
    root = tmp_path / "repo"
    root.mkdir()
    _mk(root, {"app/main.py": "x = 1\n", "framework/core.py": "y = 2\n"})
    proj = ProjectConfig(id="r", path=str(root), mode="code", dependency_paths=["framework/"])
    names = {p.resolve().relative_to(root.resolve()).as_posix()
             for _, p in scan_configured_projects([proj])}
    assert "app/main.py" in names
    assert "framework/core.py" not in names  # declared dependency excluded by default


def test_scanner_excludes_gitmodule_submodules(tmp_path):
    from ragtools.config import ProjectConfig
    from ragtools.indexing.scanner import scan_configured_projects
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitmodules").write_text(
        '[submodule "ext"]\n\tpath = ext/dep\n\turl = https://x\n', encoding="utf-8")
    _mk(root, {"app/main.py": "x = 1\n", "ext/dep/lib.py": "z = 3\n"})
    proj = ProjectConfig(id="r", path=str(root), mode="code")
    names = {p.resolve().relative_to(root.resolve()).as_posix()
             for _, p in scan_configured_projects([proj])}
    assert "app/main.py" in names
    assert "ext/dep/lib.py" not in names  # submodule excluded automatically


def test_project_config_dependency_paths_defaults_empty():
    from ragtools.config import ProjectConfig
    assert ProjectConfig(id="x", path="/p").dependency_paths == []


def test_source_class_roundtrips_through_index_and_search(tmp_path):
    """source_class is stored in the payload and surfaced on every SearchResult."""
    from ragtools.config import Settings
    from ragtools.embedding.encoder import Encoder
    from ragtools.indexing.indexer import ensure_collection, index_file
    from ragtools.retrieval.searcher import Searcher

    settings = Settings(state_db=str(tmp_path / "s.db"))
    client = Settings.get_memory_client()
    encoder = Encoder(settings.embedding_model)
    ensure_collection(client, settings.collection_name, encoder.dimension)

    owned = tmp_path / "app.py"
    owned.write_text(
        "# user authentication helper\ndef authenticate_user(name):\n    return name\n",
        encoding="utf-8",
    )
    index_file(client=client, encoder=encoder, collection_name=settings.collection_name,
               project_id="p", file_path=owned, relative_path="p/app.py")

    gen = tmp_path / "app.min.js"
    gen.write_text(
        "// authentication helper minified\nfunction authenticateUser(n){return n}\n",
        encoding="utf-8",
    )
    index_file(client=client, encoder=encoder, collection_name=settings.collection_name,
               project_id="p", file_path=gen, relative_path="p/app.min.js")

    searcher = Searcher(client=client, encoder=encoder, settings=settings)
    # exclude_generated=False so the generated mirror is visible (it's dropped by
    # default now); we're verifying source_class round-trips through the payload.
    results = searcher.search(query="authenticate user helper", top_k=10,
                              score_threshold=0.0, exclude_generated=False)
    by_path = {r.file_path: r.source_class for r in results}
    assert by_path.get("p/app.py") == "owned"
    assert by_path.get("p/app.min.js") == "generated"
