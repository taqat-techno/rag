"""Do these collections share a vector space? — the Semantic Map's pre-flight.

The map stacks the mean embedding of every sampled file from EVERY routed
collection into ONE matrix and runs a single PCA over it. That is only
meaningful while all of those vectors are points in the same space. Nothing
checked that, and the three ways it breaks are not hypothetical:

* **Different dimension.** ``np.array`` over rows of unequal length raises an
  uncaught ``ValueError``, which reaches the blanket handler as
  ``500 Internal Server Error`` — so one collection built under a different
  model took down the map for every project that was perfectly fine.
* **Same dimension, different model.** Nothing raises. The projection separates
  *encoders* rather than *meaning*, and the result is a plausible-looking
  picture that is wrong — the worse of the two failures, because it is silent.
* **Named vectors.** ``record.vector`` is a ``dict`` rather than a list, and
  ``np.array(dict, dtype=float32)`` raises ``TypeError``. Same 500.

So the map now asks this module first, and it answers per collection rather
than for the set: an incompatible collection is EXCLUDED WITH A REASON and the
compatible ones are still drawn. Refusing the whole map because one collection
disagrees is the failure this replaces, not a safer version of it.

Two rules carried over from :mod:`ragtools.index_identity`, deliberately:

**Unknown is not different.** A field neither side can observe (no
``get_collection``, an empty collection, a corpus with no recorded model) is
tolerated. Reading absence as disagreement would exclude every framework corpus
— none of them has a ``ProjectIdentity`` row — and the map would lose the
content it was asked to show.

**The reference is unified, not voted on.** Compatibility under unknowns is not
transitive: ``A(normalize=unknown)`` agrees with both ``B(False)`` and
``C(True)`` while ``B`` and ``C`` contradict each other. Admitting "everything
compatible with the seed" would therefore re-admit the mix it exists to prevent.
Instead each admitted collection SHARPENS the reference (an observed value fills
an unknown one), so every admitted collection is compatible with the same fully
resolved space, and the order is deterministic.

Plan: WP-R07.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

logger = logging.getLogger("ragtools.service.map")

#: Vectors read per collection to observe what is ACTUALLY stored, as opposed
#: to what the collection config declares. Small on purpose: this runs before
#: the map decides whether a collection is worth enumerating at all.
PROBE_POINTS = 8

#: Normalization is not recorded anywhere per collection, so it is measured.
#: Every sampled norm inside the tight band -> normalized; every one outside the
#: loose band -> not normalized; anything in between, or a mix, is UNKNOWN.
#: The dead band between the two is what stops a borderline vector from
#: FLIPPING a verdict — an excluded project must be excluded for a fact.
_NORM_TIGHT = 0.02
_NORM_LOOSE = 0.10

#: Exclusion kinds, so a caller can count them without parsing prose.
KIND_FAILED = "failed"
KIND_INCOMPATIBLE = "incompatible"

#: Fields compared, and the value each uses to mean "not known". Listed
#: together so a new field cannot be added to one and forgotten in the other.
_COMPARED: tuple[tuple[str, str, object], ...] = (
    # (field, label, unknown-sentinel)
    ("dimension", "dimension", None),
    ("model_name", "model", ""),
    ("normalized", "normalization", None),
    ("distance", "distance metric", ""),
    ("datatype", "vector type", ""),
    ("vector_name", "vector name", None),
    ("multivector", "multivector", None),
)


@dataclass(frozen=True)
class VectorSpace:
    """What one collection's vectors ARE, as far as can be established.

    Every field is three-valued: a value, or the sentinel meaning "not known".
    ``vector_name`` is the one to read carefully — ``""`` is a real value (the
    unnamed/default vector) and ``None`` is the unknown; conflating them is
    exactly how a named-vector collection got fed to ``np.array`` as if it were
    a list of floats.
    """

    collection: str
    dimension: int | None = None
    model_name: str = ""
    normalized: bool | None = None
    distance: str = ""
    datatype: str = ""
    vector_name: str | None = None
    multivector: bool | None = None
    #: Non-empty => the collection could not be READ at all (kind: failed).
    error: str = ""
    #: Non-empty => readable, but the map cannot project it (kind: incompatible).
    unusable: str = ""

    @property
    def usable(self) -> bool:
        return not self.error and not self.unusable

    def conflict(self, other: "VectorSpace") -> str:
        """How ``other`` disagrees with this space, or ``""`` when it does not.

        Phrased from the candidate's side (``other``) with this space named as
        the map's, because the string is shown against the EXCLUDED collection.
        """
        for field, label, unknown in _COMPARED:
            mine = getattr(self, field)
            theirs = getattr(other, field)
            if mine == unknown or theirs == unknown or mine == theirs:
                continue
            return f"{label} {_show(theirs)} (map uses {_show(mine)})"
        return ""

    def unify(self, other: "VectorSpace") -> "VectorSpace | None":
        """This space sharpened by ``other``, or ``None`` if they disagree.

        Sharpening is the whole point: after admitting a collection that knows
        its normalization, the reference knows it too, so the NEXT collection is
        compared against a resolved space rather than an open question.
        """
        if self.conflict(other):
            return None
        filled = {}
        for field, _label, unknown in _COMPARED:
            mine = getattr(self, field)
            filled[field] = getattr(other, field) if mine == unknown else mine
        return replace(self, **filled)

    def describe(self) -> dict:
        """Compact, JSON-safe form for a health/diagnostics payload."""
        return {
            "collection": self.collection,
            "dimension": self.dimension,
            "model": self.model_name,
            "normalized": self.normalized,
            "distance": self.distance,
            "datatype": self.datatype,
            "vector_name": self.vector_name,
            "multivector": self.multivector,
        }


def _show(value) -> str:
    if value is None:
        return "unknown"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if value == "":
        return "unnamed"
    return str(value)


# --- probing ---------------------------------------------------------------


def extract_vector(raw, vector_name: str | None):
    """The dense float list ``raw`` holds for ``vector_name``, or ``None``.

    ``""`` selects the unnamed/default vector; a non-empty name selects that
    entry of a named-vector record. Never raises — an unexpected shape is
    ``None`` (a vector we could not take), never a fabricated one.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        if not vector_name:
            return None
        return _dense(raw.get(vector_name))
    if vector_name:
        return None
    return _dense(raw)


def _dense(value):
    """A flat list of floats, or ``None`` for multivector / sparse / junk."""
    if value is None:
        return None
    try:
        seq = list(value)
    except TypeError:
        return None
    if not seq:
        return None
    # A multivector row, a sparse entry and a string all answer __len__; a
    # float does not. One test separates every shape we cannot project.
    if any(hasattr(v, "__len__") for v in seq):
        return None
    try:
        return [float(v) for v in seq]
    except (TypeError, ValueError):
        return None


def probe_space(client, collection: str, recorded: dict | None = None) -> VectorSpace:
    """Establish what ``collection``'s vectors are. Never raises.

    Three sources, in increasing authority: what the collection config
    DECLARES, what a recorded index identity SAYS was used to build it, and
    what a handful of stored vectors actually ARE. The observed dimension wins
    over the declared one — the map projects the vectors it fetches, not the
    ones the config promises.
    """
    space = _declared(client, collection, VectorSpace(collection=collection))

    rec = (recorded or {}).get(collection)
    if rec is not None:
        space = replace(
            space,
            model_name=space.model_name or rec.model_name,
            dimension=space.dimension if space.dimension is not None else rec.dimension,
        )

    try:
        return _observed(client, collection, space)
    except Exception as exc:  # noqa: BLE001 — "could not read" is an ANSWER here
        logger.warning("Map: collection %s unreadable: %s", collection, exc)
        return replace(space, error=f"unreadable: {type(exc).__name__}")


def _declared(client, collection: str, space: VectorSpace) -> VectorSpace:
    """Fill from ``get_collection``. Best effort — an engine that cannot answer
    leaves every field unknown rather than producing a false verdict."""
    try:
        params = client.get_collection(collection).config.params
    except Exception:  # noqa: BLE001 — absent introspection is unknown, not wrong
        return space

    vectors = getattr(params, "vectors", None)
    name: str | None = None
    if isinstance(vectors, dict):
        if len(vectors) != 1:
            names = ", ".join(sorted(str(k) for k in vectors)) or "none"
            return replace(space, unusable=f"named vectors ({names}): "
                                          f"no single vector to project")
        name, vectors = next(iter(vectors.items()))
        name = str(name)
    elif vectors is not None:
        name = ""

    if vectors is None:
        if getattr(params, "sparse_vectors", None):
            return replace(space, unusable="sparse vectors cannot be projected")
        return space

    return replace(
        space,
        dimension=_as_int(getattr(vectors, "size", None)),
        distance=_as_str(getattr(vectors, "distance", None)),
        datatype=_as_str(getattr(vectors, "datatype", None)) or "float32",
        vector_name=name,
        multivector=getattr(vectors, "multivector_config", None) is not None,
    )


def _observed(client, collection: str, space: VectorSpace) -> VectorSpace:
    """Fill from the vectors actually stored. Raises only if the read fails."""
    records, _ = client.scroll(
        collection_name=collection,
        limit=PROBE_POINTS,
        with_payload=False,
        with_vectors=True,
    )

    shapes: set[str] = set()
    names: set[str] = set()
    dims: set[int] = set()
    norms: list[float] = []

    for record in records or []:
        raw = getattr(record, "vector", None)
        if raw is None:
            continue
        if isinstance(raw, dict):
            shapes.add("named")
            names.update(str(k) for k in raw)
            if len(raw) != 1:
                continue
            value = next(iter(raw.values()))
        else:
            shapes.add("unnamed")
            names.add("")
            value = raw
        dense = _dense(value)
        if dense is None:
            shapes.add("unprojectable")
            continue
        dims.add(len(dense))
        norms.append(math.sqrt(sum(v * v for v in dense)))

    carried = shapes - {"unprojectable"}
    if len(carried) > 1:
        return replace(space, unusable="both named and unnamed vectors are stored")
    if len(names - {""}) > 1:
        listed = ", ".join(sorted(n for n in names if n))
        return replace(space, unusable=f"named vectors ({listed}): "
                                       f"no single vector to project")
    if shapes == {"unprojectable"}:
        return replace(space, unusable="vectors are not dense (multivector or sparse)")
    if len(dims) > 1:
        listed = ", ".join(str(d) for d in sorted(dims))
        return replace(space, unusable=f"mixed vector dimensions ({listed})")

    if names:
        space = replace(space, vector_name=next(iter(names)))
    if dims:
        # Observed beats declared: this is the length the map will actually
        # stack, and a declared size that disagrees is the config's problem.
        space = replace(space, dimension=next(iter(dims)))
    if norms:
        space = replace(space, normalized=_normalized(norms))
    return space


def _normalized(norms: list[float]) -> bool | None:
    if all(abs(n - 1.0) <= _NORM_TIGHT for n in norms):
        return True
    if all(abs(n - 1.0) > _NORM_LOOSE for n in norms):
        return False
    return None


def _as_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _as_str(value) -> str:
    value = getattr(value, "value", value)
    return value if isinstance(value, str) else ""


# --- partitioning ----------------------------------------------------------


def partition(spaces: dict[str, VectorSpace]):
    """Split probed collections into ``(reference, admitted, rejected)``.

    ``rejected`` entries are ``{"collection", "kind", "reason"}`` — the shape
    the map's existing ``excluded[]`` channel already carries, so an
    incompatible collection is reported the same way an unreadable one is
    rather than needing a second channel nobody renders.

    **``spaces`` is ordered, and the order is the caller's priority.**
    :meth:`~ragtools.collection_router.CollectionRouter.read_collections`
    returns a project's own collection before the framework corpora it links,
    so a project-filtered request anchors on the project that was ASKED FOR.
    Alphabetical tie-breaking got this exactly backwards: ``fw_odoo_9`` sorts
    before ``proj_beta``, so a scoped request for ``beta`` made the vendored
    core the reference and excluded ``beta`` from its own map.

    The seed is the collection compatible with the most others, so a single odd
    collection cannot dictate the space and evict the majority; priority order
    only decides ties. Every later admission unifies into the reference — see
    the module docstring for why "compatible with the seed" is not enough.
    """
    rejected: list[dict] = []
    usable: dict[str, VectorSpace] = {}
    for name, space in spaces.items():
        if space.error:
            rejected.append({"collection": name, "kind": KIND_FAILED,
                             "reason": space.error})
        elif space.unusable:
            rejected.append({"collection": name, "kind": KIND_INCOMPATIBLE,
                             "reason": space.unusable})
        else:
            usable[name] = space

    if not usable:
        return None, [], rejected

    order = list(usable)

    def agreement(name: str) -> int:
        space = usable[name]
        return sum(1 for other in order
                   if other != name and not space.conflict(usable[other]))

    # max() keeps the FIRST maximal element, so a tie resolves to the
    # highest-priority collection. Deterministic given a deterministic input
    # order — the map must not reshuffle between recomputes.
    seed = max(order, key=agreement)

    reference = usable[seed]
    admitted = [seed]
    for name in order:
        if name == seed:
            continue
        merged = reference.unify(usable[name])
        if merged is None:
            rejected.append({
                "collection": name,
                "kind": KIND_INCOMPATIBLE,
                "reason": f"incompatible vector space: "
                          f"{reference.conflict(usable[name])}",
            })
            continue
        reference = merged
        admitted.append(name)

    # Back into the caller's order: downstream spends a per-collection budget
    # walking this list, and priority has to survive the partition.
    kept = set(admitted)
    return reference, [n for n in order if n in kept], rejected


# --- recorded model identity ----------------------------------------------


@dataclass(frozen=True)
class RecordedModel:
    """What the state DB says a collection was BUILT with.

    Qdrant records a dimension but never a model name, so two collections
    embedded by different models at the same dimension are indistinguishable
    from the store alone. This is the only place that difference is written
    down — see :mod:`ragtools.index_identity`.
    """

    model_name: str
    dimension: int | None


#: ``index_meta`` rows that describe the vector space. Named here so the map's
#: cache identity and this module's model lookup cannot drift apart about which
#: rows count as a compatibility input.
def identity_meta_rows(db_path: str) -> dict[str, str]:
    """The identity rows of ``index_meta``, or ``{}``.

    Reads with plain sqlite3 and ONLY when the file already exists: opening
    through :class:`~ragtools.indexing.state.IndexState` would create the
    database (and run a schema migration) as a side effect of drawing a map.
    A read path must not bring a database into being.
    """
    if not db_path or not Path(db_path).exists():
        return {}
    from ragtools.index_identity import META_KEY, PROJECT_META_PREFIX

    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute("SELECT key, value FROM index_meta").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

    return {
        key: value for key, value in rows
        if key == META_KEY or key.startswith(PROJECT_META_PREFIX)
    }


def recorded_models(db_path: str | None) -> dict[str, RecordedModel]:
    """``collection_name -> RecordedModel`` from the state DB. Never raises.

    Per-project rows win over the store-wide identity: under ``per_project``
    they are the ones that differ, and the whole reason a mixed-model install
    exists is that some projects were re-indexed under the new model and some
    were not.
    """
    rows = identity_meta_rows(db_path or "")
    if not rows:
        return {}

    from ragtools.index_identity import (
        META_KEY, PROJECT_META_PREFIX, IndexIdentity, ProjectIdentity,
    )

    out: dict[str, RecordedModel] = {}
    raw = rows.get(META_KEY)
    if raw:
        try:
            identity = IndexIdentity.from_json(raw)
        except Exception:  # noqa: BLE001 — an unreadable stamp is unknown
            identity = None
        if identity is not None and identity.collection_name:
            out[identity.collection_name] = RecordedModel(
                identity.model_name, identity.dimension or None)

    for key, raw in rows.items():
        if not key.startswith(PROJECT_META_PREFIX):
            continue
        try:
            project = ProjectIdentity.from_json(raw)
        except Exception:  # noqa: BLE001 — see above
            continue
        if project.collection_name:
            out[project.collection_name] = RecordedModel(
                project.model_name, project.dimension or None)
    return out
