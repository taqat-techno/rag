"""R06 — the registry became load-bearing without becoming durable.

v3.5.0 made ``registry.projects.collection_name`` the swap primitive: it is
what a rebuild flips to hand a project its new collection. That was the right
call — one atomic UPDATE, identical on every backend — but it raised the cost
of losing that one file without paying for it.

The failure it enables, end to end:

* ``registry.db`` is lost or corrupted;
* ``build_router`` recreates it empty and ``sync_projects_from_config`` mints a
  FRESH uuid4 per project, so 15 brand-new empty ``proj_<hex>`` collections
  appear beside the 15 that still hold the data;
* ``IndexIdentity`` carries storage_backend, collection_strategy,
  collection_name, model_name and dimension — **none of which changed** — so
  ``reconcile`` returns compatible and the incremental run trusts the state DB
  and skips every file.

Nothing raises. Search returns nothing. The dashboard reports the historical
counts. This is the same "confidently empty" shape the identity module was
written to prevent, arriving through the one door it does not watch.

Backup alone is not the fix: a restored backup is only correct if something
first NOTICES the mismatch, and ownership of a stray collection must be proven
rather than guessed from its name.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from ragtools.identity import project_collection_name
from ragtools.index_identity import IndexIdentity, current_identity, reconcile, stamp
from ragtools.indexing.state import IndexState
from ragtools.registry import ProjectRegistry


@pytest.fixture
def tmp(tmp_path):
    return tmp_path


@pytest.fixture
def reg(tmp):
    r = ProjectRegistry(str(tmp / "registry.db"))
    for pid in ("alpha", "beta", "gamma"):
        r.add(pid, path=str(tmp / pid))
    yield r
    r.close()


# --- 1. the identity must notice ------------------------------------------


def test_identity_covers_the_registry_mapping(reg, tmp):
    """A fingerprint of project -> collection is part of the store's identity.

    Without it every field of IndexIdentity is invariant to registry loss,
    which is exactly why the loss is silent.
    """
    ident = current_identity_with_registry(reg, tmp)
    assert getattr(ident, "registry_fingerprint", None), (
        "IndexIdentity does not carry a registry fingerprint, so losing the "
        "registry cannot be detected"
    )


def test_a_reminted_registry_is_detected_as_a_different_store(reg, tmp):
    """The whole point: new uuids => new collections => not the same store."""
    state = IndexState(str(tmp / "state.db"))
    try:
        before = current_identity_with_registry(reg, tmp)
        stamp(state, before)

        # Lose it, exactly as build_router would recreate it.
        reg.close()
        Path(tmp / "registry.db").unlink()
        fresh = ProjectRegistry(str(tmp / "registry.db"))
        try:
            for pid in ("alpha", "beta", "gamma"):
                fresh.add(pid, path=str(tmp / pid))
            after = current_identity_with_registry(fresh, tmp)
        finally:
            fresh.close()

        ok, reasons = reconcile(state, after)
        assert not ok, (
            "a registry whose every collection name changed reconciled as the "
            "SAME store; the incremental run would skip every file"
        )
        assert any("registry" in r.lower() for r in reasons), reasons
    finally:
        state.close()


def test_an_unchanged_registry_still_reconciles(reg, tmp):
    """The guard must not fire on a healthy install."""
    state = IndexState(str(tmp / "state.db"))
    try:
        ident = current_identity_with_registry(reg, tmp)
        stamp(state, ident)
        ok, reasons = reconcile(state, current_identity_with_registry(reg, tmp))
        assert ok, reasons
    finally:
        state.close()


# --- 2. ownership is proven, never guessed --------------------------------


def test_an_orphan_collection_is_re_adoptable_by_uuid(reg, tmp):
    """A `proj_<uuid>` whose registry row vanished belongs to a known project.

    Re-adoption reattaches it. Minting a replacement instead is what turns a
    recoverable loss into a permanent one.
    """
    from ragtools.registry import readopt_collection

    rec = reg.get("alpha")
    orphan = rec.collection_name
    reg.close()

    fresh = ProjectRegistry(str(tmp / "registry.db"))
    try:
        Path(tmp / "registry.db")  # recreated empty by the constructor
        adopted = readopt_collection(fresh, "alpha", orphan, path=str(tmp / "alpha"))
        assert adopted.collection_name == orphan, (
            "re-adoption minted a new collection instead of reclaiming the "
            "existing one"
        )
        assert adopted.uuid == rec.uuid, "re-adoption did not preserve identity"
    finally:
        fresh.close()


def test_ownership_is_not_inferred_from_the_name_alone(reg, tmp):
    """`proj_<32hex>` is a shape, not a claim of ownership.

    Another installation's collections match that shape exactly. Deciding
    ownership by pattern is how one install deletes another's index — the
    deny-list mistake the project already banned once.
    """
    from ragtools.registry import owns_collection

    mine = reg.get("beta").collection_name
    stranger = project_collection_name("ffffffff-ffff-4fff-8fff-ffffffffffff")

    assert owns_collection(reg, mine) is True
    assert owns_collection(reg, stranger) is False, (
        "a collection this installation never created was claimed as its own"
    )


# --- 3. the swap survives a crash -----------------------------------------


def test_the_pointer_swap_is_durable_across_a_reopen(reg, tmp):
    """One UPDATE is only atomic if it is also flushed.

    A swap that is committed in memory but lost on power failure leaves the
    project pointing at the collection it was replacing.
    """
    rec = reg.get("gamma")
    reg.set_active_collection(rec.uuid, "proj_staged_g1", generation=1)
    reg.close()

    reopened = ProjectRegistry(str(tmp / "registry.db"))
    try:
        after = reopened.get("gamma")
        assert after.collection_name == "proj_staged_g1"
        assert after.generation == 1
    finally:
        reopened.close()


def test_a_swap_interrupted_before_state_rows_is_detectable(reg, tmp):
    """Between the pointer flip and the state-row replacement there is a window.

    A crash there leaves the project serving the NEW collection while the state
    DB still describes the old one. That is `drifted`, and it must be
    detectable rather than silently trusted.
    """
    from ragtools.registry import registry_fingerprint

    before = registry_fingerprint(reg)
    rec = reg.get("alpha")
    reg.set_active_collection(rec.uuid, "proj_alpha_g1", generation=1)
    after = registry_fingerprint(reg)

    assert before != after, (
        "the fingerprint did not change when a project was repointed, so a "
        "half-completed swap cannot be told apart from a completed one"
    )


# --- helper ---------------------------------------------------------------


def current_identity_with_registry(registry, tmp):
    """Build the identity the way production must once R06 lands."""
    from types import SimpleNamespace
    settings = SimpleNamespace(
        storage_backend="embedded",
        collection_strategy="per_project",
        collection_name="markdown_kb",
        embedding_model="all-MiniLM-L6-v2",
        data_dir=str(tmp),
    )
    return current_identity(settings, 384, registry=registry)
