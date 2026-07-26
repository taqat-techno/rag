"""S4 e2e — the managed Qdrant lifecycle against a REAL pinned binary.

The unit tests (test_storage_managed.py) prove the supervisor's injectable logic
with fake spawn/http. This proves the other half: that a real qdrant 1.15.5
process, started from the generated loopback config, becomes ready, reports the
pinned version, serves a vector round-trip through ``QdrantClient(url=...)``, and
stops cleanly. Gate G4's acceptance ("per-project backup/restore work under the
managed backend") rests on this actually running.

Resource-gated: skipped unless ``RAG_E2E_QDRANT=1`` and ``RAG_E2E_QDRANT_BIN``
points at the binary. Never touches live :21420 / :21422 — its own ports and a
private storage dir.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S4 -> G4)
"""

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("RAG_E2E_QDRANT"),
    reason="managed-Qdrant e2e is resource-gated; set RAG_E2E_QDRANT=1 to run",
)

BIN = os.environ.get("RAG_E2E_QDRANT_BIN", "")
HTTP_PORT = int(os.environ.get("RAG_E2E_QDRANT_HTTP", "26333"))
GRPC_PORT = int(os.environ.get("RAG_E2E_QDRANT_GRPC", "26334"))


def _write_config(cfg: dict, path) -> str:
    import yaml

    text = yaml.safe_dump(cfg, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_managed_qdrant_real_lifecycle_and_roundtrip(tmp_path):
    import httpx
    import time

    from ragtools.storage_managed import (
        PINNED_QDRANT_VERSION,
        QdrantSupervisor,
        generate_qdrant_config,
        resolve_qdrant_asset,
    )

    assert BIN and os.path.exists(BIN), "RAG_E2E_QDRANT_BIN must point at qdrant.exe"
    # Platform must be one the manager supports (this machine: windows/amd64).
    import platform
    assert resolve_qdrant_asset(platform.system(), platform.machine()) is not None

    storage = tmp_path / "storage"
    snapshots = tmp_path / "snapshots"
    storage.mkdir()
    snapshots.mkdir()
    cfg = generate_qdrant_config(
        storage_path=str(storage),
        http_port=HTTP_PORT,
        grpc_port=GRPC_PORT,
        snapshots_path=str(snapshots),
    )
    # The generated config binds loopback and disables telemetry — assert that
    # before we trust the running server.
    assert cfg["service"]["host"] == "127.0.0.1"
    assert cfg["telemetry_disabled"] is True
    config_path = _write_config(cfg, tmp_path / "qdrant.yaml")

    sup = QdrantSupervisor(
        binary_path=BIN,
        storage_path=str(storage),
        http_port=HTTP_PORT,
        grpc_port=GRPC_PORT,
        config_path=config_path,
        http_get=httpx.get,
        sleep=time.sleep,
        # tmp_path is not synced; keep the real detector so the pre-flight runs.
    )

    try:
        sup.start()
        assert sup.wait_ready(timeout=45, interval=0.5) is True
        assert sup.verify_version() == PINNED_QDRANT_VERSION  # 1.15.5, or refuse

        # Real vector round-trip through the managed server (server mode).
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams, PointStruct

        client = QdrantClient(url=sup.base_url)
        coll = f"e2e_{uuid.uuid4().hex[:8]}"
        client.create_collection(
            collection_name=coll,
            vectors_config=VectorParams(size=4, distance=Distance.COSINE),
        )
        client.upsert(
            collection_name=coll,
            points=[
                PointStruct(id=1, vector=[0.1, 0.2, 0.3, 0.4], payload={"tag": "a"}),
                PointStruct(id=2, vector=[0.9, 0.8, 0.7, 0.6], payload={"tag": "b"}),
            ],
        )
        hits = client.query_points(
            collection_name=coll, query=[0.1, 0.2, 0.3, 0.4], limit=1
        ).points
        assert hits and hits[0].payload["tag"] == "a"

        # Managed server persisted both points.
        assert client.get_collection(coll).points_count == 2
        client.close()
    finally:
        sup.stop()
