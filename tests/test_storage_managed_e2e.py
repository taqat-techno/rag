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

pytestmark = [
    # Registered in pyproject.toml. The `managed-qdrant-e2e` job fetches the
    # pinned binary on all three first-class platforms and REQUIRES both tests
    # below to pass; `scripts/check_no_silent_skips.py` fails the build if
    # either reports skipped there.
    pytest.mark.e2e_managed_qdrant,
    pytest.mark.release_blocking,
    pytest.mark.skipif(
        not os.environ.get("RAG_E2E_QDRANT"),
        reason="managed-Qdrant e2e is resource-gated; set RAG_E2E_QDRANT=1 to run",
    ),
]

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


def test_the_real_engine_actually_writes_to_its_log(tmp_path):
    """The fix for the defect that made two crashes undiagnosable, proven against
    the real binary rather than a spawn double.

    Through v3.2.0 the engine was spawned with no ``stdout=``, so it inherited
    the parent's handles — and under the windowed launcher those do not exist,
    so CPython handed it a pipe with no reader and every line it wrote failed
    with ERROR_BROKEN_PIPE. Nothing the engine said about its own death survived.

    A unit test can show that a *file object* was passed. Only this can show that
    a real Qdrant, started the way the product starts it, puts real bytes in the
    file.
    """
    import time

    import httpx

    from ragtools.storage_managed import (
        QdrantSupervisor,
        engine_log_path,
        generate_qdrant_config,
    )

    assert BIN and os.path.exists(BIN), "RAG_E2E_QDRANT_BIN must point at qdrant.exe"

    data_dir = tmp_path / "data"
    storage = data_dir / "qdrant-server"
    storage.mkdir(parents=True)
    port, grpc = HTTP_PORT + 2, GRPC_PORT + 2
    config_path = _write_config(
        generate_qdrant_config(storage_path=str(storage), http_port=port,
                               grpc_port=grpc,
                               snapshots_path=str(data_dir / "snapshots")),
        tmp_path / "qdrant-log.yaml")

    sup = QdrantSupervisor(
        binary_path=BIN, storage_path=str(storage),
        http_port=port, grpc_port=grpc, config_path=config_path,
        data_dir=str(data_dir), http_get=httpx.get, sleep=time.sleep)

    try:
        sup.start()
        assert sup.wait_ready(timeout=45, interval=0.5) is True

        log = engine_log_path(data_dir)
        assert sup.log_path == str(log)
        assert not sup.log_error, f"the engine log was unavailable: {sup.log_error}"

        # Give the engine a moment to flush its startup banner.
        for _ in range(50):
            if log.is_file() and log.stat().st_size > 0:
                break
            time.sleep(0.1)

        assert log.is_file(), f"no engine log was created at {log}"
        size = log.stat().st_size
        assert size > 0, (
            "the engine log exists but is EMPTY — the engine's output is still "
            "going somewhere other than the file we gave it")

        text = log.read_text(encoding="utf-8", errors="replace").lower()
        assert "qdrant" in text or "access web ui" in text or "http" in text, (
            f"the log holds {size} bytes but nothing recognisable as Qdrant "
            f"output:\n{text[:400]}")
    finally:
        sup.stop()
