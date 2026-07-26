"""S3 — StorageBackend abstraction.

Every Qdrant access must flow through one interface that can be *asked* what it
supports (``capabilities()``) rather than discovering limits at runtime. This
is load-bearing: embedded (local) mode silently no-ops payload indexes and
raises on snapshots, so the product must know before offering a snapshot button.

Capabilities encode the OFFICIAL local-mode limits (brute-force, no payload
indexes, no snapshots, no quantization; sparse IS supported) vs a real server.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S3 -> G3)
"""

import dataclasses

import pytest

from ragtools.config import Settings
from ragtools.storage import (
    Capabilities,
    EmbeddedBackend,
    ExternalBackend,
    ManagedBackend,
    StorageBackend,
    resolve_backend,
)
from ragtools.storage_managed import PINNED_QDRANT_VERSION


def test_capabilities_is_frozen():
    caps = Capabilities(
        hnsw=False,
        payload_indexes=False,
        snapshots=False,
        quantization=False,
        sparse_vectors=True,
        named_vectors=True,
        concurrent_readers=False,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.hnsw = True  # type: ignore[misc]


def test_embedded_capabilities_reflect_local_mode_limits():
    caps = EmbeddedBackend(":memory:").capabilities()
    # Official local-mode facts (qdrant-client source): brute force, payload
    # indexes are a no-op, no snapshots, no quantization; sparse works.
    assert caps.hnsw is False
    assert caps.payload_indexes is False
    assert caps.snapshots is False
    assert caps.quantization is False
    assert caps.sparse_vectors is True
    assert caps.concurrent_readers is False


def test_external_capabilities_are_full():
    caps = ExternalBackend(url="http://127.0.0.1:6333").capabilities()
    assert caps.hnsw is True
    assert caps.payload_indexes is True
    assert caps.snapshots is True
    assert caps.quantization is True
    assert caps.concurrent_readers is True


def test_embedded_client_creates_and_lists_collections(tmp_path):
    from qdrant_client.models import Distance, VectorParams

    backend = EmbeddedBackend(str(tmp_path / "qd"))
    assert isinstance(backend, StorageBackend)
    client = backend.client()
    client.create_collection(
        "t", vectors_config=VectorParams(size=4, distance=Distance.COSINE)
    )
    assert "t" in [c.name for c in client.get_collections().collections]
    backend.close()


def test_resolve_backend_defaults_to_embedded():
    backend = resolve_backend(Settings())
    assert isinstance(backend, EmbeddedBackend)
    assert backend.mode == "embedded"


def test_resolve_backend_external_from_settings():
    s = Settings(storage_backend="external", storage_url="http://127.0.0.1:6333")
    backend = resolve_backend(s)
    assert isinstance(backend, ExternalBackend)
    assert backend.mode == "external"


def test_resolve_backend_managed_from_settings():
    # S4: managed is now a ragtools-supervised local server — server caps, the
    # pinned engine version, reached at the supervised URL.
    s = Settings(storage_backend="managed", storage_url="http://127.0.0.1:26333")
    backend = resolve_backend(s)
    assert isinstance(backend, ManagedBackend)
    assert backend.mode == "managed"
    caps = backend.capabilities()
    assert caps.snapshots and caps.hnsw and caps.concurrent_readers
    assert caps.server_version == PINNED_QDRANT_VERSION


def test_resolve_backend_managed_requires_a_target():
    # Managed must know where its supervised server is — never a silent embedded
    # fallback when misconfigured.
    with pytest.raises(ValueError):
        resolve_backend(Settings(storage_backend="managed"))


def test_resolve_backend_rejects_unknown_mode():
    with pytest.raises(ValueError):
        resolve_backend(Settings(storage_backend="bogus"))


def test_get_qdrant_client_routes_through_backend(tmp_path):
    # The single production constructor now delegates to the backend.
    from qdrant_client.models import Distance, VectorParams

    s = Settings(qdrant_path=str(tmp_path / "qd"))
    client = s.get_qdrant_client()
    client.create_collection(
        "x", vectors_config=VectorParams(size=4, distance=Distance.COSINE)
    )
    assert "x" in [c.name for c in client.get_collections().collections]
