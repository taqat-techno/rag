"""S15 — the Diagnostics screen (route-testable without a browser).

§26.1 lists a Diagnostics screen (service identity, bound ports, storage). The
plan notes most screens are "rendering exercises over tested backends", so the
route is verifiable via TestClient even though the full visual/Playwright pass
needs a browser. This surfaces the S16 /identity data in the panel.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S15 §26.1 -> G15)
"""

import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ragtools.config import Settings
import ragtools.service.app as app_module
from ragtools.service.app import create_app
from ragtools.service.owner import QdrantOwner


@pytest.fixture
def client():
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(
            content_root=str(Path(tmpdir)),
            qdrant_path=str(Path(tmpdir) / "qdrant"),
            state_db=str(Path(tmpdir) / "state.db"),
            service_host="127.0.0.1",
            service_port=21437,
        )
        app_module._owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        app_module._settings = settings
        try:
            with TestClient(create_app(), raise_server_exceptions=True) as tc:
                yield tc, settings
        finally:
            app_module._owner = None
            app_module._settings = None


def test_diagnostics_page_renders_identity(client):
    tc, settings = client
    r = tc.get("/diagnostics")
    assert r.status_code == 200
    body = r.text
    assert "Diagnostics" in body
    assert "service identity" in body.lower()   # heading; sentence case product-wide
    assert settings.collection_name in body
    assert "21437" in body            # bound port surfaced
    assert "/identity" in body        # links to the live JSON


def test_diagnostics_has_main_landmark(client):
    # Accessibility (§26.2): a <main> landmark must be present.
    # The landmark now comes from base.html (<main id="main-content">) instead
    # of a bare <main> nested inside it — two <main> elements on one page is
    # invalid, and only one may be the document's main landmark.
    tc, _ = client
    body = tc.get("/diagnostics").text
    assert "<main" in body
    assert body.count("<main") == 1
