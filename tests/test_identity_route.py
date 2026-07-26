"""S16 — the live GET /identity route (wires build_identity into the service).

§27.1: identity goes in a NEW endpoint (so /health stays additively compatible),
and ``bound_port`` is the ACTUAL bind — the field that would have caught the
live ``:21422``-reports-``:21420`` defect. This drives the route that assembles
the identity payload from the running service, verified through a TestClient.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S16 §27.1 -> G16)
"""

import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ragtools.config import Settings
import ragtools.service.app as app_module
from ragtools.service.app import create_app
from ragtools.service.identity import API_VERSION
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
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        app_module._owner = owner
        app_module._settings = settings
        from ragtools.service import routes as routes_module
        routes_module.set_bound_address(None, None)  # reset any prior override
        try:
            with TestClient(create_app(), raise_server_exceptions=True) as tc:
                yield tc, settings
        finally:
            app_module._owner = None
            app_module._settings = None
            routes_module.set_bound_address(None, None)


def test_identity_route_returns_the_v3_shape(client):
    tc, settings = client
    r = tc.get("/identity")
    assert r.status_code == 200
    body = r.json()
    for key in ("service", "service_id", "instance_id", "version", "api_version",
                "profile", "install_mode", "bound_host", "bound_port", "data_dir",
                "storage", "auth_mode", "capabilities", "collections_ready"):
        assert key in body, f"missing {key}"
    assert body["service"] == "ragtools"
    assert body["api_version"] == API_VERSION
    assert body["storage"]["mode"] in ("embedded", "managed", "external")


def test_bound_port_reflects_the_actual_recorded_bind(client):
    # The whole point: report the ACTUAL bind, not just the configured value.
    tc, settings = client
    from ragtools.service import routes as routes_module
    routes_module.set_bound_address("127.0.0.1", 26999)  # the real socket bind
    body = tc.get("/identity").json()
    assert body["bound_port"] == 26999
    assert body["bound_host"] == "127.0.0.1"


def test_bound_port_falls_back_to_configured_when_unrecorded(client):
    tc, settings = client
    body = tc.get("/identity").json()
    assert body["bound_port"] == settings.service_port  # 21437


def test_health_still_works_alongside_identity(client):
    # /identity is additive — /health's stable contract is untouched.
    tc, _ = client
    assert tc.get("/health").status_code == 200
