"""S15 §26.2 — the two non-browser UI defects the plan flags in passing.

1. scikit-learn is an *undeclared* runtime dependency of the Knowledge Map
   (`map_data.py` imports `sklearn.decomposition.PCA`), surviving only
   transitively via sentence-transformers — a live break the moment torch is
   dropped. It must be declared.
2. The Map empty-state links to `/index`, which has **no GET handler** (only
   `POST /api/index`) — a guaranteed 404 in the exact state a new user first
   sees the Map. It must point at a real page.

These need no browser, so they are pinned here as ordinary tests.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S15 §26.2 -> G15)
"""

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES = _ROOT / "src" / "ragtools" / "service" / "templates"

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


def _dependencies() -> list[str]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["dependencies"]


def test_scikit_learn_is_a_declared_dependency():
    # The map's PCA import must not rely on a transitive dependency.
    names = [re.split(r"[><=~!\[ ]", d, 1)[0].lower() for d in _dependencies()]
    assert "scikit-learn" in names, f"scikit-learn missing from {names}"


def test_no_template_links_to_the_dead_index_route():
    # `/index` has no GET handler; nothing may link to it.
    offenders = []
    for html in _TEMPLATES.glob("*.html"):
        text = html.read_text(encoding="utf-8")
        if re.search(r'href="/index"', text):
            offenders.append(html.name)
    assert offenders == [], f"dead /index link in: {offenders}"


def test_map_empty_state_points_at_a_real_page():
    # The empty-state a new user first sees must link somewhere that exists.
    text = (_TEMPLATES / "map.html").read_text(encoding="utf-8")
    assert 'href="/projects"' in text
