"""S2 — data-directory separation.

v2.7.0 overloaded ``qdrant_path`` as both the vector-store path and the
application data-directory anchor: ~10 modules derived logs/pids/backups/
markers from ``Path(qdrant_path).parent``, and in dev mode the defaults were
CWD-relative strings so ``RAG_DATA_DIR`` was a no-op for storage. This pins
``data_dir`` as the single authoritative anchor: honored via ``RAG_DATA_DIR``
in EVERY mode, absolute, with qdrant/state defaulting under it.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S2 -> G2)
"""

from pathlib import Path

from ragtools.config import Settings


def test_data_dir_honors_rag_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("RAG_DATA_DIR", str(tmp_path))
    s = Settings()
    assert Path(s.data_dir) == tmp_path
    # qdrant + state default UNDER the data dir (canonical layout).
    assert Path(s.qdrant_path) == tmp_path / "qdrant"
    assert Path(s.state_db) == tmp_path / "index_state.db"


def test_rag_data_dir_redirects_storage_in_dev(monkeypatch, tmp_path):
    # The B10 fix: RAG_DATA_DIR must redirect the STORAGE path even in a
    # source (dev) checkout — previously it was ignored for qdrant/state.
    monkeypatch.setenv("RAG_DATA_DIR", str(tmp_path / "isolated"))
    s = Settings()
    assert Path(s.qdrant_path).is_relative_to(tmp_path / "isolated")


def test_data_dir_explicit_override(tmp_path):
    s = Settings(data_dir=str(tmp_path / "explicit"))
    assert Path(s.data_dir) == tmp_path / "explicit"


def test_data_dir_is_absolute(monkeypatch):
    # No RAG_DATA_DIR: the dev default must still be an ABSOLUTE path, not a
    # CWD-relative string (so running from another dir can't split the anchor).
    monkeypatch.delenv("RAG_DATA_DIR", raising=False)
    s = Settings()
    assert Path(s.data_dir).is_absolute()


def test_log_anchor_follows_data_dir_not_qdrant_path(tmp_path):
    # A9: the log dir follows data_dir even when qdrant_path/state_db are
    # overridden to divergent locations — no more split anchors.
    from ragtools.service.logs import _logs_dir

    s = Settings(
        data_dir=str(tmp_path / "dd"),
        qdrant_path=str(tmp_path / "elsewhere" / "qdrant"),
        state_db=str(tmp_path / "other" / "state.db"),
    )
    assert _logs_dir(s) == (tmp_path / "dd") / "logs"
