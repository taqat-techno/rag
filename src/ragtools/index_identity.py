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
historical counts. R06 closes that door.

**TWO identities, because there are two questions.** The first R06 answered
both with one digest of the whole ``project_id -> collection_name`` mapping, and
fusing them made a plain project addition cost a full corpus re-index — adding
the sixteenth project changed the global digest, so the fifteen already indexed
were declared untrustworthy and re-embedded. The questions are separate:

* *"Is this the same registry?"* — global integrity. Still
  :func:`ragtools.registry.registry_fingerprint`, still a field on
  :class:`IndexIdentity`, and still the thing that notices a lost, replaced or
  rolled-back ``registry.db``. It is an **integrity signal**, not a
  per-project verdict — see :mod:`ragtools.registry_integrity`.
* *"Is THIS project's mapping still the one its file hashes were written
  against?"* — :class:`ProjectIdentity`, one ``index_meta`` row per project.
  Only the project whose own mapping moved is invalidated.

A project appearing is therefore not a reason to distrust the projects that
were already there: they have their own rows, and their own rows did not
change. A project whose UUID, collection, generation or embedding identity
moved is invalidated **alone**, and invalidation means RE-INDEX — nothing here
deletes anything.

Absent is unknown, never different. A v3.5.0 stamp has no fingerprint and no
per-project rows; reading that as "different" would re-embed every upgraded
install's corpus on first start.

Plan: docs/planning/RAG_COLLECTION_ARCHITECTURE_IMPLEMENTATION_PLAN.md (W9, W10)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

#: Key under which the identity is stored in ``index_meta``.
META_KEY = "index_identity"

#: Key prefix for the per-project collection identity, one row per project.
#: Keyed by ``project_id`` and not by UUID on purpose: the UUID is one of the
#: things being compared, so a store keyed by it could never notice that a
#: project was removed and re-added under a fresh identity.
PROJECT_META_PREFIX = "project_identity:"


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
    #: Digest of the registry's ``project_id -> collection_name`` mapping —
    #: the GLOBAL INTEGRITY signal: "is this the same registry?". Empty means
    #: "not known", NOT "no projects": a v3.5.0 stamp predates the field and a
    #: shared-layout install has no registry at all.
    #:
    #: Deliberately NOT part of :meth:`differences`. It is registry-wide, so
    #: comparing it to decide whether a *file hash* may be trusted invalidates
    #: every project whenever any one of them changes — which is how adding one
    #: project came to cost a full corpus re-index. Per-project trust is
    #: :class:`ProjectIdentity`; this field feeds
    #: :mod:`ragtools.registry_integrity`.
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
        """Structural field names that differ — used to explain the refusal.

        STRUCTURAL means: true of the whole store, so a difference invalidates
        every project at once. Engine, layout, legacy collection name, model,
        dimension — change any of them and no project's chunks are where the
        state DB says they are.

        ``registry_fingerprint`` is excluded. It is registry-WIDE, so folding it
        in here means one project's mapping change invalidates the file hashes
        of every other project; that is what made adding a project cost a full
        re-index. It is compared, as an integrity signal, by
        :func:`registry_changed` and :mod:`ragtools.registry_integrity`.
        """
        return [
            f for f in asdict(self)
            if f != "registry_fingerprint" and getattr(self, f) != getattr(other, f)
        ]

    def registry_changed(self, other: "IndexIdentity") -> bool:
        """Do the two sides describe DIFFERENT registries? (integrity only.)

        Answered only when both sides have a fingerprint. An unknown
        fingerprint is unknown, not different: treating "" as a value would make
        every v3.5.0 stamp mismatch on the first v3.5.1 start, and would make
        ``rag doctor`` (which has no registry to hand) contradict the service.

        A True here says the registry is not the one this index was built
        against. It does NOT say which project moved — that is exactly what the
        per-project rows are for, and what makes the answer actionable instead
        of global.
        """
        return bool(self.registry_fingerprint and other.registry_fingerprint
                    and self.registry_fingerprint != other.registry_fingerprint)


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

    # The registry changed, and there are no per-project rows to say WHICH
    # project moved. That is a stamp written before the per-project identity
    # existed; with nothing to localise the change against, the only honest
    # answer is the whole-store one. Once per-project rows exist (every stamp
    # this version writes), the same divergence is resolved project by project
    # — and adding a project stops costing a corpus re-index.
    if stored.registry_changed(identity) and not stored_project_identities(state):
        return False, ["registry_fingerprint"]
    return True, []


def stamp(state, identity: IndexIdentity) -> None:
    """Record ``identity`` as the store the state DB now describes."""
    state.set_meta(META_KEY, identity.to_json())


# --- per-project collection identity --------------------------------------


@dataclass(frozen=True)
class ProjectIdentity:
    """Where ONE project's chunks go, and whether they are comparable.

    Four things, each of which moves a project's vectors somewhere its file
    hashes no longer describe:

    * ``project_uuid`` — the immutable identity. A change here is not a move,
      it is a different project wearing the same id: a lost registry re-mints
      UUIDs, and a removed-then-re-added project gets a fresh one. Either way
      the old state rows describe a collection this project does not own.
    * ``collection_name`` — where the writes land. The rebuild swap primitive
      changes exactly this.
    * ``generation`` — which rebuild the collection belongs to. It is what
      tells a forward swap (``g2`` after ``g1``) from a restored older registry
      (``g1`` after ``g2``), which is a rollback and an integrity failure.
    * ``model_name`` / ``dimension`` — embedding identity. Also carried by the
      store-wide :class:`IndexIdentity`; repeated per project so a project
      re-adopted from a differently-embedded corpus is caught on its own.

    Compared per project and NOWHERE aggregated. That is the entire point of
    the R06 refinement: project 3 moving must cost project 3.
    """

    project_uuid: str
    collection_name: str
    generation: int
    model_name: str
    dimension: int

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "ProjectIdentity":
        data = json.loads(raw)
        return cls(
            project_uuid=str(data.get("project_uuid", "")),
            collection_name=str(data.get("collection_name", "")),
            generation=int(data.get("generation", 0) or 0),
            model_name=str(data.get("model_name", "")),
            dimension=int(data.get("dimension", 0) or 0),
        )

    def differences(self, other: "ProjectIdentity") -> list[str]:
        """Field names that differ. Non-empty means: re-index THIS project."""
        return [f for f in asdict(self) if getattr(self, f) != getattr(other, f)]


def project_meta_key(project_id: str) -> str:
    return f"{PROJECT_META_PREFIX}{project_id}"


def current_project_identities(settings, dimension: int, *,
                               registry=None) -> dict[str, ProjectIdentity]:
    """The live ``project_id -> ProjectIdentity`` map, or ``{}`` with no registry.

    ``{}`` under ``shared``: there is no per-project mapping to record, and an
    empty map must never be read as "every project vanished" (see
    :func:`untrusted_projects`, which compares only projects the registry
    actually reports on).
    """
    if registry is None:
        return {}
    identities: dict[str, ProjectIdentity] = {}
    for rec in registry.list(include_archived=True):
        identities[rec.project_id] = ProjectIdentity(
            project_uuid=str(rec.uuid),
            collection_name=str(rec.collection_name),
            generation=int(getattr(rec, "generation", 0) or 0),
            model_name=settings.embedding_model,
            dimension=int(dimension),
        )
    return identities


def stamp_projects(state, identities: dict[str, ProjectIdentity]) -> None:
    """Record each project's mapping. Written with — never instead of — :func:`stamp`.

    Rows for projects NOT in ``identities`` are left alone. A project missing
    from the live registry is the interesting case (removed? lost registry?),
    and deleting the evidence would answer that question by destroying it. The
    deliberate removal path forgets its own row via :func:`forget_project`.
    """
    for project_id, identity in identities.items():
        state.set_meta(project_meta_key(project_id), identity.to_json())


def forget_project(state, project_id: str) -> None:
    """Drop one project's recorded mapping — for DELIBERATE removal only.

    Called where the project's file rows are deleted too, so the two stay
    consistent: a project with no state rows has nothing to invalidate, and
    leaving its identity behind would make an ordinary removal look like a
    registry that lost a project.
    """
    state.delete_meta(project_meta_key(project_id))


def stored_project_identities(state) -> dict[str, ProjectIdentity]:
    """Every recorded per-project mapping. Unreadable rows are dropped.

    A row we cannot parse cannot be compared; dropping it makes the project
    read as "unknown", which is the same conservative-but-quiet answer a
    v3.5.0 stamp gets. It never fabricates a mismatch.
    """
    out: dict[str, ProjectIdentity] = {}
    for key, raw in state.get_meta_prefix(PROJECT_META_PREFIX).items():
        project_id = key[len(PROJECT_META_PREFIX):]
        if not project_id:
            continue
        try:
            out[project_id] = ProjectIdentity.from_json(raw)
        except Exception:  # noqa: BLE001 — see docstring
            continue
    return out


def untrusted_projects(state, identities: dict[str, ProjectIdentity]) -> dict[str, list[str]]:
    """``project_id -> changed fields`` for the projects that must be re-indexed.

    A project appears here only when a mapping WAS recorded for it and the live
    one differs. Two absences, both deliberate:

    * no recorded row (a new project, or a v3.5.0 / v3.5.0-era stamp) — unknown
      is not different, so nothing is invalidated;
    * recorded but not live (removed, or a registry that lost it) — there is
      nothing to re-index it INTO. That divergence is an integrity question,
      answered by :mod:`ragtools.registry_integrity`, not a skip decision.

    Consequence, and the reason this function exists: adding project N+1 leaves
    the other N untouched. They each have a row, and their rows did not change.
    """
    stored = stored_project_identities(state)
    changed: dict[str, list[str]] = {}
    for project_id, live in identities.items():
        was = stored.get(project_id)
        if was is None:
            continue
        fields = was.differences(live)
        if fields:
            changed[project_id] = fields
    return changed


def explain_project(project_id: str, changed: list[str]) -> str:
    """Human-facing reason one project — and only it — is being re-indexed."""
    detail = ", ".join(sorted(changed))
    if "project_uuid" in changed:
        return (f"project '{project_id}': {detail} — its registry identity was "
                f"re-minted, so its recorded chunks belong to a collection it no "
                f"longer owns")
    return (f"project '{project_id}': {detail} — its chunks no longer live where "
            f"this index state was written against")


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
        # full re-index) is otherwise indistinguishable from a bug. This is the
        # one case that cannot be narrowed to a project: the mapping changed and
        # the state DB has no per-project rows to say which one moved.
        return (detail + " — the project -> collection mapping is no longer the "
                "one this index was built against, and this index state has no "
                "per-project record to narrow it down, so every project is "
                "re-indexed once and then recorded individually")
    return detail
