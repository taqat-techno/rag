"""Phase 1 (W1) — UI reactivity contracts.

The master plan's Gate G1: no action requires a manual refresh to observe its own
result, and nothing fails silently. These tests pin the client-side contracts that
make that true, at the level the server actually controls — the rendered HTML.

Runtime behaviour (a real 404 producing a visible toast, a real focus event
refetching) is covered by the Playwright suite; these tests stop the contracts
from silently regressing.

Plan: docs/planning/RAG_MODERNIZATION_MASTER_PLAN.md  (Phase 1 -> G1)
"""

import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ragtools.config import ProjectConfig, Settings
import ragtools.service.app as app_module
from ragtools.service.app import create_app
from ragtools.service.owner import QdrantOwner


@pytest.fixture
def client():
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            content_root=str(Path(tmp)),
            qdrant_path=str(Path(tmp) / "qdrant"),
            state_db=str(Path(tmp) / "state.db"),
            projects=[ProjectConfig(id="proj-a", path=str(Path(tmp)))],
        )
        app_module._owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        app_module._settings = settings
        try:
            with TestClient(create_app(), raise_server_exceptions=True) as tc:
                yield tc
        finally:
            app_module._owner = None
            app_module._settings = None


# --- 1. Nothing fails silently -----------------------------------------


def test_global_error_handlers_are_registered(client):
    """htmx 2.x does NOT swap on 4xx/5xx; without these listeners every failed
    action is invisible. This is the single highest-blast-radius defect."""
    body = client.get("/").text
    assert "htmx:responseError" in body
    assert "htmx:sendError" in body


def test_toast_region_exists_and_is_announced(client):
    body = client.get("/").text
    assert 'id="rag-toasts"' in body
    assert 'aria-live="assertive"' in body  # errors must be announced


# --- 2. State regions refresh (the core staleness fix) ------------------


@pytest.mark.parametrize("region", ["dash-status", "dash-projects"])
def test_state_regions_refresh_and_listen_for_invalidation(client, region):
    """These two render live counts (files/chunks/projects/watcher) and were
    `hx-trigger="load"` only — fetched once, never updated again."""
    body = client.get("/").text
    frag = body.split(f'id="{region}"', 1)[1].split(">", 1)[0]
    assert "every" in frag, f"{region} never refreshes"
    assert "rag-invalidate" in frag, f"{region} does not listen for invalidation"


def test_crash_banner_refreshes(client):
    body = client.get("/").text
    frag = body.split('id="crash-banner-slot"', 1)[1].split(">", 1)[0]
    assert "every" in frag or "rag-invalidate" in frag


# --- 3. Focus / visibility refetch --------------------------------------


def test_focus_and_visibility_trigger_refresh(client):
    """Cheapest high-value fix: come back to the window after using the CLI and
    the page reconciles."""
    body = client.get("/").text
    assert "visibilitychange" in body
    assert "rag-invalidate" in body


# --- 4. Activity cursor is actually consumed ----------------------------


def test_activity_panel_sends_the_cursor_and_appends(client):
    """The server has always accepted `after=` and even rendered
    `data-latest-id` "for next poll" — the client never sent it back."""
    body = client.get("/").text
    frag = body.split('id="activity-panel"', 1)[1].split(">", 1)[0]
    assert "hx-vals" in frag, "cursor is not sent"
    assert "after" in frag
    assert 'hx-swap="afterbegin"' in frag, "still replacing the whole log"


def test_activity_fragment_reports_its_cursor(client):
    r = client.get("/ui/activity")
    assert r.status_code == 200
    assert "rag-activity-cursor" in r.headers.get("HX-Trigger", "")


def test_activity_fragment_is_empty_when_nothing_is_new(client):
    """Append mode: with a cursor past the end there must be NOTHING to prepend —
    otherwise an 'empty' placeholder is prepended on every poll forever."""
    r = client.get("/ui/activity", params={"after": 10_000_000})
    assert r.status_code == 200
    assert r.text.strip() == ""


# --- 5. Degraded health is visible --------------------------------------


def test_dashboard_status_surfaces_degraded_state(client):
    """/health already computes `degraded` + `issues[]`; the UI never showed it,
    and rendered a misleading 'Watcher starting' for any non-running state."""
    r = client.get("/ui/dash/status")
    assert r.status_code == 200
    assert "data-degraded" in r.text


# --- 6. Accessibility on the destructive path ---------------------------


def test_confirm_dialog_has_dialog_semantics(client):
    """This modal gates the two most destructive actions in the product."""
    body = client.get("/").text
    frag = body.split('id="confirm-modal"', 1)[1].split(">", 1)[0]
    assert 'role="dialog"' in frag
    assert 'aria-modal="true"' in frag


def test_confirm_dialog_is_keyboard_dismissable(client):
    body = client.get("/").text
    assert "Escape" in body


def test_main_landmark_and_skip_link(client):
    body = client.get("/").text
    assert "<main" in body
    assert "skip-link" in body


def test_activity_drawer_toggle_is_keyboard_operable(client):
    """It was a <div onclick=...> — invisible to keyboard and screen readers."""
    body = client.get("/").text
    assert '<button type="button" class="activity-bar"' in body
    assert 'aria-expanded' in body
    assert '<div class="activity-bar" onclick' not in body


def test_live_regions_are_announced(client):
    body = client.get("/").text
    assert 'aria-live="polite"' in body  # activity / status regions


# --- 7. Config is a single owner and fails loudly -----------------------


def test_config_load_has_error_handling_and_single_owner(client):
    """The populate fetch had no .catch(); the numeric inputs carry no value=
    default, so a failed load left an empty form that would 422 on save —
    and the 422 was swallowed. Now: loud, and saving is disabled."""
    body = client.get("/config").text
    assert "function ragLoadConfig" in body
    assert ".catch(" in body
    assert "ragLoadConfig();" in body


def test_config_is_reread_after_save(client):
    """Mutations must not leave user input on screen as if it were persisted."""
    body = client.get("/config").text
    assert "/ui/config/save" in body
    assert body.count("ragLoadConfig()") >= 3  # initial + after form save + after MCP save


# --- 8. Mutations declare what they invalidated -------------------------


def test_mutating_fragments_emit_the_invalidation_contract(client, tmp_path):
    """Generalises the single `HX-Trigger: projectAdded` that nothing listened
    for into a contract every mutating fragment uses."""
    r = client.post("/ui/projects/add", data={
        "id": "invalidation-probe", "name": "Probe", "path": str(tmp_path), "mode": "docs",
    })
    assert r.status_code == 200
    assert "rag-invalidate" in r.headers.get("HX-Trigger", "")


def test_sse_subscriber_is_installed(client):
    body = client.get("/").text
    assert "EventSource" in body
    assert "/events" in body
