"""S12 — the `rag client` CLI surface for managing client access profiles."""

import pytest
from typer.testing import CliRunner

from ragtools.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    # Point the profile store at an isolated data dir (data_dir anchors to
    # qdrant_path.parent), so the CLI never touches real state.
    monkeypatch.setenv("RAG_QDRANT_PATH", str(tmp_path / "qdrant"))
    monkeypatch.setenv("RAG_STATE_DB", str(tmp_path / "state.db"))


def test_add_then_list_show_remove():
    r = runner.invoke(app, ["client", "add", "docs-bot", "--all-projects", "--cap", "retrieval"])
    assert r.exit_code == 0, r.output
    assert "created" in r.output.lower()
    assert "RAG_CLIENT_PROFILE" in r.output          # prints the .mcp.json snippet

    r = runner.invoke(app, ["client", "list"])
    assert r.exit_code == 0 and "docs-bot" in r.output

    r = runner.invoke(app, ["client", "show", "docs-bot"])
    assert r.exit_code == 0 and "retrieval" in r.output

    r = runner.invoke(app, ["client", "remove", "docs-bot"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["client", "list"])
    assert "docs-bot" not in r.output


def test_add_multiple_caps_and_scope():
    r = runner.invoke(app, ["client", "add", "ops", "--projects", "rag-docs,royal",
                            "--cap", "retrieval", "--cap", "indexing", "--allow-destructive"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["client", "show", "ops"])
    assert "indexing" in r.output


def test_missing_scope_refused():
    r = runner.invoke(app, ["client", "add", "x", "--cap", "retrieval"])
    assert r.exit_code == 2
    assert "scope" in r.output.lower()


def test_reserved_owner_id_refused():
    r = runner.invoke(app, ["client", "add", "owner", "--all-projects", "--cap", "retrieval"])
    assert r.exit_code == 2


def test_ungrantable_admin_refused():
    r = runner.invoke(app, ["client", "add", "x", "--all-projects", "--cap", "profile_administration"])
    assert r.exit_code == 2


def test_capabilities_lists_grantable_groups():
    r = runner.invoke(app, ["client", "capabilities"])
    assert r.exit_code == 0
    assert "retrieval" in r.output


def test_show_unknown_client_errors():
    r = runner.invoke(app, ["client", "show", "ghost"])
    assert r.exit_code == 1
