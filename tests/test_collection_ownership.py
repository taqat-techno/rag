"""Delete only what you can prove you made.

`obsolete_collections` computed ``existing - current``: every collection on the
server that this installation's registry did not recognise. `_retire_old_storage`
then deletes what it returns, and `rag storage reclaim` computed the same
difference.

On a shared engine that set is *the other installation's entire index*. The
v3.1.0 incident put two services on one Qdrant; the canonical index survived
only because validation never passed, so the destructive step was never reached.
The safety came from an unrelated bug.

The rule these tests pin: **"I do not recognise it" is never a reason to
delete.** A collection this installation cannot account for is reported and left
alone, because a `proj_<uuid>` with no registry row is indistinguishable from
another installation's live project.
"""

from __future__ import annotations

import types

from ragtools.upgrade import relayout


class FakeClient:
    def __init__(self, names):
        self._names = list(names)
        self.deleted = []

    def get_collections(self):
        return types.SimpleNamespace(
            collections=[types.SimpleNamespace(name=n) for n in self._names])

    def delete_collection(self, name):
        self.deleted.append(name)
        self._names.remove(name)


class FakeRouter:
    def __init__(self, current):
        self._current = list(current)

    def all_collections(self):
        return list(self._current)


class FakeFrameworks:
    def __init__(self, names):
        self._names = list(names)

    def all_frameworks(self):
        return [{"collection_name": n} for n in self._names]


def owner(*, on_server, current, shared="markdown_kb", frameworks=()):
    return types.SimpleNamespace(
        _client=FakeClient(on_server),
        router=FakeRouter(current),
        settings=types.SimpleNamespace(collection_name=shared),
        _frameworks=FakeFrameworks(frameworks) if frameworks else None,
    )


# --- the intended job still works ------------------------------------------


def test_the_old_shared_index_is_still_reclaimed():
    """The actual use case: after a shared -> per_project migration, the v2
    collection is ours, unused, and should go."""
    o = owner(on_server=["markdown_kb", "proj_aaa"], current=["proj_aaa"])

    assert relayout.obsolete_collections(o) == ["markdown_kb"]


def test_an_archived_project_is_not_reclaimed():
    """`all_collections` includes archived projects deliberately — archiving
    keeps the vectors."""
    o = owner(on_server=["proj_aaa", "proj_bbb"], current=["proj_aaa", "proj_bbb"])

    assert relayout.obsolete_collections(o) == []


# --- the boundary -----------------------------------------------------------


def test_another_installations_collections_are_never_returned():
    """The defect, asserted directly. `existing - current` would return both
    strangers, and `_retire_old_storage` deletes what this returns."""
    o = owner(on_server=["markdown_kb", "proj_mine", "proj_theirs", "fw_odoo_deadbeef"],
              current=["proj_mine"])

    assert relayout.obsolete_collections(o) == ["markdown_kb"]


def test_strangers_are_reported_rather_than_deleted():
    o = owner(on_server=["proj_mine", "proj_theirs", "somebody_elses_app"],
              current=["proj_mine"])

    assert relayout.unattributed_collections(o) == ["proj_theirs",
                                                    "somebody_elses_app"]


def test_retiring_leaves_a_foreign_collection_intact():
    """End to end through the destructive path."""
    o = owner(on_server=["markdown_kb", "proj_mine", "proj_theirs"],
              current=["proj_mine"])

    relayout._retire_old_storage(o)

    assert o._client.deleted == ["markdown_kb"]
    assert "proj_theirs" in o._client._names, (
        "it deleted another installation's index"
    )


def test_an_orphaned_project_collection_is_left_alone():
    """A `proj_<uuid>` with no registry row could be ours (project removed) or
    theirs (project live). Guessing wrong destroys somebody's index, so the
    honest answer is to name it and stop."""
    o = owner(on_server=["proj_orphan"], current=[])

    assert relayout.obsolete_collections(o) == []
    assert relayout.unattributed_collections(o) == ["proj_orphan"]


def test_a_framework_corpus_we_registered_is_ours():
    """Framework names are derived from build identity, so they are IDENTICAL
    across installations that vendor the same build. Registry membership — not
    the name — is what makes one ours."""
    o = owner(on_server=["fw_odoo_abc123", "proj_mine"], current=["proj_mine"],
              frameworks=["fw_odoo_abc123"])

    assert relayout.obsolete_collections(o) == ["fw_odoo_abc123"]


def test_an_identically_named_framework_we_did_not_register_is_not_ours():
    o = owner(on_server=["fw_odoo_abc123", "proj_mine"], current=["proj_mine"])

    assert relayout.obsolete_collections(o) == []
    assert "fw_odoo_abc123" in relayout.unattributed_collections(o)


def test_the_owned_set_includes_the_configured_shared_name():
    o = owner(on_server=[], current=[], shared="custom_kb")

    assert "custom_kb" in relayout.owned_collections(o)


def test_an_unreachable_server_deletes_nothing():
    class Exploding:
        def get_collections(self):
            raise ConnectionError("engine is down")

    o = types.SimpleNamespace(
        _client=Exploding(), router=FakeRouter([]),
        settings=types.SimpleNamespace(collection_name="markdown_kb"),
        _frameworks=None)

    assert relayout.obsolete_collections(o) == []
    assert relayout.unattributed_collections(o) == []
