"""Pluggable vector-storage backend (RAG v3, Stage S3).

Every Qdrant access flows through a :class:`StorageBackend` so the product can
*ask* what a backend supports (:meth:`StorageBackend.capabilities`) instead of
discovering limits at runtime. Embedded (local) mode silently no-ops payload
indexes and raises on snapshots; a real server supports both. Callers gate
capability-dependent features (snapshots, payload indexes, quantization) on
``capabilities()`` rather than assuming.

Backends:

* ``embedded``  — ``QdrantClient(path=...)``; exclusive lock, brute-force search.
* ``managed``   — ragtools-supervised native server (lands in S4).
* ``external``  — a separately-run ``QdrantClient(url=..., api_key=...)``.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S3 -> G3)
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

import httpx
from qdrant_client import QdrantClient


#: The HTTP connection pool, stated rather than inherited.
#:
#: qdrant-client's default is not neutral. When the host is ``localhost`` or
#: ``127.0.0.1`` — which is EVERY managed engine and almost every external one
#: here — ``QdrantRemote.__init__`` substitutes
#: ``Limits(max_connections=None, max_keepalive_connections=0)``, disabling
#: keep-alive on the theory that a local hop is cheap. The consequence is that
#: every request opens a brand-new TCP socket and closes it, and on Windows each
#: of those sits in TIME_WAIT for minutes against a dynamic range only 16,384
#: wide. A 17-minute rebuild issuing an upsert per batch and a delete per batch
#: walks that range and then cannot connect:
#:
#:     [WinError 10048] Only one usage of each socket address ... is permitted
#:
#: So the value that matters here is ``max_keepalive_connections`` being above
#: zero at all: a reused connection costs no port. ``max_connections`` bounds
#: the burst — the inherited default is effectively unbounded (2**63-1) — and
#: ``keepalive_expiry`` must outlast the gap between two upserts, which is one
#: encode of a batch, not milliseconds.
#:
#: Raising the OS port range instead is not a fix: it is per-machine, needs
#: administrator rights, is not portable off Windows, and only moves the wall.
#:
#: A connection the server has already closed is still possible (Qdrant's own
#: idle timeout is shorter than this one). httpcore drops those on checkout, and
#: the race that slips through raises ``RemoteProtocolError`` — which
#: :func:`ragtools.transport.is_retryable` allow-lists for exactly this reason.
_HTTP_LIMITS = httpx.Limits(
    max_connections=32,
    max_keepalive_connections=32,
    keepalive_expiry=30.0,
)


@dataclass(frozen=True)
class Capabilities:
    """What a storage backend actually supports. Callers check before offering."""

    hnsw: bool
    payload_indexes: bool
    snapshots: bool
    quantization: bool
    sparse_vectors: bool
    named_vectors: bool
    concurrent_readers: bool
    server_version: Optional[str] = None
    max_collections: Optional[int] = None


#: Local/embedded mode limits, per the official qdrant-client source: search is
#: brute force (no HNSW), payload indexes are a silent no-op, snapshots raise,
#: no quantization; sparse vectors ARE supported. One process holds an
#: exclusive file lock, so no concurrent readers.
_EMBEDDED_CAPS = Capabilities(
    hnsw=False,
    payload_indexes=False,
    snapshots=False,
    quantization=False,
    sparse_vectors=True,
    named_vectors=True,
    concurrent_readers=False,
)

#: A real Qdrant server (managed or external) supports the full feature set.
_SERVER_CAPS = Capabilities(
    hnsw=True,
    payload_indexes=True,
    snapshots=True,
    quantization=True,
    sparse_vectors=True,
    named_vectors=True,
    concurrent_readers=True,
)


class StorageBackend(abc.ABC):
    """One interface for every vector-store interaction."""

    mode: str

    @abc.abstractmethod
    def capabilities(self) -> Capabilities:
        """What this backend supports — checked before capability-gated work."""

    @abc.abstractmethod
    def client(self) -> QdrantClient:
        """Return (constructing/caching if needed) the Qdrant client."""

    def close(self) -> None:
        """Release the client (and any exclusive lock). Idempotent."""


class EmbeddedBackend(StorageBackend):
    """``QdrantClient(path=...)`` — exclusive lock, brute-force, no snapshots."""

    mode = "embedded"

    def __init__(self, path: str):
        self._path = path
        self._client: Optional[QdrantClient] = None

    def capabilities(self) -> Capabilities:
        return _EMBEDDED_CAPS

    def client(self) -> QdrantClient:
        if self._client is None:
            # ``:memory:`` is a sentinel, not a real path — do not mkdir it.
            if self._path != ":memory:":
                Path(self._path).mkdir(parents=True, exist_ok=True)
            self._client = QdrantClient(path=self._path)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            # ``del`` is how local mode releases its exclusive file lock.
            try:
                del self._client
            finally:
                self._client = None


class ExternalBackend(StorageBackend):
    """A separately-managed Qdrant server reached over HTTP/gRPC."""

    mode = "external"

    def __init__(self, url: str, api_key: Optional[str] = None):
        if not url:
            raise ValueError("external storage backend requires a url")
        self._url = url
        self._api_key = api_key
        self._client: Optional[QdrantClient] = None

    def capabilities(self) -> Capabilities:
        return _SERVER_CAPS

    def client(self) -> QdrantClient:
        if self._client is None:
            self._client = QdrantClient(
                url=self._url, api_key=self._api_key, limits=_HTTP_LIMITS,
            )
        return self._client

    def close(self) -> None:
        # ``close()`` releases the httpx pool — and with it every socket still
        # held open by keep-alive. Skipping it leaves them to the garbage
        # collector, which is the one thing keep-alive must not depend on.
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


class ManagedBackend(StorageBackend):
    """A ragtools-supervised native Qdrant server (S4).

    Like :class:`ExternalBackend` over the wire, but ragtools owns the process
    (started by :class:`ragtools.storage_managed.QdrantSupervisor` at service
    boot) and the engine version is *pinned*, so capabilities carry that version.
    This backend only *connects* — it never spawns; lifecycle is the service's.
    """

    mode = "managed"

    def __init__(self, url: str, *, pinned_version: Optional[str] = None,
                 api_key: Optional[str] = None):
        if not url:
            raise ValueError(
                "managed storage backend requires the supervised server's url "
                "(storage_url); it never silently falls back to embedded"
            )
        self._url = url
        self._pinned_version = pinned_version
        self._api_key = api_key
        self._client: Optional[QdrantClient] = None

    def capabilities(self) -> Capabilities:
        return replace(_SERVER_CAPS, server_version=self._pinned_version)

    def client(self) -> QdrantClient:
        if self._client is None:
            # The managed engine is ALWAYS on 127.0.0.1, so this is the backend
            # the inherited keep-alive-disabling default hits every time.
            self._client = QdrantClient(
                url=self._url, api_key=self._api_key, limits=_HTTP_LIMITS,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


def resolve_backend(settings) -> StorageBackend:
    """Select the storage backend from ``settings.storage_backend``.

    Refuses an unknown mode and never silently falls back to embedded — a
    misconfigured ``managed``/``external`` is a ``ValueError``, not a downgrade.
    """
    mode = (getattr(settings, "storage_backend", "embedded") or "embedded").strip().lower()
    if mode == "embedded":
        return EmbeddedBackend(settings.qdrant_path)
    if mode == "external":
        return ExternalBackend(
            getattr(settings, "storage_url", None),
            getattr(settings, "storage_api_key", None),
        )
    if mode == "managed":
        from ragtools.storage_managed import PINNED_QDRANT_VERSION

        return ManagedBackend(
            getattr(settings, "storage_url", None),
            pinned_version=PINNED_QDRANT_VERSION,
            api_key=getattr(settings, "storage_api_key", None),
        )
    raise ValueError(
        f"unknown storage_backend {mode!r}; expected one of "
        "'embedded', 'managed', 'external'"
    )
