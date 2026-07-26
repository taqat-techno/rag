"""S12 — the Client Access UI (config-page fragment: add / list / remove)."""

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
            projects=[ProjectConfig(id="rag-docs", path=str(Path(tmp)))],
        )
        app_module._owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        app_module._settings = settings
        try:
            with TestClient(create_app(), raise_server_exceptions=True) as tc:
                yield tc
        finally:
            app_module._owner = None
            app_module._settings = None


def test_fragment_renders_form_with_checkboxes(client):
    r = client.get("/ui/clients")
    assert r.status_code == 200
    body = r.text
    assert "Add a client" in body
    assert 'name="caps" value="retrieval"' in body       # capability checkbox
    assert 'name="all_projects"' in body                  # scope checkbox
    assert "rag-docs" in body                             # project checkbox from config
    # Destructive opt-in: assert the control, not its wording, so copy can be
    # improved without a false failure.
    assert 'name="allow_destructive"' in body
    assert "restoring collections" in body
    # owner-only admin group is NOT offered as a checkbox
    assert 'value="profile_administration"' not in body


def test_add_client_via_form_then_appears_and_removes(client):
    r = client.post("/ui/clients/add", data={
        "id": "docs-bot", "name": "Docs Bot", "all_projects": "1", "caps": "retrieval",
    })
    assert r.status_code == 200
    assert "docs-bot" in r.text
    assert "saved" in r.text.lower()

    # It's listed on a fresh fragment fetch.
    assert "docs-bot" in client.get("/ui/clients").text

    # Remove it — its row (edit button) is gone from the list.
    r = client.delete("/ui/clients/docs-bot/remove")
    assert r.status_code == 200
    assert "removed" in r.text.lower()
    assert 'hx-get="/ui/clients/docs-bot/edit"' not in client.get("/ui/clients").text


def test_add_invalid_scope_shows_error_not_crash(client):
    # No scope chosen → the fragment re-renders with an error, HTTP 200.
    r = client.post("/ui/clients/add", data={"id": "x", "caps": "retrieval"})
    assert r.status_code == 200
    assert "scope is required" in r.text.lower()


def test_add_ungrantable_admin_shows_error(client):
    r = client.post("/ui/clients/add", data={
        "id": "x", "all_projects": "1", "caps": "profile_administration",
    })
    assert r.status_code == 200
    assert "owner-only" in r.text.lower()


def test_config_page_includes_client_access_card(client):
    body = client.get("/config").text
    # The card is titled "Client profiles", inside the "Claude access" section.
    assert "Client profiles" in body
    assert 'hx-get="/ui/clients"' in body


# --- editing an existing client -----------------------------------------


def _add(client, **fields):
    return client.post("/ui/clients/add", data=fields)


def test_client_rows_have_an_edit_button(client):
    _add(client, id="docs-bot", all_projects="1", caps="retrieval")
    body = client.get("/ui/clients").text
    assert 'hx-get="/ui/clients/docs-bot/edit"' in body


def test_edit_prefills_the_form_with_current_settings(client):
    _add(client, id="scoped-bot", name="Scoped Bot", projects="rag-docs",
         caps=["retrieval", "indexing"], allow_destructive="1")
    body = client.get("/ui/clients/scoped-bot/edit").text
    # id is pre-filled and locked; name pre-filled
    assert 'name="id" value="scoped-bot" readonly' in body
    assert 'value="Scoped Bot"' in body
    # current capabilities are pre-checked
    assert 'value="retrieval" checked' in body
    assert 'value="indexing" checked' in body
    # scope + destructive reflect current state
    assert 'value="rag-docs" checked' in body
    assert 'name="allow_destructive" value="1" checked' in body
    # a group it does NOT have is not checked
    assert 'value="collection_management" checked' not in body
    assert "Update client" in body


def test_edit_all_projects_prechecks_all_projects(client):
    _add(client, id="owner-ish", all_projects="1", caps="retrieval")
    body = client.get("/ui/clients/owner-ish/edit").text
    assert 'name="all_projects" value="1" checked' in body


def test_edit_then_save_updates_in_place(client):
    _add(client, id="bot", all_projects="1", caps=["retrieval", "indexing"], allow_destructive="1")
    # Edit: drop indexing + destructive, narrow scope to a project.
    r = _add(client, id="bot", name="Bot v2", projects="rag-docs", caps="retrieval")
    assert r.status_code == 200 and "saved" in r.text.lower()

    from ragtools.profile_store import ProfileStore
    settings = app_module._settings
    from pathlib import Path
    with ProfileStore(str(Path(settings.data_dir) / "profiles.db")) as store:
        p = store.get("bot")
    assert p.display_name == "Bot v2"
    assert p.capability_groups == frozenset({"retrieval"})     # indexing removed
    assert p.allowed_projects == frozenset({"rag-docs"})        # scope narrowed
    assert p.destructive_policy == "forbidden"                   # destructive removed


def test_edit_unknown_client_shows_error(client):
    r = client.get("/ui/clients/ghost/edit")
    assert r.status_code == 200
    assert "no such client" in r.text.lower()
