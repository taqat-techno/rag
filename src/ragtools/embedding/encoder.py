"""Embedding model wrapper using Sentence Transformers.

**Offline-first, because the model is already here.** The installer ships a
complete Hugging Face cache inside the bundle (``rag.spec`` adds
``build/model_cache``), and yet v3.3.0 constructed ``SentenceTransformer`` with a
bare Hub repo id and no offline flag. A bare id is resolved *against the Hub* on
every construction, cache or no cache — the stack re-validates even files it has
already recorded as absent in ``.no_exist``. On ``LAKOSHA-TAQAT`` that produced

    HEAD https://huggingface.co/.../adapter_config.json
    [Errno 11001] getaddrinfo failed

during service startup, which uvicorn turned into ``SystemExit: 3`` and the crash
recorder wrote down as ``SystemExit``. A DNS blip took the whole product down and
left a crash marker that named neither DNS nor the model.

So: find the local copy, load it with the network switched off, and reach for the
Hub only on a genuine, classified cache miss. When the model cannot be had at
all, raise :class:`ModelUnavailable` — a named failure the crash recorder can
attribute to the encoder, rather than an anonymous exit code that reads like a
storage outage.
"""

import contextlib
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

logger = logging.getLogger("ragtools.service")

_QUERY_CACHE_SIZE = 128

#: Set while a local-only load is attempted. ``local_files_only=True`` covers the
#: documented path; the rest of the Hugging Face stack reads these directly, and
#: a single component that misses the keyword argument is enough to reach the
#: network — which is the entire failure being fixed.
_OFFLINE_ENV = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")

#: Opt out of the network fallback entirely. Set by tests, and available to
#: anyone who would rather a missing model fail loudly than download silently.
OFFLINE_ONLY_ENV = "RAG_EMBEDDING_OFFLINE_ONLY"


class ModelUnavailable(RuntimeError):
    """The embedding model could not be loaded. Distinct from a storage failure.

    Named on purpose. This condition used to reach the operator as
    ``SystemExit: 3`` with the real cause absent from ``last_crash.json``, so it
    was indistinguishable from the engine dying. A type lets the crash recorder
    say ``subsystem: encoder`` and the health payload say ``model_unavailable``.
    """

    def __init__(self, model, searched, cause=None):
        self.model = model
        self.searched = [str(s) for s in searched]
        self.cause = cause
        detail = f" ({type(cause).__name__}: {cause})" if cause is not None else ""
        super().__init__(
            f"the embedding model {model!r} could not be loaded{detail}. "
            f"Searched: {', '.join(self.searched) or 'the default cache'}. "
            f"This is a MODEL failure, not a storage failure."
        )


def packaged_model_cache():
    """The model cache shipped inside the installation, or ``None``."""
    import sys

    if not getattr(sys, "frozen", False):
        return None
    try:
        candidate = Path(sys.executable).resolve().parent / "model_cache"
    except OSError:
        return None
    return candidate if candidate.is_dir() else None


def resolve_model_cache():
    """Where the model actually lives on this machine, or ``None`` for the default.

    ONE resolver, reached by every entry point. This lived in ``run.py:main`` as
    an ``os.environ.setdefault`` guarded by ``sys.frozen``, so the *service*
    found the bundled model while the MCP server and the CLI — different
    processes of the same installation — looked in the user's home cache and
    went to the network.
    """
    packaged = packaged_model_cache()
    if packaged is not None:
        return packaged
    for var in ("SENTENCE_TRANSFORMERS_HOME", "HF_HUB_CACHE", "HF_HOME"):
        raw = (os.environ.get(var) or "").strip()
        if raw:
            try:
                path = Path(raw)
            except (OSError, ValueError):
                continue
            if path.is_dir():
                return path
    return None


@contextlib.contextmanager
def _network_disabled():
    """Switch the Hugging Face stack off the network for the duration."""
    previous = {key: os.environ.get(key) for key in _OFFLINE_ENV}
    for key in _OFFLINE_ENV:
        os.environ[key] = "1"
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def offline_only() -> bool:
    return (os.environ.get(OFFLINE_ONLY_ENV) or "").strip().lower() in {"1", "true", "yes"}


def _construct(model_name, cache_folder, *, local_only):
    kwargs = {"device": "cpu", "local_files_only": local_only}
    if cache_folder is not None:
        kwargs["cache_folder"] = str(cache_folder)
    return SentenceTransformer(model_name, **kwargs)


def load_model(model_name, *, cache_folder=None, allow_network=None):
    """Load ``model_name`` without touching the network when it is already here.

    Order is the whole point:

    1. **Local only.** If the model is present, this succeeds and performs no DNS
       lookup and no HTTP request — the cache's ``.no_exist`` markers are honoured
       instead of re-validated.
    2. **Network**, and only then, and only when it is permitted. A cache miss is
       a legitimate reason to download; a DNS failure on a machine that already
       has the model is not.
    3. **Fail with the cause attached.** Both attempts' exceptions are preserved
       so the crash record can name the real one.
    """
    cache = cache_folder if cache_folder is not None else resolve_model_cache()
    searched = [cache] if cache is not None else []
    if allow_network is None:
        allow_network = not offline_only()

    try:
        with _network_disabled():
            model = _construct(model_name, cache, local_only=True)
        logger.info("Encoder %s loaded from the local cache (%s); no network was used",
                    model_name, cache or "default")
        return model
    except Exception as local_exc:  # noqa: BLE001 — classified below, never swallowed
        if not allow_network:
            raise ModelUnavailable(model_name, searched, local_exc) from local_exc
        logger.warning(
            "Encoder %s is not in the local cache (%s: %s); falling back to the "
            "Hugging Face Hub. This is the only path that needs the network.",
            model_name, type(local_exc).__name__, local_exc)

    try:
        model = _construct(model_name, cache, local_only=False)
    except Exception as net_exc:  # noqa: BLE001
        raise ModelUnavailable(model_name, searched, net_exc) from net_exc
    logger.info("Encoder %s downloaded to %s", model_name, cache or "the default cache")
    return model


class Encoder:
    """Thin wrapper around SentenceTransformer for consistent encoding."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        self.model = load_model(model_name)
        self.dimension = self.model.get_sentence_embedding_dimension()
        self._query_cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._cache_lock = threading.Lock()

    def metadata(self):
        """Declare this backend's model identity for collection enforcement (A5).

        Satisfies :class:`ragtools.embedding.backend.EmbeddingBackend`; the
        returned record is what :func:`~ragtools.embedding.backend.assert_model_compatible`
        checks on every collection open. ``normalize`` is always True here —
        :meth:`encode_batch` / :meth:`encode_query` pass ``normalize_embeddings=True``.
        """
        from ragtools.embedding.backend import ModelMetadata

        return ModelMetadata(
            model_name=self.model_name, dimension=self.dimension, normalize=True
        )

    def encode_batch(self, texts: list[str], batch_size: int = 64) -> np.ndarray:
        """Encode a list of texts into normalized embeddings.

        Args:
            texts: List of strings to encode.
            batch_size: Batch size for encoding.

        Returns:
            numpy array of shape (len(texts), dimension)
        """
        return self.model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=len(texts) > 100,
            convert_to_numpy=True,
        )

    def encode_query(self, query: str) -> np.ndarray:
        """Encode a single query string with LRU caching. Thread-safe.

        Returns:
            numpy array of shape (dimension,)
        """
        with self._cache_lock:
            if query in self._query_cache:
                self._query_cache.move_to_end(query)
                return self._query_cache[query]

        embedding = self.model.encode(
            query,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        with self._cache_lock:
            self._query_cache[query] = embedding
            if len(self._query_cache) > _QUERY_CACHE_SIZE:
                self._query_cache.popitem(last=False)
        return embedding
