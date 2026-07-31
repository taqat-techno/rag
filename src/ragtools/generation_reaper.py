"""Reclaiming the collections a rebuild leaves behind — WP-R03.

A per-project rebuild builds into a NEW collection and swaps to it only once the
replacement is verified (:meth:`ragtools.service.owner.QdrantOwner._rebuild_project`):

    proj_<uuid>        the collection the project was created with (generation 0)
    proj_<uuid>_g1     the first rebuild's replacement
    proj_<uuid>_g2     the second's, and so on

The superseded collection is dropped LAST, deliberately, so a failure to drop it
costs disk rather than data. Two things therefore accumulate on a real machine:

* **superseded** generations — the swap happened, the drop did not;
* **abandoned staging** generations — the process died between
  ``ensure_collection`` and the swap, so a ``_g<n+1>`` exists that nothing points
  at and nothing will ever finish.

Both are pure waste, and until now there was no way to reclaim either:
``rag storage reclaim`` computes ``owned ∩ existing - current``, and
``owned_collections`` is built from the router, which only ever reports the
collections the registry *currently points at*. A generation nobody points at is
invisible to it.

**Reaping is the one destructive addition in this release, so it is off.**
The default is a dry run that deletes nothing, reports every candidate AND every
exclusion by name, and records a durable first-sighting so the grace period is
already running if an operator later opts in (``reap_generations``).

Why the guard rail is this heavy
--------------------------------

A collection is NEVER deleted because its name resembles a generation. ``proj_``
plus 32 hex characters is a *shape*, and another installation sharing a managed
engine produces collections that match it perfectly — this project has already
shipped the opposite mistake once (``obsolete_collections`` returned
``existing - current`` and the caller deleted it, which on a shared engine is
another install's entire index). So nine things must ALL hold, and each failure
is reported as a named exclusion rather than silently dropping the candidate:

1. :func:`ragtools.registry_integrity.assert_reaping_allowed` — a lost,
   replaced or rolled-back registry makes EVERY collection look orphaned, which
   is precisely when reaping destroys the index it was meant to protect;
2. the project identity is unambiguous — one registry row, and that row's own
   active collection shares this candidate's base;
3. generation metadata is present — the name carries an explicit ``_g<n>``;
4. the project UUID in the name resolves to a row in OUR registry;
5. the embedding identity matches, where this installation can state its own;
6. it is not any project's active registry pointer;
7. it is not referenced by a running, interrupted, failed, resumable or
   otherwise unresolved rebuild — including a live migration plan;
8. a grace period has elapsed since the collection was FIRST SEEN orphaned;
9. a durable audit record was written, and read back, before anything is dropped.

Ambiguity is not permission. Anything that cannot be established — a point count
that could not be taken, a vector configuration that could not be read — excludes
the candidate and produces a degraded diagnostic, never a deletion.

A bare ``proj_<32 hex>`` is deliberately never a candidate. Nothing in that name
says which generation it is, and it is exactly what another installation's live
project looks like; it is reported with ``no_generation_metadata`` so an operator
can see it, and left alone. Only names that could only have been produced by
:meth:`~ragtools.service.owner.QdrantOwner._staging_collection` are in scope.

State lives in SQLite beside the index state, for the reason the project already
settled (``CLAUDE.md``, "Do NOT use JSON files for state"): the first-sighting
ledger is what makes the grace period durable across restarts, and the audit is
the record an operator reads after the fact.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ragtools.generation_reaper")

#: The ONLY shape this module will consider deleting: a project collection name
#: followed by an explicit generation marker. ``proj_`` + 32 hex is produced by
#: :func:`ragtools.identity.project_collection_name`; the ``_g<n>`` suffix by
#: :meth:`ragtools.service.owner.QdrantOwner._staging_collection`. Hex has no
#: ``g``, so the suffix is unambiguous and never part of the identity.
_GENERATION_RE = re.compile(r"^(proj_([0-9a-f]{32}))_g(\d+)$")

#: A project collection with no generation marker. Considered — so it can be
#: REPORTED — and never a candidate.
_BARE_RE = re.compile(r"^proj_([0-9a-f]{32})$")

#: How long a collection must have been *observed orphaned* before it may be
#: reaped. Measured from the first sighting this installation recorded, not from
#: any property of the collection: Qdrant does not tell us when a collection was
#: created, and inventing an age is how an in-flight staging collection gets
#: deleted out from under the rebuild that is filling it.
DEFAULT_GRACE_SECONDS = 24 * 60 * 60.0

# --- named exclusion reasons ----------------------------------------------
#
# Every one of these is a sentence an operator can act on. They are constants
# because they are asserted on: a reason that is only ever a literal in a log
# line is a reason nobody can test.

REGISTRY_INTEGRITY_UNRESOLVED = "registry_integrity_unresolved"
NO_GENERATION_METADATA = "no_generation_metadata"
NOT_OWNED = "not_owned_by_this_installation"
AMBIGUOUS_IDENTITY = "ambiguous_project_identity"
ACTIVE_POINTER = "active_registry_pointer"
EMBEDDING_MISMATCH = "embedding_identity_mismatch"
EMBEDDING_UNKNOWN = "embedding_identity_unknown"
REFERENCED_BY_REBUILD = "referenced_by_interrupted_rebuild"
REBUILD_UNRESOLVED = "rebuild_unresolved"
MIGRATION_UNRESOLVED = "migration_unresolved"
WITHIN_GRACE = "within_grace_period"
POINTS_UNKNOWN = "points_unknown"
OPERATION_REFUSED = "operation_refused"
AUDIT_WRITE_FAILED = "audit_write_failed"
#: The ledger could not be written, so neither the grace clock nor the audit
#: trail can be established. Distinct from `within_grace_period`, which is a
#: statement about time; this one is a statement about evidence.
LEDGER_UNAVAILABLE = "ledger_unavailable"

#: Audit actions, written to the durable ledger.
AUDIT_INTENT = "reap_intent"
AUDIT_DELETED = "reaped"
AUDIT_FAILED = "reap_failed"


@dataclass(frozen=True)
class Candidate:
    """One collection the sweep considered, and what it concluded about it."""

    collection: str
    project_id: str = ""
    project_uuid: str = ""
    #: The generation encoded in the name, or ``None`` when the name carries no
    #: marker. Never guessed.
    generation: Optional[int] = None
    #: The project's CURRENT generation, per the registry, or ``None``.
    active_generation: Optional[int] = None
    #: Points the collection holds. ``None`` means "could not be counted" — it
    #: is never rendered as 0, for the reason `_count_points` exists.
    points: Optional[int] = None
    first_seen: Optional[float] = None
    #: Named reasons this collection may not be reaped. Empty iff reapable.
    exclusions: tuple[str, ...] = ()
    #: Free text expanding on the exclusions, for the operator.
    detail: str = ""

    @property
    def reapable(self) -> bool:
        return not self.exclusions

    def describe(self) -> str:
        who = f"{self.project_id or '?'}"
        gen = "?" if self.generation is None else self.generation
        pts = "unknown" if self.points is None else f"{self.points:,}"
        if self.reapable:
            return f"{self.collection} (project {who}, generation {gen}, {pts} points)"
        return (f"{self.collection} (project {who}, generation {gen}) — "
                f"{', '.join(self.exclusions)}")


@dataclass
class ReapReport:
    """What one sweep saw, excluded, and — only when asked — deleted."""

    dry_run: bool = True
    #: False when the global guard refused; nothing is even considered then.
    allowed: bool = True
    #: Why the whole sweep was refused, or "".
    refusal: str = ""
    #: Collections that passed every check.
    candidates: list[Candidate] = field(default_factory=list)
    #: Collections that were considered and excluded, each with its reasons.
    excluded: list[Candidate] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    #: ``(collection, error)`` for deletions that were attempted and failed.
    failures: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def considered(self) -> list[Candidate]:
        return list(self.candidates) + list(self.excluded)

    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for cand in self.excluded:
            for reason in cand.exclusions:
                counts[reason] = counts.get(reason, 0) + 1
        return counts

    def describe(self) -> str:
        if not self.allowed:
            return f"generation reap refused: {self.refusal}"
        head = ("dry run: " if self.dry_run else "")
        return (f"{head}{len(self.candidates)} reapable, "
                f"{len(self.excluded)} excluded, {len(self.deleted)} deleted")

    def to_dict(self) -> dict:
        """The diagnostic surface's shape. Unknown stays ``None``, never 0."""
        return {
            "dry_run": self.dry_run,
            "allowed": self.allowed,
            "refusal": self.refusal,
            "summary": self.describe(),
            "reapable": [
                {"collection": c.collection, "project": c.project_id,
                 "project_uuid": c.project_uuid, "generation": c.generation,
                 "active_generation": c.active_generation, "points": c.points,
                 "first_seen": c.first_seen}
                for c in self.candidates
            ],
            "excluded": [
                {"collection": c.collection, "project": c.project_id,
                 "project_uuid": c.project_uuid, "generation": c.generation,
                 "active_generation": c.active_generation, "points": c.points,
                 "first_seen": c.first_seen,
                 "reasons": list(c.exclusions), "detail": c.detail}
                for c in self.excluded
            ],
            "exclusion_counts": self.reason_counts(),
            "deleted": list(self.deleted),
            "failures": [{"collection": n, "error": e} for n, e in self.failures],
            "notes": list(self.notes),
        }


# --- the durable ledger ----------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reap_sighting (
    collection_name TEXT PRIMARY KEY,
    project_id      TEXT,
    project_uuid    TEXT,
    generation      INTEGER,
    first_seen      REAL NOT NULL,
    last_seen       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reap_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              REAL NOT NULL,
    collection_name TEXT NOT NULL,
    project_id      TEXT,
    project_uuid    TEXT,
    generation      INTEGER,
    points          INTEGER,
    action          TEXT NOT NULL,
    detail          TEXT
);
"""


def ledger_path(settings) -> Path:
    """Beside the index state, so one data directory holds one machine's state."""
    return Path(settings.state_db).with_name("reaper.db")


def _connect(settings) -> sqlite3.Connection:
    path = ledger_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.executescript(_SCHEMA)
    return conn


def record_sighting(settings, cand: Candidate, *, now: float) -> float:
    """First-sighting timestamp for ``cand``, inserting it if this is the first.

    Returns the FIRST time this installation saw the collection orphaned, which
    is what the grace period is measured from. Idempotent: a second sweep
    refreshes ``last_seen`` and leaves ``first_seen`` alone, so the clock cannot
    be reset by polling.
    """
    conn = _connect(settings)
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO reap_sighting (collection_name, project_id,"
                " project_uuid, generation, first_seen, last_seen)"
                " VALUES (?,?,?,?,?,?)",
                (cand.collection, cand.project_id, cand.project_uuid,
                 cand.generation, now, now))
            conn.execute(
                "UPDATE reap_sighting SET last_seen=?, project_id=?,"
                " project_uuid=?, generation=? WHERE collection_name=?",
                (now, cand.project_id, cand.project_uuid, cand.generation,
                 cand.collection))
        row = conn.execute(
            "SELECT first_seen FROM reap_sighting WHERE collection_name=?",
            (cand.collection,)).fetchone()
        return float(row[0]) if row else now
    finally:
        conn.close()


def forget_sighting(settings, collection: str) -> None:
    """Drop a sighting. Called once the collection is gone.

    Without this, a NEW collection that happened to reuse the name would inherit
    the old clock and be reapable the instant it appeared.
    """
    try:
        conn = _connect(settings)
    except sqlite3.Error:
        return
    try:
        with conn:
            conn.execute("DELETE FROM reap_sighting WHERE collection_name=?",
                         (collection,))
    finally:
        conn.close()


def prune_sightings(settings, present: set) -> int:
    """Forget sightings for collections that are no longer on the engine."""
    conn = _connect(settings)
    try:
        rows = conn.execute(
            "SELECT collection_name FROM reap_sighting").fetchall()
        gone = [r[0] for r in rows if r[0] not in present]
        if gone:
            with conn:
                conn.executemany(
                    "DELETE FROM reap_sighting WHERE collection_name=?",
                    [(n,) for n in gone])
        return len(gone)
    finally:
        conn.close()


def record_audit(settings, cand: Candidate, action: str, *,
                 detail: str = "", now: Optional[float] = None) -> int:
    """Write one audit row and PROVE it landed. Returns the row id.

    Read back on the same connection after the commit, for the same reason
    :meth:`ragtools.registry.ProjectRegistry.set_active_collection` reads its
    row back: a write that "probably happened" is not a record. Raises if it
    cannot be proven, and the caller must then refuse to delete — an
    unrecorded deletion is indistinguishable from data that vanished.
    """
    stamp = time.time() if now is None else now
    conn = _connect(settings)
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO reap_audit (at, collection_name, project_id,"
                " project_uuid, generation, points, action, detail)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (stamp, cand.collection, cand.project_id, cand.project_uuid,
                 cand.generation, cand.points, action, detail or None))
            audit_id = int(cursor.lastrowid or 0)
        row = conn.execute(
            "SELECT action FROM reap_audit WHERE id=?", (audit_id,)).fetchone()
        if row is None or str(row[0]) != action:
            raise RuntimeError(
                f"the audit record for {cand.collection!r} committed but could "
                f"not be read back; refusing to treat it as durable")
        return audit_id
    finally:
        conn.close()


def audit_log(settings, *, limit: int = 100) -> list[dict]:
    """The audit trail, newest first. Read-only; for the diagnostic surface.

    Never raises. An unreadable ledger is the moment the diagnostic is most
    wanted, and a 500 then helps nobody — the sweep beside it already reports
    ``ledger_unavailable`` as the reason nothing may be deleted.
    """
    if not ledger_path(settings).is_file():
        return []
    try:
        conn = _connect(settings)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT at, collection_name, project_id, project_uuid, generation,"
            " points, action, detail FROM reap_audit ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [
        {"at": r[0], "collection": r[1], "project": r[2], "project_uuid": r[3],
         "generation": r[4], "points": r[5], "action": r[6], "detail": r[7]}
        for r in rows
    ]


# --- what a rebuild is still holding on to ---------------------------------


@dataclass(frozen=True)
class RebuildHold:
    """Rebuild work that is not finished, and therefore still owns collections."""

    #: Project ids an unresolved rebuild named. Their generations are off limits.
    projects: frozenset = frozenset()
    #: Collection names an unresolved rebuild named.
    collections: frozenset = frozenset()
    #: True when something is unresolved but we cannot say WHICH projects — in
    #: which case nothing may be reaped at all.
    unlocalised: bool = False
    reason: str = ""

    def blocks(self, cand: Candidate) -> tuple[str, str]:
        """``(reason, detail)`` if this hold covers ``cand``, else ``("", "")``."""
        if cand.project_id and cand.project_id in self.projects:
            return REFERENCED_BY_REBUILD, self.reason
        if cand.collection in self.collections:
            return REFERENCED_BY_REBUILD, self.reason
        # A generation is created by a rebuild OF a project, so a rebuild that
        # named the project also owns the generation the name does not mention.
        base = cand.collection.rpartition("_g")[0]
        if base and base in self.collections:
            return REFERENCED_BY_REBUILD, self.reason
        if self.unlocalised:
            return REBUILD_UNRESOLVED, self.reason
        return "", ""


def rebuild_hold(settings) -> RebuildHold:
    """Which rebuilds are unresolved right now, re-read rather than remembered.

    Two durable markers, both of which mean "a rebuild started and this machine
    cannot yet say it finished":

    * ``rebuild-intent.json`` (:func:`ragtools.service.destructive.pending_intent`)
      — written before the irreversible step and re-written, not cleared, when a
      run ends with failures. It names the projects and the collections;
    * a relayout plan that is not complete — the migration is a rebuild of every
      unit in it, and its staging collections are its working set.

    An intent whose shape we do not recognise is ``unlocalised``: it says a
    rebuild is unresolved without saying what it touched, and that is a reason to
    reap nothing rather than a reason to guess.
    """
    from ragtools.service import destructive

    projects: set[str] = set()
    collections: set[str] = set()
    unlocalised = False
    reasons: list[str] = []

    intent = None
    try:
        intent = destructive.pending_intent(settings)
    except Exception:  # noqa: BLE001 — an unreadable marker is still a marker
        unlocalised = True
        reasons.append("a rebuild intent marker exists but could not be read")

    if intent is not None:
        named = False
        for pid in intent.get("projects") or []:
            projects.add(str(pid))
            named = True
        for name in intent.get("collections") or []:
            collections.add(str(name))
            named = True
        for pid in intent.get("failed_projects") or []:
            projects.add(str(pid))
            named = True
        reasons.append(
            f"a {intent.get('operation', 'rebuild')} started and has not been "
            f"resolved")
        if not named:
            unlocalised = True

    try:
        from ragtools.upgrade import relayout

        plan = relayout.active_plan(settings)
        if plan is not None:
            report = relayout.progress(settings, plan)
            if report is not None and not report.complete:
                unlocalised = True
                reasons.append(
                    f"layout migration plan {plan} is unresolved "
                    f"({report.describe()})")
    except Exception:  # noqa: BLE001 — no plan store means no migration
        pass

    return RebuildHold(frozenset(projects), frozenset(collections),
                       unlocalised, "; ".join(reasons))


# --- the sweep -------------------------------------------------------------


def _collection_dimension(client, name: str) -> Optional[int]:
    """The vector size a collection declares, or ``None`` when unreadable.

    Tolerant on purpose: Qdrant reports either a single ``VectorParams`` or a
    mapping of named vectors, and the embedded and server backends have not
    always agreed on the wrapper. ``None`` is "could not establish", which
    excludes the candidate — it is never read as "matches".
    """
    try:
        info = client.get_collection(name)
    except Exception:  # noqa: BLE001
        return None

    node = info
    for attr in ("config", "params", "vectors"):
        node = getattr(node, attr, None)
        if node is None:
            break
    if node is None:
        return None

    size = getattr(node, "size", None)
    if size is None and isinstance(node, dict):
        size = node.get("size")
        if size is None:
            for value in node.values():
                size = (getattr(value, "size", None)
                        if not isinstance(value, dict) else value.get("size"))
                if size is not None:
                    break
    try:
        return None if size is None else int(size)
    except (TypeError, ValueError):
        return None


def _own_dimension(owner) -> Optional[int]:
    """The embedding dimension THIS installation writes, or ``None``.

    ``None`` means the installation cannot state its own embedding identity
    (no encoder in this process), which makes check 5 *not applicable* rather
    than failed — the alternative is refusing every candidate on a CLI that
    deliberately never loads a model.
    """
    try:
        dim = getattr(owner.encoder, "dimension", None)
    except Exception:  # noqa: BLE001
        return None
    try:
        return None if dim is None else int(dim)
    except (TypeError, ValueError):
        return None


def _registry_of(owner):
    try:
        return owner.registry
    except Exception:  # noqa: BLE001
        return None


def _classify(registry, name: str) -> Optional[Candidate]:
    """Structural verdict for one collection, or ``None`` if out of scope.

    Out of scope means the name is not a project collection at all — a framework
    corpus, the legacy shared collection, another product's collection. Those are
    not reported, because reporting every collection on a shared engine as a
    near-miss is noise, not diagnostics.
    """
    from ragtools.registry import owns_collection

    if _BARE_RE.match(name):
        # Considered so it can be SEEN, never a candidate. Ownership first,
        # because "this is a live project's collection" is the more informative
        # answer for the same observation — and on a healthy installation every
        # generation-0 project lands here.
        if registry is not None and owns_collection(registry, name):
            return Candidate(
                collection=name, exclusions=(ACTIVE_POINTER,),
                detail=f"{name} is the active collection of a registered project")
        return Candidate(
            collection=name,
            exclusions=(NO_GENERATION_METADATA,),
            detail="the name carries no generation marker, so which rebuild it "
                   "belongs to cannot be established from it",
        )

    match = _GENERATION_RE.match(name)
    if not match:
        return None

    base, hexuuid, gen = match.group(1), match.group(2), int(match.group(3))
    from ragtools.identity import project_uuid_from_collection_name

    try:
        project_uuid = project_uuid_from_collection_name(base)
    except Exception:  # noqa: BLE001 — cannot invert => cannot attribute
        return Candidate(collection=name, generation=gen,
                         exclusions=(NOT_OWNED,),
                         detail=f"{base!r} does not invert to a project UUID")

    record = None
    if registry is not None:
        try:
            record = registry.get_by_uuid(project_uuid)
        except Exception as exc:  # noqa: BLE001
            return Candidate(collection=name, generation=gen,
                             project_uuid=project_uuid,
                             exclusions=(NOT_OWNED,),
                             detail=f"the registry could not be read: {exc}")

    if record is None:
        # THE STRANGER. `proj_<32 hex>` is a shape; only a registry row makes it
        # ours. Reported by name and never touched — a generation of another
        # installation's project is still that installation's data.
        return Candidate(
            collection=name, generation=gen, project_uuid=project_uuid,
            exclusions=(NOT_OWNED,),
            detail="no project in this installation's registry has that UUID, "
                   "so this collection was not created here",
        )

    active = str(record.collection_name)
    active_base = active.rpartition("_g")[0] if _GENERATION_RE.match(active) else active
    active_generation = int(getattr(record, "generation", 0) or 0)

    cand = Candidate(
        collection=name, project_id=str(record.project_id),
        project_uuid=project_uuid, generation=gen,
        active_generation=active_generation,
    )

    if active_base != base:
        # The row is ours and points somewhere else entirely. Which project this
        # generation belongs to is then a guess, and a guess is not permission.
        return replace(
            cand, exclusions=(AMBIGUOUS_IDENTITY,),
            detail=(f"project {record.project_id!r} owns this UUID but points "
                    f"at {active!r}, whose base is {active_base!r}"))

    if owns_collection(registry, name):
        # Some project's LIVE pointer. Never, under any circumstances.
        return replace(
            cand, exclusions=(ACTIVE_POINTER,),
            detail=f"{name} is the active collection of a registered project")

    return cand


def _with(cand: Candidate, reason: str, detail: str) -> Candidate:
    """Add one exclusion, accumulating rather than replacing.

    Every applicable reason is reported. A candidate excluded for three
    independent reasons that only ever shows one of them teaches an operator to
    fix that one and be surprised.
    """
    if reason in cand.exclusions:
        return cand
    joined = cand.detail
    if detail:
        joined = f"{joined}; {detail}" if joined else detail
    return replace(cand, exclusions=cand.exclusions + (reason,), detail=joined)


def reap(owner, *, apply: bool = False,
         grace_seconds: Optional[float] = None,
         now: Optional[float] = None) -> ReapReport:
    """Find — and, only when asked, delete — orphaned generation collections.

    ``apply=False`` (the default, and the shipped behaviour) deletes nothing. It
    still records first sightings, so the grace period is a real clock rather
    than something that starts the day somebody enables deletion.

    Never raises for an ordinary refusal: a diagnostic surface that 500s when the
    registry is unresolved is a diagnostic nobody can use during the incident it
    exists for. The refusal is IN the report, named.
    """
    from ragtools import registry_integrity
    from ragtools.registry import RegistryIntegrityError
    from ragtools.service import destructive

    settings = owner.settings
    stamp = time.time() if now is None else float(now)
    grace = (DEFAULT_GRACE_SECONDS if grace_seconds is None
             else float(grace_seconds))
    report = ReapReport(dry_run=not apply)

    registry = _registry_of(owner)

    # 1. GLOBAL INTEGRITY, FIRST AND UNCONDITIONALLY.
    #
    # A lost, replaced or rolled-back registry makes every collection on the
    # engine look orphaned — which is exactly the state in which reaping deletes
    # the index it exists to protect. Nothing below runs until this passes.
    try:
        registry_integrity.assert_reaping_allowed(registry)
    except RegistryIntegrityError as exc:
        report.allowed = False
        report.refusal = str(exc)
        report.notes.append(
            "no collection was inspected: while the registry cannot be vouched "
            "for, 'the registry does not claim it' is not evidence of anything")
        logger.warning("generation reap refused: %s", exc)
        return report
    except Exception as exc:  # noqa: BLE001 — an unaskable guard is a refusal
        report.allowed = False
        report.refusal = f"registry integrity could not be established: {exc}"
        return report

    try:
        existing = [c.name for c in owner.client.get_collections().collections]
    except Exception as exc:  # noqa: BLE001
        report.allowed = False
        report.refusal = f"the collection list is unavailable: {exc}"
        return report

    # 7 (part). Re-read every time. A rebuild marker is durable and nothing about
    # it expires, so the question is asked of the marker now — never carried over
    # from a previous sweep.
    hold = rebuild_hold(settings)

    # Preconditions the rest of the product already owns. Reported per candidate
    # rather than as a bare refusal so the sweep still says WHAT it saw.
    refused_code, refused_reason = "", ""
    try:
        refused_code, refused_reason = destructive.blocking_reason(
            owner, operation="reaping orphaned collections")
    except Exception:  # noqa: BLE001 — no app context; the guard below still runs
        refused_code, refused_reason = "", ""

    own_dimension = _own_dimension(owner)
    if own_dimension is None:
        report.notes.append(
            "this process cannot state its own embedding dimension, so the "
            "embedding-identity check was not applicable")

    for name in sorted(existing):
        cand = _classify(registry, name)
        if cand is None:
            continue

        if cand.exclusions:
            # Structural: identity could not be established, so every check
            # below is unanswerable. Report and move on.
            report.excluded.append(cand)
            continue

        # 5. Embedding identity, where this installation can state its own.
        if own_dimension is not None:
            declared = _collection_dimension(owner.client, name)
            if declared is None:
                cand = _with(cand, EMBEDDING_UNKNOWN,
                             "the collection's vector configuration could not "
                             "be read, so it cannot be shown to belong to this "
                             "index")
            elif declared != own_dimension:
                cand = _with(cand, EMBEDDING_MISMATCH,
                             f"the collection declares {declared} dimensions "
                             f"and this installation writes {own_dimension}")

        # The size of what would be lost. UNKNOWN IS NOT ZERO: a count that
        # could not be taken is a reason to keep the collection, never a
        # confident report that it is empty.
        points = None
        try:
            points = owner._count_points(name)
        except Exception:  # noqa: BLE001
            points = None
        cand = replace(cand, points=points)
        if points is None:
            cand = _with(cand, POINTS_UNKNOWN,
                         "the collection could not be counted, so what would "
                         "be deleted is unknown")

        # 7. Unresolved rebuild work.
        reason, detail = hold.blocks(cand)
        if reason:
            cand = _with(cand, reason, detail)

        # The product's own destructive preconditions (engine down, storage
        # unreachable, an indexing run in flight, a migration owning the index).
        if refused_code:
            cand = _with(cand, OPERATION_REFUSED,
                         f"{refused_code}: {refused_reason}")

        # 8. Grace. Recorded even for candidates already excluded for other
        # reasons — the clock should be running by the time those clear.
        try:
            first_seen = record_sighting(settings, cand, now=stamp)
        except Exception as exc:  # noqa: BLE001 — no ledger, no grace, no reap
            # NO EVIDENCE, NO DELETION. Without the ledger there is neither a
            # grace clock nor anywhere to write the audit that must precede a
            # drop, so the candidate is excluded outright rather than reaped on
            # the strength of a check that could not run.
            cand = _with(cand, LEDGER_UNAVAILABLE,
                         f"the sighting ledger could not be written ({exc}), so "
                         f"neither a grace period nor an audit trail can be "
                         f"established")
            report.excluded.append(cand)
            continue
        cand = replace(cand, first_seen=first_seen)
        waited = stamp - first_seen
        if waited < grace:
            cand = _with(
                cand, WITHIN_GRACE,
                f"first seen {waited:.0f}s ago; {grace:.0f}s must elapse before "
                f"an orphan is considered settled")

        (report.excluded if cand.exclusions else report.candidates).append(cand)

    try:
        pruned = prune_sightings(settings, set(existing))
        if pruned:
            report.notes.append(
                f"forgot {pruned} sighting(s) for collections that are gone")
    except Exception as exc:  # noqa: BLE001 — housekeeping, never the point
        report.notes.append(f"could not prune stale sightings: {exc}")

    if not apply:
        if report.candidates:
            report.notes.append(
                f"{len(report.candidates)} collection(s) are reapable; nothing "
                f"was deleted (dry run). Enable `reap_generations` or pass "
                f"--apply to reclaim them")
        return report

    _delete(owner, settings, report)
    return report


def _delete(owner, settings, report: ReapReport) -> None:
    """Drop the reapable candidates, audit-first, under the destructive lock.

    Every door into a destructive operation goes through
    :mod:`ragtools.service.destructive` — this is one of those doors, and it does
    not get its own copy of the preconditions.
    """
    from ragtools.service import destructive

    if not report.candidates:
        return

    try:
        ctx = destructive.destructive_operation(
            owner, operation="reaping orphaned collections")
    except Exception as exc:  # noqa: BLE001
        report.allowed = False
        report.refusal = str(exc)
        return

    try:
        with ctx:
            survivors: list[Candidate] = []
            for cand in report.candidates:
                # 9. THE AUDIT IS WRITTEN AND PROVEN BEFORE THE DELETE.
                #
                # An unrecorded deletion is indistinguishable from data that
                # vanished, and the moment somebody needs this record is the
                # moment the collection is already gone.
                try:
                    record_audit(settings, cand, AUDIT_INTENT,
                                 detail=f"generation {cand.generation} of "
                                        f"project {cand.project_id}")
                except Exception as exc:  # noqa: BLE001
                    survivors.append(_with(
                        cand, AUDIT_WRITE_FAILED,
                        f"the audit record could not be written ({exc}); "
                        f"nothing was deleted"))
                    continue

                try:
                    owner.client.delete_collection(cand.collection)
                except Exception as exc:  # noqa: BLE001 — one collection, not the sweep
                    report.failures.append((cand.collection, str(exc)))
                    try:
                        record_audit(settings, cand, AUDIT_FAILED, detail=str(exc))
                    except Exception:  # noqa: BLE001
                        pass
                    continue

                report.deleted.append(cand.collection)
                forget_sighting(settings, cand.collection)
                try:
                    record_audit(settings, cand, AUDIT_DELETED,
                                 detail=f"{cand.points} points")
                except Exception:  # noqa: BLE001 — the intent row already stands
                    pass
                logger.info("generation reap: dropped %s (project %s, "
                            "generation %s, %s points)", cand.collection,
                            cand.project_id, cand.generation, cand.points)

            report.candidates = []
            report.excluded.extend(survivors)
    except destructive.OperationRefused as refused:
        report.allowed = False
        report.refusal = refused.reason


def auto_reap_enabled(settings) -> bool:
    """Is unattended deletion switched on? Off unless explicitly declared.

    The switch exists so the behaviour can be turned on deliberately, once the
    dry-run report has been believed on a real machine. It is never on by
    default, and nothing infers it.
    """
    return bool(getattr(settings, "reap_generations", False))
