"""Settings has ONE save action, and it appears only when there is work to do.

Two problems, both reported from real use:

1. **Two save buttons.** "Save settings" (the form) and "Save tool access" (the
   MCP grid) existed because they hit different endpoints — an implementation
   detail. From the reader's side it is one Settings page, so two buttons made
   "which one applies to what I just changed?" a real question, and it was
   possible to change tool access, press "Save settings", and lose the edit.

2. **The save bar floated.** Pinned to the viewport, it hovered over the very
   fields it was meant to save — the Log level row sat underneath it at rest.

Now: one button in the page header (where every other page keeps its primary
action), hidden until something actually changes.

Plan: docs/planning/RAG_COLLECTION_ARCHITECTURE_IMPLEMENTATION_PLAN.md (W7)
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
    with tempfile.TemporaryDirectory() as td:
        settings = Settings(
            content_root=str(FIXTURES),
            state_db=str(Path(td) / "state.db"),
            qdrant_path=str(Path(td) / "q"),
            data_dir=str(Path(td) / "d"),
            projects=[ProjectConfig(id="project_a", path=str(FIXTURES / "project_a"))],
        )
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        app_module._owner = owner
        app_module._settings = settings
        try:
            with TestClient(create_app()) as c:
                yield c
        finally:
            app_module._owner = None
            app_module._settings = None
            owner.close()


@pytest.fixture(scope="module")
def body(client):
    return client.get("/config").text


# --- one action ----------------------------------------------------------


def _save_buttons(body):
    """Rendered <button> elements whose label is a save action.

    Matched on elements, not raw text: the source comments deliberately quote
    the old labels to record why they were merged.
    """
    return [m.group(0) for m in re.finditer(r"<button\b[^>]*>.*?</button>", body,
                                            re.S | re.I)
            if re.search(r"\bsave\b", re.sub(r"<[^>]+>", " ", m.group(0)), re.I)]


def test_there_is_exactly_one_save_control(body):
    """The page-header button is the only way to save settings."""
    assert 'id="settings-save"' in body
    assert body.count('id="settings-save"') == 1

    buttons = _save_buttons(body)
    assert len(buttons) == 1, (
        f"expected one save button, found {len(buttons)}: "
        f"{[re.sub(r'<[^>]+>', '', b).strip() for b in buttons]}"
    )
    assert 'id="settings-save"' in buttons[0]
    # The MCP grid's own save button is gone.
    assert 'id="mcp-tools-save"' not in body


def test_the_lazily_loaded_client_panel_adds_no_second_save(client):
    """`#clients-panel` is fetched after load, so it is absent from the page
    source — but it is very much on screen, and a button there reading "Save…"
    would put the two-save ambiguity straight back."""
    fragment = client.get("/ui/clients").text
    buttons = _save_buttons(fragment)
    assert not buttons, (
        "the client panel contributes a save-looking button: "
        f"{[re.sub(r'<[^>]+>', '', b).strip() for b in buttons]}"
    )
    # It still has its own create action — just not called "save".
    assert "Add client" in fragment


def test_no_submit_button_remains_inside_the_settings_form(body):
    """A second visible submit inside the form would be the old ambiguity."""
    form = body[body.index('id="settings-form"'):body.index("</form>")]
    assert 'type="submit"' not in form


def test_the_save_button_starts_hidden(body):
    """Nothing to save on load, so the header stays quiet."""
    btn = re.search(r'<button[^>]*id="settings-save"[^>]*>', body)
    assert btn, "save button not found"
    assert "hidden" in btn.group(0), "save button is visible with no pending changes"

    hint = re.search(r'<span[^>]*id="settings-dirty"[^>]*>', body)
    assert hint and "hidden" in hint.group(0)


def test_the_save_control_is_in_the_page_header_not_a_floating_bar(body):
    """It must not hover over the fields it saves."""
    assert 'class="page-actions"' in body
    header = body[body.index('class="page-actions"'):]
    header = header[:header.index("</header>")]
    assert 'id="settings-save"' in header, "save button is not in the page header"

    # The sticky bar is gone entirely.
    assert 'class="form-actions"' not in body
    css = "".join(open(
        Path(__file__).parent.parent / "src/ragtools/service/static/design.css",
        encoding="utf-8").readlines())
    assert ".form-actions" not in css, "the floating save bar's styles remain"


# --- dirty tracking ------------------------------------------------------


def test_the_page_tracks_unsaved_changes(body):
    for symbol in ("ragSettingsSnapshot", "ragSettingsDirty",
                   "ragRefreshSaveAffordance", "ragMarkSettingsClean"):
        assert symbol in body, f"missing dirty-tracking helper {symbol}"


def test_the_snapshot_covers_both_the_form_and_the_tool_grid(body):
    """Either kind of edit must reveal the button — that is the whole point of
    merging the two saves."""
    snap = body[body.index("function ragSettingsSnapshot"):]
    snap = snap[:snap.index("function ragSettingsDirty")]
    assert "settings-form" in snap, "form fields are not part of the dirty check"
    assert "ragCollectMcpTools" in snap, "tool access is not part of the dirty check"


def test_editing_anything_in_the_settings_layout_re_evaluates(body):
    assert "settings-layout" in body
    assert "ragRefreshSaveAffordance" in body


def test_saving_is_re_baselined_after_a_successful_save(body):
    """The button must disappear again once the server confirms."""
    assert "ragMarkSettingsClean()" in body
    assert "/ui/config/save" in body


# --- the single save still covers both destinations ----------------------


def test_one_click_saves_both_the_tool_grid_and_the_form(body):
    save = body[body.index("function ragSaveSettings"):]
    save = save[:save.index("addEventListener('click', ragSaveSettings)")]
    assert "'/api/config'" in save, "tool access is not saved"
    assert "mcp_tools" in save
    assert "htmx.trigger(form, 'submit')" in save, "form fields are not saved"


def test_a_failed_tool_save_aborts_instead_of_half_applying(body):
    """Reporting success after saving only half the page would be a lie."""
    save = body[body.index("function ragSaveSettings"):]
    save = save[:save.index("addEventListener('click', ragSaveSettings)")]
    assert "throw new Error" in save
    assert ".catch(" in save


def test_both_save_endpoints_still_exist(client):
    """The merge is a UI change: the server contract is untouched."""
    assert client.put("/ui/config/save", data={"chunk_size": 400}).status_code == 200
    assert client.put("/api/config", json={"mcp_tools": {}}).status_code == 200
