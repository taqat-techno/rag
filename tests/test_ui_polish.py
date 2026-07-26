"""Invariants for the admin-panel design system.

Each test here pins a defect that was measured in the browser before the
redesign, so it cannot silently come back:

* ``--color-text-muted`` was ``#94A3B8`` — **2.56:1** on white, where WCAG 2.1
  AA requires 4.5:1. It is the colour of every hint, timestamp and file path
  in the product.
* ``html { font-size: 14.5px }`` hard-overrode the reader's own browser
  font-size preference.
* ``.muted`` (8 uses), ``.table`` and ``.kv-table`` were referenced by
  templates and by ``pages.py`` but never defined, so the Client Access and
  Diagnostics surfaces rendered unstyled.
* The Settings page carried **147** inline ``style=`` attributes, against a
  workspace convention of no inline styles.
* The degraded banner printed raw enum keys (``scale_over``) at the user.
* ``compute_scale_warning`` emits ``approaching``, but the banner only matched
  ``("warn", "over")`` — so the 15k–20k soft warning was computed on every
  request and then dropped.

Plan: docs/planning/RAG_MODERNIZATION_MASTER_PLAN.md  (UI revision pass)
"""

import re
import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ragtools.config import ProjectConfig, Settings
from ragtools.service import app as app_module
from ragtools.service.app import create_app
from ragtools.service.owner import QdrantOwner

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def client():
    """Same wiring as tests/test_pages.py — an indexed, in-memory panel."""
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(
            content_root=str(FIXTURES),
            state_db=str(Path(tmpdir) / "test_state.db"),
            projects=[
                ProjectConfig(id="project_a", path=str(FIXTURES / "project_a")),
                ProjectConfig(id="project_b", path=str(FIXTURES / "project_b")),
            ],
        )
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        owner.run_full_index()

        app_module._owner = owner
        app_module._settings = settings
        try:
            with TestClient(create_app(), raise_server_exceptions=True) as tc:
                yield tc
        finally:
            app_module._owner = None
            app_module._settings = None


@pytest.fixture(scope="module")
def css(client):
    r = client.get("/static/design.css")
    assert r.status_code == 200
    return r.text


# --- Colour contrast ----------------------------------------------------


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    parts = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _token(css_text: str, name: str) -> str:
    """First (light-mode) value of a custom property."""
    m = re.search(rf"^\s*{re.escape(name)}:\s*(#[0-9A-Fa-f]{{6}})\s*;", css_text, re.M)
    assert m, f"token {name} not found"
    return m.group(1)


@pytest.mark.parametrize("token", ["--color-text", "--color-text-secondary", "--color-text-muted"])
def test_every_text_token_meets_aa_on_every_surface(css, token):
    """The regression that started this: muted text at 2.56:1."""
    colour = _token(css, token)
    for surface_token in ("--color-surface", "--color-bg", "--color-surface-sunken"):
        surface = _token(css, surface_token)
        ratio = _contrast(colour, surface)
        assert ratio >= 4.5, (
            f"{token} ({colour}) on {surface_token} ({surface}) is {ratio:.2f}:1, "
            "below the WCAG AA minimum of 4.5:1"
        )


def test_link_colour_is_readable_on_the_sunken_surface(css):
    """The raw brand accent is 4.39:1 on the sunken surface — under AA. Links
    use a deeper variant so card footers and tinted panels stay legible."""
    link = _token(css, "--color-link")
    for surface_token in ("--color-surface", "--color-bg", "--color-surface-sunken"):
        ratio = _contrast(link, _token(css, surface_token))
        assert ratio >= 4.5, f"--color-link on {surface_token} is {ratio:.2f}:1"


def test_dark_mode_does_not_put_white_text_on_the_light_accent(css):
    """The dark accent is a light lavender; white on it measured 2.88:1."""
    dark = css.split("prefers-color-scheme: dark", 1)[1]
    assert "--color-text-on-accent" in dark, (
        "dark mode must override --color-text-on-accent; white on the lightened "
        "accent fails AA"
    )


# --- Type & spacing system ---------------------------------------------


def test_root_font_size_does_not_override_the_reader(css):
    """`html { font-size: 14.5px }` overrode the browser's own font-size
    setting, which is an accessibility anti-pattern."""
    html_block = css.split("html {", 1)[1].split("}", 1)[0]
    assert "font-size: 100%" in html_block
    assert "14.5px" not in html_block


def test_a_type_scale_exists(css):
    for step in ("--text-xs", "--text-sm", "--text-base", "--text-md",
                 "--text-lg", "--text-xl"):
        assert f"{step}:" in css, f"missing type-scale step {step}"


def test_a_spacing_scale_exists(css):
    for step in ("--space-1", "--space-2", "--space-4", "--space-6", "--space-8"):
        assert f"{step}:" in css, f"missing spacing step {step}"


# --- Classes that are used must exist -----------------------------------


@pytest.mark.parametrize("selector", [".muted", ".table", ".kv-table", ".hint",
                                      ".card-desc", ".card-footer", ".empty-state",
                                      ".sr-only", ".overlay-host"])
def test_referenced_classes_are_defined(css, selector):
    """`.muted` had 8 usages, `.table` and `.kv-table` one each — none defined,
    so those surfaces rendered with browser defaults."""
    assert re.search(rf"(^|[,\s]){re.escape(selector)}[\s,{{:]", css, re.M), \
        f"{selector} is used in markup but not defined in design.css"


# --- Motion -------------------------------------------------------------


def test_reduced_motion_is_honoured(css):
    """Three keyframe animations ran unconditionally."""
    assert "prefers-reduced-motion: reduce" in css


# --- No inline styles in server-rendered markup -------------------------


@pytest.mark.parametrize("path", ["/", "/projects", "/search", "/config", "/diagnostics"])
def test_pages_carry_no_inline_style_attributes(client, path):
    """Settings alone had 147. Runtime-set styles (canvas sizing, data-driven
    legend colours) are fine — this covers server-rendered markup only."""
    body = client.get(path).text
    assert 'style="' not in body, f"{path} still contains inline style attributes"


@pytest.mark.parametrize("path", ["/ui/dash/status", "/ui/dash/projects",
                                  "/ui/projects/list", "/ui/clients", "/ui/watcher"])
def test_fragments_carry_no_inline_style_attributes(client, path):
    body = client.get(path).text
    assert 'style="' not in body, f"{path} still contains inline style attributes"


# --- Copy ---------------------------------------------------------------


def test_degraded_banner_does_not_print_raw_enum_keys(client):
    """The banner used to read `Degraded: scale_over`. The key is retained as a
    data attribute for tests and styling, but the reader gets a sentence."""
    from ragtools.service.pages import _ISSUE_HEADLINES

    body = client.get("/ui/dash/status").text
    assert "data-degraded" in body            # the existing contract
    for key, headline in _ISSUE_HEADLINES.items():
        assert not headline.startswith(key), f"{key} has no human headline"
        assert "_" not in headline, f"headline for {key} still reads as an identifier"


def test_soft_scale_warning_is_actually_surfaced():
    """`compute_scale_warning` emits ok | approaching | over, but the banner
    matched only ("warn", "over") — the 15k-20k warning was computed and then
    silently discarded."""
    import inspect

    from ragtools.service import pages
    from ragtools.service.owner import compute_scale_warning

    assert compute_scale_warning(17_000)["level"] == "approaching"
    src = inspect.getsource(pages.ui_dash_status)
    assert "approaching" in src, (
        "the dashboard still ignores the soft scale warning"
    )
    assert "scale_approaching" in pages._ISSUE_HEADLINES


def test_connect_to_claude_card_is_gone(client):
    """Removed at the owner's request — the MCP snippet lives in the README."""
    body = client.get("/config").text
    assert "Connect to Claude" not in body
    assert 'id="mcp-config"' not in body
    assert "/api/mcp-config" not in body


# --- Structure ----------------------------------------------------------


def test_settings_has_section_navigation(client):
    """A single 2,600px scroll of eight cards is not navigable."""
    body = client.get("/config").text
    assert 'id="settings-nav"' in body
    for anchor in ("#sec-indexing", "#sec-service", "#sec-claude", "#sec-danger"):
        assert anchor in body, f"settings nav is missing {anchor}"


def test_client_profiles_are_not_nested_inside_the_settings_form(client):
    """`_client_form` renders its own <form>. A form nested inside another is
    invalid HTML — it survives today only because htmx parses the fragment
    detached from the document."""
    body = client.get("/config").text
    form_start = body.index('hx-put="/ui/config/save"')
    form_end = body.index("</form>", form_start)
    assert 'id="clients-panel"' not in body[form_start:form_end], (
        "the Client profiles panel is inside the settings <form> again"
    )


def test_diagnostics_is_reachable_from_the_navigation(client):
    """The page existed and was rendered, but nothing linked to it."""
    assert 'href="/diagnostics"' in client.get("/").text


def test_diagnostics_does_not_nest_a_second_main_landmark(client):
    """base.html already provides <main id="main-content">."""
    body = client.get("/diagnostics").text
    assert body.count("<main") == 1, "two <main> landmarks on one page"
