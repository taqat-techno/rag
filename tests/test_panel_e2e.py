"""S15 — admin-panel browser E2E (Playwright).

The S15 validation gate: a real browser drives the booted admin panel and
verifies the screens render with the right data + accessibility landmarks. These
exact assertions were executed successfully on 2026-07-24 against a dev panel on
:21450 (see the Diagnostics screen reporting the ACTUAL bound port — the S16
/identity work, validated end-to-end in a browser).

Resource-gated, like the managed-Qdrant e2e: skipped unless Playwright is
installed AND ``RAG_E2E_PANEL_URL`` points at a running panel. To run:

    pip install playwright && playwright install chromium
    # boot a dev panel on an isolated port + data dir, then:
    RAG_E2E_PANEL_URL=http://127.0.0.1:21450 pytest tests/test_panel_e2e.py

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S15 §26 -> G15)
"""

import os

import pytest

PANEL_URL = os.environ.get("RAG_E2E_PANEL_URL", "").rstrip("/")

playwright_sync = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed"
) if PANEL_URL else None

pytestmark = pytest.mark.skipif(
    not PANEL_URL,
    reason="panel e2e is resource-gated; set RAG_E2E_PANEL_URL to a running panel",
)


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        try:
            yield pg
        finally:
            browser.close()


def test_diagnostics_screen_renders_identity(page):
    page.goto(f"{PANEL_URL}/diagnostics")
    assert page.title() == "Diagnostics — RAG Tools"
    # Accessibility landmark (§26.2) + the S16 identity data.
    assert page.locator("main").count() == 1
    body = page.content()
    # Case-insensitive: headings are sentence case product-wide.
    assert "service identity" in body.lower()
    assert "ragtools" in body
    # The ACTUAL bound port is surfaced (the :21422-reports-:21420 fix).
    port = PANEL_URL.rsplit(":", 1)[-1]
    assert port in body


def test_dashboard_and_nav_render(page):
    page.goto(f"{PANEL_URL}/")
    assert page.title() == "Dashboard — RAG Tools"
    # The sidebar nav links every primary screen.
    for label in ("Dashboard", "Search", "Projects", "Diagnostics"):
        assert page.get_by_role("link", name=label).count() >= 1


def test_identity_api_reachable_from_browser(page):
    page.goto(f"{PANEL_URL}/")
    ident = page.evaluate("async () => (await fetch('/identity')).json()")
    assert ident["service"] == "ragtools"
    assert ident["api_version"]
    assert str(ident["bound_port"]) == PANEL_URL.rsplit(":", 1)[-1]


# --- Storage engine + collection model, in a real browser ---------------


def _status(page):
    page.goto(f"{PANEL_URL}/")
    return page.evaluate("async () => (await fetch('/api/status')).json()")


def test_diagnostics_shows_the_storage_engine(page):
    """"Which engine am I actually on" must be answerable without reading source."""
    page.goto(f"{PANEL_URL}/diagnostics")
    body = page.content().lower()
    assert "storage engine" in body
    assert "indexed search" in body        # the HNSW row
    assert "collection model" in body

    ident = page.evaluate("async () => (await fetch('/identity')).json()")
    assert ident["storage"]["mode"] in ("embedded", "managed", "external")
    if ident["storage"]["mode"] == "managed":
        # A managed engine is version-pinned and must report it.
        assert ident["storage"]["engine_version"], "managed engine reports no version"


def test_diagnostics_lists_every_collection(page):
    page.goto(f"{PANEL_URL}/diagnostics")
    status = page.evaluate("async () => (await fetch('/api/status')).json()")
    body = page.content()
    assert "Collections" in body
    for entry in status.get("collections", []):
        assert entry["name"] in body, f"{entry['name']} missing from the table"


def test_the_scale_warning_matches_the_engine(page):
    """The 20,000-point ceiling belongs to local mode's brute-force scan.

    On a server with HNSW that limitation does not exist, so the banner must be
    absent however many points are indexed; on the embedded engine past the
    limit it must still be present. Anything else is either a lie or noise.
    """
    status = _status(page)
    hnsw = status["storage"]["hnsw"]
    points = status["points_count"]

    page.goto(f"{PANEL_URL}/")
    page.wait_for_selector("#dash-status .dash-status-row", timeout=15000)
    degraded = page.locator(".dash-degraded")

    if hnsw:
        assert status["scale"]["level"] == "ok", (
            f"scale warning at level {status['scale']['level']} on an engine with "
            f"HNSW ({points:,} points) — the limitation it describes is gone"
        )
        assert "scale" not in (page.locator("#dash-status").inner_html() or "").lower() \
            or degraded.count() == 0 or "storage engine handles well" not in degraded.inner_text()
    elif points >= 20000:
        assert degraded.count() == 1
        assert "storage engine handles well" in degraded.inner_text()


def test_per_project_search_is_isolated_in_the_browser(page):
    """Drive the real search UI; a scoped query must not surface another
    project's files."""
    status = _status(page)
    if status.get("collection_strategy") != "per_project":
        pytest.skip("panel is not running the per-project model")

    projects = [c for c in status.get("collections", [])
                if c["kind"] == "project" and c["points"] > 0]
    if len(projects) < 2:
        pytest.skip("need two populated projects to test isolation")

    target = projects[0]["project"]
    other = projects[1]["project"]

    page.goto(f"{PANEL_URL}/search")
    page.wait_for_function(
        "() => document.querySelector('select[name=project]')"
        "  && !document.querySelector('select[name=project]').disabled",
        timeout=15000,
    )
    page.select_option("select[name=project]", target)
    page.fill("input[name=query]", "configuration")
    page.click("button[type=submit]")
    page.wait_for_selector("#results .result-card, #results .empty-state", timeout=30000)

    for path in page.locator("#results .result-path").all_inner_texts():
        assert not path.startswith(f"{other}/"), \
            f"query scoped to {target} returned a {other} file: {path}"


def test_no_console_errors_on_any_screen(page):
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    for path in ("/", "/projects", "/search", "/config", "/diagnostics"):
        page.goto(f"{PANEL_URL}{path}")
        page.wait_for_timeout(1500)

    # The 3D map bundle is lazy-loaded only when its tab is opened.
    errors = [e for e in errors if "echarts" not in e.lower()]
    assert not errors, f"console errors: {errors[:5]}"
