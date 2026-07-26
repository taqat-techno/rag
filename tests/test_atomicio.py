"""S1 / A4 — atomic file writes.

v2.7.0 wrote TOML config by dumping straight onto the live file
(``open(path, "wb"); tomli_w.dump``). An interruption truncates the owner's
entire project configuration. These pin an atomic writer: fully-formed bytes
are written to a temp file, fsynced, then ``os.replace``d (atomic on one
filesystem), so a failure can never leave a partial or empty target.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S1/A4 -> G1)
"""

import pytest

from ragtools.atomicio import atomic_write_bytes


def test_writes_and_reads_back(tmp_path):
    p = tmp_path / "c.toml"
    atomic_write_bytes(p, b"hello")
    assert p.read_bytes() == b"hello"


def test_overwrites_existing(tmp_path):
    p = tmp_path / "c.toml"
    p.write_bytes(b"OLD")
    atomic_write_bytes(p, b"NEW")
    assert p.read_bytes() == b"NEW"


def test_creates_parent_dirs(tmp_path):
    p = tmp_path / "a" / "b" / "c.toml"
    atomic_write_bytes(p, b"x")
    assert p.read_bytes() == b"x"


def test_leaves_no_temp_files_on_success(tmp_path):
    p = tmp_path / "c.toml"
    atomic_write_bytes(p, b"x")
    assert sorted(q.name for q in tmp_path.iterdir()) == ["c.toml"]


def test_failure_at_replace_leaves_original_and_no_temp(tmp_path, monkeypatch):
    """A simulated interruption at the replace step must not corrupt anything."""
    p = tmp_path / "c.toml"
    p.write_bytes(b"ORIGINAL")
    import ragtools.atomicio as aio

    def boom(src, dst):
        raise OSError("simulated interruption")

    monkeypatch.setattr(aio.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_bytes(p, b"CORRUPT")
    assert p.read_bytes() == b"ORIGINAL"  # untouched
    assert sorted(q.name for q in tmp_path.iterdir()) == ["c.toml"]  # temp cleaned


def test_backup_retains_previous_version(tmp_path):
    p = tmp_path / "c.toml"
    p.write_bytes(b"V1")
    atomic_write_bytes(p, b"V2", backup=True)
    assert p.read_bytes() == b"V2"
    assert (tmp_path / "c.toml.bak").read_bytes() == b"V1"


def test_backup_noop_when_no_existing_file(tmp_path):
    p = tmp_path / "c.toml"
    atomic_write_bytes(p, b"V1", backup=True)  # nothing to back up
    assert p.read_bytes() == b"V1"
    assert not (tmp_path / "c.toml.bak").exists()
