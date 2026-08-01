"""Is the live registry the one this index was built against? (R06, part A.)

``registry.db`` says which collection holds each project's vectors. Lose it and
the collections survive while nothing knows whose they are; restore an older
copy and a project silently points at the generation before its last rebuild.
Neither shows up in ``storage_backend``, ``collection_strategy``,
``collection_name``, ``model_name`` or ``dimension`` — the whole store looks
unchanged — which is why :mod:`ragtools.index_identity` grew a registry
fingerprint.

**One fingerprint was answering two questions, and that was the bug.**

    "Is this the same registry?"            — global, integrity
    "Is this project's mapping still valid?" — per project, skip decision

Fused, they made a plain addition catastrophic: registering project N+1 changes
the registry-wide digest, so the N projects already indexed were declared
untrustworthy and re-embedded. The cost of adding one project was the whole
corpus.

This module owns the first question ONLY. It never decides whether a file may
be skipped — :func:`ragtools.index_identity.untrusted_projects` does that, per
project, from the per-project rows. What this module decides is whether the
registry can be *vouched for at all*, and therefore whether the operations that
would make an unproven mapping permanent may run:

* minting a new project identity (a fresh UUID means a fresh EMPTY collection);
* swapping a project's active collection pointer;
* reaping "orphaned" collections — WP-R03's reaper, which must consult
  :func:`assert_reaping_allowed` before it may treat anything as orphaned.

Nothing here deletes, drops or rebuilds anything. The remedy for every failure
state is the same shape: re-adopt the existing collections (their names encode
the UUIDs), or restore ``registry.db``, then reconcile. A project whose mapping
genuinely moved is RE-INDEXED, never emptied.

**Absent is unknown, never different.** A v3.5.0 state DB has no fingerprint and
no per-project rows; it is ``unknown`` — reported, not acted on. Reading it as
"different" would mass-invalidate every install on its first start after an
upgrade, which is the failure this whole work package exists to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("ragtools.registry_integrity")

#: The live mapping is exactly the recorded one.
STATE_OK = "ok"
#: Nothing recorded to compare against (fresh install, ``shared`` layout, or a
#: state DB stamped before per-project identity existed).
STATE_UNKNOWN = "unknown"
#: The only difference is projects that were ADDED. The recorded ones are
#: untouched — this is the case the fused fingerprint used to punish.
STATE_EXTENDED = "extended"
#: A known project points at a different collection under the SAME uuid, at a
#: generation that did not go backwards: an ordinary rebuild swap. Invalidates
#: that project alone; not an integrity failure.
STATE_REPOINTED = "repointed"
#: A known project's UUID changed — its identity was re-minted, so its recorded
#: chunks belong to a collection it no longer owns.
STATE_REPLACED = "replaced"
#: A known project's generation went BACKWARDS, or a recorded project is gone
#: from a registry that still has others: an older registry was restored.
STATE_ROLLED_BACK = "rolled_back"
#: The registry cannot be read, or reports no projects while the index state
#: describes several.
STATE_MISSING = "missing"
#: The registry demonstrably changed and there are no per-project rows to say
#: how. Ambiguous, and ambiguity is not permission.
STATE_UNLOCALISABLE = "unlocalisable"

#: States in which the registry can be vouched for.
_SOUND = frozenset({STATE_OK, STATE_UNKNOWN, STATE_EXTENDED, STATE_REPOINTED})
#: States in which identity-creating writes and reaping are refused.
_BLOCKING = frozenset({STATE_REPLACED, STATE_ROLLED_BACK, STATE_MISSING,
                       STATE_UNLOCALISABLE})


@dataclass(frozen=True)
class IntegrityStatus:
    """What the live registry is, relative to what the index state recorded."""

    state: str
    reason: str = ""
    stored_fingerprint: str = ""
    live_fingerprint: str = ""
    #: Projects present live but not recorded — an addition, and by itself
    #: never a reason to distrust anything.
    added: tuple[str, ...] = ()
    #: Recorded but absent from a registry that still has other projects.
    removed: tuple[str, ...] = ()
    #: Recorded, live, same uuid, different collection, generation not
    #: regressed. One project to re-index; nothing to block.
    repointed: tuple[str, ...] = ()
    #: Recorded, live, DIFFERENT uuid. The registry minted a new identity.
    reidentified: tuple[str, ...] = ()
    #: Recorded, live, same uuid, generation regressed.
    regressed: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Can the live registry be vouched for?"""
        return self.state in _SOUND

    @property
    def degraded(self) -> bool:
        """Should health say so? Anything that is not sound."""
        return not self.ok

    @property
    def blocking(self) -> bool:
        """Are identity-creating writes and reaping refused?"""
        return self.state in _BLOCKING

    @property
    def affected(self) -> tuple[str, ...]:
        """Projects whose own mapping diverged — the RE-INDEX set, never a
        delete set."""
        return tuple(sorted(set(self.repointed) | set(self.reidentified)
                            | set(self.regressed)))

    def describe(self) -> str:
        return f"{self.state}: {self.reason}" if self.reason else self.state


#: Last status this process computed, for reporting surfaces that must stay
#: cheap (``/health`` is polled). Set by :func:`reconcile_startup`; ``None``
#: until a reconciliation has run, which is honestly "not established yet".
_last: Optional[IntegrityStatus] = None


def last_status() -> Optional[IntegrityStatus]:
    """The most recent reconciliation's verdict, or None if none has run."""
    return _last


def _record(status: IntegrityStatus) -> IntegrityStatus:
    global _last
    _last = status
    return status


def reset_for_tests() -> None:
    """Forget the cached status. Test-support only."""
    global _last
    _last = None


@dataclass
class _Live:
    """The registry side of the comparison, read once."""

    fingerprint: str = ""
    records: dict = field(default_factory=dict)   # project_id -> (uuid, coll, gen)


def _read_live(registry) -> _Live:
    from ragtools.registry import registry_fingerprint

    live = _Live(fingerprint=registry_fingerprint(registry))
    for rec in registry.list(include_archived=True):
        live.records[rec.project_id] = (
            str(rec.uuid), str(rec.collection_name),
            int(getattr(rec, "generation", 0) or 0),
        )
    return live


def _stored_fingerprint(state) -> str:
    from ragtools.index_identity import META_KEY, IndexIdentity

    raw = state.get_meta(META_KEY)
    if not raw:
        return ""
    try:
        return IndexIdentity.from_json(raw).registry_fingerprint
    except Exception:  # noqa: BLE001 — an unreadable stamp records nothing
        return ""


def evaluate(state, registry) -> IntegrityStatus:
    """Compare the live registry with the mapping the index state recorded.

    Pure: reads both sides, decides nothing else. It does not arm the hold, does
    not touch the cached status, and — like everything in this module — does not
    delete, drop or rebuild anything.

    ``registry is None`` is ``unknown``, not a failure: under ``shared`` there
    is no registry to have integrity about, and a layout flip is already the
    structural :class:`~ragtools.index_identity.IndexIdentity`'s business.
    """
    from ragtools.index_identity import stored_project_identities

    recorded = stored_project_identities(state)
    stored_fp = _stored_fingerprint(state)

    if registry is None:
        return IntegrityStatus(
            STATE_UNKNOWN,
            "no project registry is open; the collection layout is 'shared' or "
            "the registry was not built",
            stored_fingerprint=stored_fp,
        )

    try:
        live = _read_live(registry)
    except Exception as exc:  # noqa: BLE001 — an unreadable registry IS the fault
        return IntegrityStatus(
            STATE_MISSING, f"the registry could not be read: {exc}",
            stored_fingerprint=stored_fp,
        )

    if not recorded and not stored_fp:
        return IntegrityStatus(
            STATE_UNKNOWN,
            "this index state records no registry mapping, so there is nothing "
            "to compare the live one against",
            live_fingerprint=live.fingerprint,
        )

    if stored_fp and live.fingerprint and stored_fp == live.fingerprint:
        return IntegrityStatus(STATE_OK, "", stored_fp, live.fingerprint)

    if recorded and not live.records:
        return IntegrityStatus(
            STATE_MISSING,
            f"the registry reports no projects while the index state describes "
            f"{len(recorded)} ({', '.join(sorted(recorded)[:5])})",
            stored_fp, live.fingerprint,
            removed=tuple(sorted(recorded)),
        )

    if not recorded:
        return IntegrityStatus(
            STATE_UNLOCALISABLE,
            "the registry mapping changed and this index state has no "
            "per-project record to say which project moved",
            stored_fp, live.fingerprint,
        )

    added = tuple(sorted(set(live.records) - set(recorded)))
    removed = tuple(sorted(set(recorded) - set(live.records)))
    reidentified: list[str] = []
    regressed: list[str] = []
    repointed: list[str] = []
    for pid in sorted(set(recorded) & set(live.records)):
        was = recorded[pid]
        uuid_now, collection_now, generation_now = live.records[pid]
        if was.project_uuid != uuid_now:
            # Not a move: a different identity wearing the same project id.
            reidentified.append(pid)
        elif generation_now < was.generation:
            # Generations only ever go forward. Backwards means an older
            # registry came back — the one case a fingerprint alone cannot
            # tell from an ordinary swap.
            regressed.append(pid)
        elif was.collection_name != collection_now:
            repointed.append(pid)

    common = dict(stored_fingerprint=stored_fp, live_fingerprint=live.fingerprint,
                  added=added, removed=removed, repointed=tuple(repointed),
                  reidentified=tuple(reidentified), regressed=tuple(regressed))

    if reidentified:
        return IntegrityStatus(
            STATE_REPLACED,
            f"{len(reidentified)} project(s) have a different registry identity "
            f"than the one this index was built against "
            f"({', '.join(reidentified[:5])}); their collections still hold the "
            f"vectors, so re-adopt rather than re-create",
            **common)
    if regressed or removed:
        parts = []
        if regressed:
            parts.append(f"{len(regressed)} project(s) moved BACK a generation "
                         f"({', '.join(regressed[:5])})")
        if removed:
            parts.append(f"{len(removed)} recorded project(s) are absent "
                         f"({', '.join(removed[:5])})")
        return IntegrityStatus(
            STATE_ROLLED_BACK,
            "the live registry is older than the one this index was built "
            "against: " + "; ".join(parts),
            **common)
    if repointed:
        return IntegrityStatus(
            STATE_REPOINTED,
            f"{len(repointed)} project(s) were repointed at a newer collection "
            f"({', '.join(repointed[:5])}); each is re-indexed on its own",
            **common)
    if added:
        return IntegrityStatus(
            STATE_EXTENDED,
            f"{len(added)} project(s) were added ({', '.join(added[:5])}); the "
            f"projects already indexed are unaffected",
            **common)
    return IntegrityStatus(STATE_OK, "", **common)


def reconcile_startup(state, registry) -> IntegrityStatus:
    """Evaluate, then arm or release the registry's hold. THE enforcement seam.

    Called once per owner construction, and again after a run has re-stamped the
    mapping — a hold must be able to clear itself, or an install that has since
    proven its registry stays wedged until it is restarted.

    Arming is the whole point of evaluating: a status nobody acts on is a log
    line. Releasing is equally deliberate — the hold reflects THIS process's
    current belief, so a reconciliation that finds the mapping sound must undo
    a hold an earlier one armed.
    """
    status = _record(evaluate(state, registry))
    if registry is None:
        return status
    if status.blocking:
        registry.hold(status.reason)
        logger.warning("Registry integrity %s — identity-creating writes and "
                       "collection reaping are refused: %s",
                       status.state, status.reason)
    else:
        if registry.hold_reason:
            logger.info("Registry integrity resolved (%s); writes re-enabled",
                        status.state)
        registry.release_hold()
        if status.degraded:
            logger.warning("Registry integrity %s: %s", status.state, status.reason)
    return status


# --- the guard other work packages consult --------------------------------


def registry_integrity_ok(registry) -> bool:
    """May operations that assume a trustworthy mapping proceed?

    The predicate WP-R03's generation reaper consults. False while a hold is in
    force, and False for a registry that is not there at all — "there is nothing
    to ask" is not the same as "yes".
    """
    if registry is None:
        return False
    return not getattr(registry, "hold_reason", "")


def integrity_block_reason(registry) -> str:
    """Why such operations are refused, or "" when they are not."""
    if registry is None:
        return "no project registry is open"
    return getattr(registry, "hold_reason", "")


def require_integrity(registry, operation: str) -> None:
    """Raise unless the registry can be vouched for.

    Named for the caller's intent so the refusal says what was refused, rather
    than leaving an operator to guess which subsystem gave up.
    """
    from ragtools.registry import RegistryIntegrityError

    reason = integrity_block_reason(registry)
    if reason:
        raise RegistryIntegrityError(
            f"refusing to {operation}: {reason}. Nothing has been deleted; "
            f"re-adopt the existing collections or restore registry.db, then "
            f"reconcile."
        )


def assert_reaping_allowed(registry) -> None:
    """The guard WP-R03 must call before treating any collection as orphaned.

    A collection looks orphaned exactly when the registry does not claim it —
    which is precisely what a lost, replaced or rolled-back registry produces
    for EVERY collection this installation owns. Reaping on that evidence
    deletes the index it was meant to protect, so the reaper does not get to
    decide: it asks here first, and here refuses while integrity is unresolved.
    """
    require_integrity(registry, "reap orphaned collections")


def verify_restored_mapping(registry, expected: dict) -> tuple[bool, list[str]]:
    """Did a restore bring back EXACTLY the recorded ``project_id -> collection``?

    Returns ``(ok, discrepancies)``. A restore that lands a different mapping is
    not a partial success — every project it disagrees about is pointed at the
    wrong vectors — so the discrepancies are named, per project, rather than
    summarised into a boolean somebody has to trust.
    """
    from ragtools.registry import project_mapping

    live = project_mapping(registry)
    problems: list[str] = []
    for pid in sorted(set(expected) | set(live)):
        want, got = expected.get(pid), live.get(pid)
        if want == got:
            continue
        if want is None:
            problems.append(f"{pid}: unexpected project pointing at {got!r}")
        elif got is None:
            problems.append(f"{pid}: missing; expected {want!r}")
        else:
            problems.append(f"{pid}: expected {want!r}, restored {got!r}")
    return (not problems), problems
