"""The registry is the swap primitive for a safe rebuild.

v3.4's rebuild dropped every collection and deleted the state DB BEFORE
indexing, so a failure at project 14 of 15 left the machine with less than it
started with. A safe rebuild has to build the replacement first and only then
point the project at it — which needs an atomic, durable, portable way to say
"this project now reads that collection".

That primitive already existed and was not being used as one:
``registry.projects.collection_name`` is a single SQLite UPDATE in a WAL
database. Qdrant aliases were evaluated and rejected — the embedded backend
(the product default) implements them as an in-memory dict with a deferred
save, so they are not crash-atomic exactly where it matters most, and Qdrant
has no rename at all.
"""

from __future__ import annotations

import sqlite3

import pytest

from ragtools.identity import project_collection_name
from ragtools.registry import ProjectRegistry


@pytest.fixture
def reg(tmp_path):
    r = ProjectRegistry(str(tmp_path / "registry.db"))
    yield r
    r.close()


def test_a_new_project_starts_at_generation_zero(reg):
    """Generation 0 keeps the EXISTING name, so upgrading moves no data."""
    rec = reg.add("alpha", path="/tmp/alpha")
    assert rec.generation == 0
    assert rec.collection_name == project_collection_name(rec.uuid)


def test_activating_a_generation_repoints_the_project(reg):
    """The swap: build under a new name, verify, then flip the pointer."""
    rec = reg.add("alpha", path="/tmp/alpha")
    original = rec.collection_name

    staged = f"{original}_g1"
    updated = reg.set_active_collection(rec.uuid, staged, generation=1)

    assert updated.collection_name == staged
    assert updated.generation == 1
    # and it is durable, not just in memory
    assert reg.get("alpha").collection_name == staged
    assert reg.get("alpha").collection_name != original


def test_the_swap_is_a_single_statement(reg, tmp_path):
    """Atomicity is the point.

    A swap that is two writes can be interrupted between them, which is how a
    project ends up pointing at a collection that was never finished.
    """
    rec = reg.add("alpha", path="/tmp/alpha")
    db = str(tmp_path / "registry.db")

    conn = sqlite3.connect(db)
    try:
        before = conn.execute(
            "SELECT collection_name, generation FROM projects WHERE uuid = ?",
            (rec.uuid,)).fetchone()
    finally:
        conn.close()
    assert before[1] == 0

    reg.set_active_collection(rec.uuid, "proj_staged", generation=7)

    conn = sqlite3.connect(db)
    try:
        after = conn.execute(
            "SELECT collection_name, generation FROM projects WHERE uuid = ?",
            (rec.uuid,)).fetchone()
    finally:
        conn.close()
    assert after == ("proj_staged", 7)


def test_generation_survives_rename_and_move(reg):
    """Identity is the UUID. Renaming or moving a project must not reset it."""
    rec = reg.add("alpha", path="/tmp/alpha")
    reg.set_active_collection(rec.uuid, "proj_staged", generation=3)

    reg.rename("alpha", "alpha-renamed")
    reg.move("alpha-renamed", "/tmp/elsewhere")

    moved = reg.get("alpha-renamed")
    assert moved.collection_name == "proj_staged"
    assert moved.generation == 3


def test_an_existing_v34_registry_gains_the_column(tmp_path):
    """Upgrade path.

    ``CREATE TABLE IF NOT EXISTS`` does not add a column to a table that
    already exists — the exact trap ``relayout._add_retry_columns`` was written
    for. A v3.4 registry must gain ``generation`` on open, defaulting to 0 so
    every existing project keeps reading the collection it already has.
    """
    db = str(tmp_path / "registry.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE projects (
            uuid            TEXT PRIMARY KEY,
            project_id      TEXT UNIQUE NOT NULL,
            display_name    TEXT,
            path            TEXT,
            mode            TEXT NOT NULL DEFAULT 'docs',
            collection_name TEXT NOT NULL,
            created_at      TEXT,
            archived        INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "INSERT INTO projects (uuid, project_id, display_name, path, mode, "
        "collection_name, created_at, archived) VALUES (?,?,?,?,?,?,?,0)",
        ("u-1", "legacy", "Legacy", "/tmp/legacy", "docs", "proj_abc", "2026-01-01"),
    )
    conn.commit()
    conn.close()

    r = ProjectRegistry(db)
    try:
        rec = r.get("legacy")
        assert rec is not None
        assert rec.generation == 0, "an upgraded row must keep its current collection"
        assert rec.collection_name == "proj_abc"
    finally:
        r.close()
