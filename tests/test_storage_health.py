"""`/health` must reflect the STORE, not just the process.

`status: ready` was returned whenever the process was up. With the embedded
engine that is nearly true — the store is in-process, so if Python is alive the
store is alive. With a managed or external server it is not: `qdrant.exe` can
die (crash, OOM, someone stops it) while this service keeps answering /health
with a cheerful "ready", and every search and index then fails against a store
nobody said was gone.

A monitor polling /health would report green through a total outage. So the
probe is now part of health — cached, because /health is polled often and
hammering the engine adds load without adding information.

Plan: docs/planning/RAG_STABILITY_HARDENING_PLAN.md (stability)
"""

import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ragtools.config import Settings
from ragtools.service import app as app_module
from ragtools.service.app import create_app
from ragtools.service.owner import QdrantOwner


class _DeadClient:
    """A client whose engine has gone away."""

    def __init__(self, inner):
        self._inner = inner
        self.dead = False
        self.probes = 0

    def get_collections(self, *a, **kw):
        self.probes += 1
        if self.dead:
            raise ConnectionError("[WinError 10061] connection refused")
        return self._inner.get_collections(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture
def env():
    with tempfile.TemporaryDirectory() as td:
        settings = Settings(
            content_root=td,
            qdrant_path=str(Path(td) / "q"),
            state_db=str(Path(td) / "s.db"),
            data_dir=str(Path(td) / "d"),
        )
        client = _DeadClient(Settings.get_memory_client())
        owner = QdrantOwner(settings=settings, client=client)
        app_module._owner = owner
        app_module._settings = settings
        try:
            with TestClient(create_app()) as tc:
                yield tc, owner, client
        finally:
            app_module._owner = None
            app_module._settings = None
            owner.close()


def test_a_healthy_store_reports_reachable(env):
    tc, _owner, _client = env
    body = tc.get("/health").json()
    assert body["storage_reachable"] is True
    # Assert about STORAGE specifically: `degraded` also covers the watcher,
    # which is not running in this test app.
    assert "storage_unreachable" not in body["issues"]


def test_a_dead_store_is_reported_as_degraded(env):
    """The whole point: the process is fine, the store is not."""
    tc, owner, client = env
    client.dead = True
    owner._storage_probe = None          # bypass the TTL cache for the test

    body = tc.get("/health").json()
    assert body["storage_reachable"] is False
    assert body["degraded"] is True
    assert "storage_unreachable" in body["issues"]
    assert "10061" in body["storage_error"] or "Connection" in body["storage_error"]


def test_health_still_returns_200_when_the_store_is_down(env):
    """Liveness is about the process. A 5xx here would make a supervisor kill a
    service that is perfectly capable of reporting what is wrong."""
    tc, owner, client = env
    client.dead = True
    owner._storage_probe = None
    r = tc.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_recovery_is_noticed(env):
    tc, owner, client = env
    client.dead = True
    owner._storage_probe = None
    assert tc.get("/health").json()["storage_reachable"] is False

    client.dead = False
    owner._storage_probe = None
    assert tc.get("/health").json()["storage_reachable"] is True


def test_the_probe_is_cached_so_polling_health_is_cheap(env):
    """/health is polled every few seconds by the panel and by monitors."""
    tc, owner, client = env
    owner._storage_probe = None
    client.probes = 0
    for _ in range(10):
        tc.get("/health")
    assert client.probes <= 2, (
        f"{client.probes} engine probes for 10 /health calls — the cache is not "
        "working, and polling health now loads the engine"
    )


def test_a_probe_failure_never_breaks_liveness(env):
    """If the probe itself explodes, /health must still answer."""
    tc, owner, _client = env

    def boom():
        raise RuntimeError("probe blew up")

    owner.storage_reachable = boom
    r = tc.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_owner_probe_returns_a_reason(env):
    _tc, owner, client = env
    client.dead = True
    owner._storage_probe = None
    ok, detail = owner.storage_reachable()
    assert ok is False
    assert detail, "no reason given for an unreachable store"
