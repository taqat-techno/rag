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

**Whereabouts is not only the settings.** Under ``per_project`` the collection a
project's chunks live in is named by ``registry.projects.collection_name`` — the
swap primitive — and every field above is invariant to that table. Lose
``registry.db`` and ``build_router`` recreates it empty, ``sync_projects_from_config``
mints a fresh uuid4 per project, and N brand-new EMPTY ``proj_<hex>`` collections
appear beside the N that still hold the data. Storage backend, layout, legacy
collection name, model and dimension are all unchanged, so this module said
"compatible", the incremental run trusted the state DB, and it skipped every
file. Nothing raised; search returned nothing; the dashboard showed the
historical counts. ``registry_fingerprint`` closes that door (R06).

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
    #: Digest of the registry's ``project_id -> collection_name`` mapping, the
    #: thing that actually says where a per-project chunk lands. Empty means
    #: "not known", NOT "no projects": a v3.5.0 stamp predates the field and a
    #: shared-layout install has no registry at all. See ``differences``.
    registry_fingerprint: str = ""

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
            # Absent in every identity stamped before R06. Defaulting to "" is
            # what lets an upgraded install load its own stamp instead of
            # reading it as a mismatch and re-embedding the whole corpus.
            registry_fingerprint=str(data.get("registry_fingerprint", "") or ""),
        )

    def differences(self, other: "IndexIdentity") -> list[str]:
        """Field names that differ — used to explain the refusal.

        The fingerprint is compared only when BOTH sides have one. An unknown
        fingerprint is unknown, not different: treating "" as a value would
        make every v3.5.0 stamp mismatch on the first v3.5.1 start, and would
        make ``rag doctor`` (which has no registry to hand) contradict the
        service. A known-vs-known difference, though, is the R06 signal — the
        mapping the state DB was written against no longer holds.
        """
        changed = [
            f for f in asdict(self)
            if f != "registry_fingerprint" and getattr(self, f) != getattr(other, f)
        ]
        if (self.registry_fingerprint and other.registry_fingerprint
                and self.registry_fingerprint != other.registry_fingerprint):
            changed.append("registry_fingerprint")
        return changed


def current_identity(settings, dimension: int, *, registry=None) -> IndexIdentity:
    """The identity of the store ``settings`` currently points at.

    ``registry`` is optional so every existing caller keeps its exact behaviour
    (fingerprint ""). Supply it wherever one is already open — the service owner
    holds one whenever the layout is ``per_project`` — and the identity then
    covers *where each project's chunks actually go*, not merely which layout
    was configured.
    """
    return IndexIdentity(
        storage_backend=(getattr(settings, "storage_backend", "embedded")
                         or "embedded"),
        collection_strategy=(getattr(settings, "collection_strategy", "shared")
                             or "shared"),
        collection_name=settings.collection_name,
        model_name=settings.embedding_model,
        dimension=int(dimension),
        registry_fingerprint=_fingerprint(registry),
    )


def _fingerprint(registry) -> str:
    """Digest the registry mapping, or "" when there is no registry to read.

    Imported lazily: this module is deliberately dependency-light (it is on the
    ``rag doctor`` path, which must work when the service does not), and the
    registry pulls in SQLite plus the identity rules.
    """
    if registry is None:
        return ""
    from ragtools.registry import registry_fingerprint

    return registry_fingerprint(registry)


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
    detail = "changed: " + ", ".join(sorted(changed))
    if "registry_fingerprint" in changed:
        # Name the cause, because the operator-visible symptom (an unexplained
        # full re-index) is otherwise indistinguishable from a bug.
        return (detail + " — the project -> collection mapping is no longer the "
                "one this index was built against; a lost or recreated "
                "registry.db re-mints project UUIDs, so every collection name "
                "changes while nothing else does")
    return detail
