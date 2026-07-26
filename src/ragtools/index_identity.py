"""Does the state DB describe the store we are about to write to?

Incremental indexing is fast because it trusts ``index_state.db``: a file whose
hash is unchanged is skipped without re-embedding. That trust is only valid
while the state DB and the vector store describe the same index.

Switching the storage engine (``embedded`` -> ``managed``) or the collection
layout (``shared`` -> ``per_project``) points ragtools at a **different, empty
store** while leaving the state DB untouched. The next incremental run then
reads "38,286 files already indexed", skips every one, and reports success —
against a store that holds a fraction of them. Observed exactly that during the
live cutover on this machine:

    state DB : 38,213 files / 88,825 chunks
    Qdrant   : 60,930 points
    result   : "indexed 0, skipped 38286"  ← ~28k chunks silently missing

Nothing was corrupt; the state DB was simply describing the *previous* store.
So the state DB now records which store it describes, and a mismatch means the
file hashes cannot be trusted — the run must re-index rather than skip.

This composes with, and does not replace,
:func:`ragtools.embedding.backend.assert_model_compatible`: that guards vector
*comparability* (model / dimension / runtime), this guards *whereabouts*.

Plan: docs/planning/RAG_COLLECTION_ARCHITECTURE_IMPLEMENTATION_PLAN.md (W9, W10)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

#: Key under which the identity is stored in ``index_meta``.
META_KEY = "index_identity"


@dataclass(frozen=True)
class IndexIdentity:
    """The store a state DB describes.

    Every field changes *where the vectors live* or *whether they are
    comparable*; any difference invalidates the file hashes for skip purposes.
    """

    storage_backend: str
    collection_strategy: str
    collection_name: str
    model_name: str
    dimension: int

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "IndexIdentity":
        data = json.loads(raw)
        return cls(
            storage_backend=data.get("storage_backend", "embedded"),
            collection_strategy=data.get("collection_strategy", "shared"),
            collection_name=data.get("collection_name", ""),
            model_name=data.get("model_name", ""),
            dimension=int(data.get("dimension", 0)),
        )

    def differences(self, other: "IndexIdentity") -> list[str]:
        """Field names that differ — used to explain the refusal."""
        return [f for f in asdict(self) if getattr(self, f) != getattr(other, f)]


def current_identity(settings, dimension: int) -> IndexIdentity:
    """The identity of the store ``settings`` currently points at."""
    return IndexIdentity(
        storage_backend=(getattr(settings, "storage_backend", "embedded")
                         or "embedded"),
        collection_strategy=(getattr(settings, "collection_strategy", "shared")
                             or "shared"),
        collection_name=settings.collection_name,
        model_name=settings.embedding_model,
        dimension=int(dimension),
    )


def reconcile(state, identity: IndexIdentity) -> tuple[bool, list[str]]:
    """Compare the state DB's recorded identity with ``identity``.

    Returns ``(trustworthy, changed_fields)``:

    * ``(True, [])``  — the state DB describes this store; skipping is safe.
      Also the answer for a brand-new state DB (nothing to be wrong about),
      and the identity is recorded.
    * ``(False, [...])`` — it describes a different store. The caller must
      re-index rather than skip. The identity is **not** overwritten here: it is
      stamped by :func:`stamp` once the re-index has actually run, so an
      interrupted migration is retried rather than assumed complete.
    """
    raw = state.get_meta(META_KEY)
    if raw is None:
        # A state DB written before identities existed. If it already tracks
        # files we cannot know which store they went to, so it is not
        # trustworthy; if it is empty there is nothing to distrust.
        if state.get_all_paths():
            return False, ["unrecorded"]
        stamp(state, identity)
        return True, []

    try:
        stored = IndexIdentity.from_json(raw)
    except Exception:  # noqa: BLE001 — unreadable metadata is not trustworthy
        return False, ["unreadable"]

    changed = stored.differences(identity)
    if changed:
        return False, changed
    return True, []


def stamp(state, identity: IndexIdentity) -> None:
    """Record ``identity`` as the store the state DB now describes."""
    state.set_meta(META_KEY, identity.to_json())


def explain(changed: list[str]) -> str:
    """Human-facing reason for a forced re-index."""
    if changed == ["unrecorded"]:
        return ("the index state predates storage-identity tracking, so it "
                "cannot be matched to the current store")
    if changed == ["unreadable"]:
        return "the recorded storage identity could not be read"
    return "changed: " + ", ".join(sorted(changed))
