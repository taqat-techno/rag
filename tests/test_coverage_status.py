"""project_status coverage/freshness surfacing (P0-D / P1-D, W13/W14).

Generic: the orientation tool must expose what the index *can* answer — Mode,
source-class breakdown, and a freshness/stale signal — so a code FAIL is never
mistaken for "feature absent".
"""

from datetime import datetime, timedelta

import pytest


@pytest.fixture(autouse=True)
def _restore_app_singletons():
    from ragtools.service import app as app_module
    o, s = app_module._owner, app_module._settings
    yield
    app_module._owner, app_module._settings = o, s


def test_source_class_breakdown_helper():
    from ragtools.service.routes import _source_class_breakdown
    rows = [{"file_path": "p/app.py"}, {"file_path": "p/dist/bundle.js"}, {"file_path": "p/README.md"}]
    b = _source_class_breakdown(rows)
    assert b.get("owned", 0) == 2       # app.py + README.md
    assert b.get("generated", 0) == 1   # dist/bundle.js


def test_is_stale_helper():
    from ragtools.service.routes import _is_stale
    now = datetime(2026, 6, 30, 12, 0, 0)
    fresh = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(hours=48)).isoformat()
    assert _is_stale(fresh, 24, now=now) is False
    assert _is_stale(old, 24, now=now) is True
    assert _is_stale(None, 24, now=now) is False  # never indexed -> not "stale"


def test_project_status_exposes_coverage_and_mode_note(tmp_path):
    from ragtools.config import ProjectConfig, Settings
    from ragtools.service import app as app_module, routes as routes_mod
    app_module._settings = Settings(
        state_db=str(tmp_path / "none.db"),
        projects=[ProjectConfig(id="d", path=str(tmp_path), mode="docs")],
    )
    res = routes_mod.project_status_endpoint("d")
    assert "source_class_breakdown" in res
    assert "stale" in res
    assert res["mode_note"]  # docs mode -> non-empty note
    assert "not indexed" in res["mode_note"].lower()


def test_project_status_code_mode_has_no_mode_note(tmp_path):
    from ragtools.config import ProjectConfig, Settings
    from ragtools.service import app as app_module, routes as routes_mod
    app_module._settings = Settings(
        state_db=str(tmp_path / "none.db"),
        projects=[ProjectConfig(id="c", path=str(tmp_path), mode="code")],
    )
    res = routes_mod.project_status_endpoint("c")
    assert res.get("mode_note", "") == ""  # code mode -> no docs-only warning
