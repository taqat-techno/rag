"""Recovering from a handled failure without restarting anything — WP-R05.

A rebuild that ends with failures writes ``rebuild-intent.json`` rather than
clearing it (:func:`ragtools.service.destructive.record_intent`), naming the
projects it could not finish. ``/health`` then reports ``rebuild_interrupted``
— and that was the whole of it. Nothing re-drove those projects, nothing
re-tested why they failed, and nothing ever removed the marker. The product's
own words for the remedy were "Use Rebuild or restart the service to recover."

**"Restart the service" is not a remedy.** It is an instruction to lose the
state that explains the failure, and here it did not even work: the interrupted
rebuild is re-driven by nothing on start either, so a user who followed the
advice got the same banner back with a fresh timestamp.

What this module does instead, on the ordinary maintenance tick:

* **Reads the pending intent marker** and adopts it into a plan — one unit per
  project the rebuild could not finish.
* **Re-tests the preconditions.** A persisted ``blocked_reason`` is a record of
  what was true when it was written, and nothing about that record expires. A
  two-hour-old ``WinError 10061`` shown beside a healthy engine is how a health
  payload loses its credibility, so the reason is measured NOW, re-written with
  what is currently true, and reported with the time it was measured.
* **Re-drives with bounded backoff**, using :mod:`ragtools.upgrade.relayout`'s
  accounting — ``units_to_do``, ``mark(count_attempt=True)``, ``reset_attempts``,
  ``exhausted_units``. Not a second retry policy: that module already learned,
  from an observed CPU loop, that an unbounded retry of a full re-index is how a
  machine spins forever without converging.
* **Clears the marker only when the work is genuinely done** — every unit
  rebuilt and counted. A marker cleared on a hopeful tick is exactly the
  ``finally``-clause bug that left ``/health`` reporting a clean bill of health
  over a half-rebuilt index for twelve hours.

The plan is created at :data:`~ragtools.upgrade.relayout.PLAN_RECOVERING`, not
``running``. ``guard_ready`` refuses every search while a plan is ``running`` —
right for a layout migration, where the index really is half-built, and the
v3.1.0 disproportion again if one project's failed rebuild disabled retrieval
for the other twenty-four.

Nothing here is destructive. It re-indexes; it never drops a collection, retires
old storage, or deletes a project's rows.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from ragtools.upgrade import relayout

logger = logging.getLogger("ragtools.service.recovery")

#: How often the unattended recovery tick runs. Short enough that a blocker
#: lifting is noticed in minutes, long enough that a re-test costs nothing.
TICK_SECONDS = 5 * 60.0


@dataclass(frozen=True)
class Precondition:
    """Whether recovery may proceed RIGHT NOW, and when that was established.

    ``at`` is the point of the whole type. Without it a caller cannot tell a
    verdict measured a second ago from one written down during an outage that
    ended hours later, and it is precisely that confusion this work package
    exists to remove.
    """

    ok: bool
    code: str = ""
    reason: str = ""
    at: float = 0.0

    def as_dict(self, *, now: Optional[float] = None) -> dict:
        stamp = time.time() if now is None else now
        return {
            "ok": self.ok,
            "code": self.code,
            "reason": self.reason,
            "retested_at": self.at,
            "retested_seconds_ago": (max(0.0, stamp - self.at) if self.at else None),
        }


def retest_preconditions(owner) -> Precondition:
    """Ask the store, the engine and the index whether recovery can run — now.

    Delegates to :func:`ragtools.service.destructive.blocking_reason`, which is
    the ONE place that answers "may something that mutates the index run?".
    Copying the checks here is how three of four doors stay correct.
    """
    from ragtools.service import destructive

    try:
        code, reason = destructive.blocking_reason(
            owner, operation="recovering an interrupted rebuild")
    except Exception as exc:  # noqa: BLE001 — a guard that cannot answer refuses
        return Precondition(False, "precondition_unavailable",
                            f"the preconditions could not be evaluated: {exc}",
                            time.time())
    if code:
        return Precondition(False, code, reason, time.time())
    return Precondition(True, "", "", time.time())


@dataclass
class RecoveryReport:
    """What one recovery tick did. Reported, never silent."""

    plan_id: Optional[int] = None
    precondition: Optional[Precondition] = None
    #: Units re-driven successfully on this tick.
    rebuilt: list = field(default_factory=list)
    #: ``(kind, unit_id, error)`` for units that failed again.
    failed: list = field(default_factory=list)
    #: Units offered no attempt because their backoff had not elapsed or their
    #: automatic budget was spent.
    deferred: list = field(default_factory=list)
    #: True when the blocker is still real and every unit was left parked.
    parked: bool = False
    #: True when the marker was cleared because the work is genuinely finished.
    resolved: bool = False
    notes: list = field(default_factory=list)

    @property
    def acted(self) -> bool:
        return bool(self.rebuilt or self.failed or self.resolved)

    def describe(self) -> str:
        if self.plan_id is None:
            return "nothing to recover"
        if self.parked:
            reason = self.precondition.reason if self.precondition else "unknown"
            return (f"recovery plan {self.plan_id} still blocked (re-tested): "
                    f"{reason}")
        if self.resolved:
            return f"recovery plan {self.plan_id} resolved"
        return (f"recovery plan {self.plan_id}: {len(self.rebuilt)} rebuilt, "
                f"{len(self.failed)} failed, {len(self.deferred)} deferred")


# --- adopting the marker ---------------------------------------------------


def _projects_from(intent: dict) -> list[str]:
    """Which projects an interrupted rebuild left unfinished.

    ``failed_projects`` when the run ENDED with failures and said which; the full
    project list when the process was killed outright and never got to say. The
    second is deliberately the wider set: a hard kill leaves no evidence about
    which project it happened during, and re-indexing one that was already fine
    costs time, whereas skipping one that was not costs the user their content.
    """
    failed = [str(p) for p in (intent.get("failed_projects") or []) if str(p)]
    if failed:
        return sorted(set(failed))
    return sorted({str(p) for p in (intent.get("projects") or []) if str(p)})


def pending_plan(settings) -> Optional[int]:
    """The unresolved recovery plan, or None. Creates nothing."""
    return relayout.plan_with_status(settings, relayout.PLAN_RECOVERING)


def ensure_plan(owner, settings) -> Optional[int]:
    """Adopt a pending intent marker into a recovery plan, idempotently.

    Returns the plan id, or ``None`` when there is nothing unresolved. An
    existing recovery plan is reused rather than superseded: a second plan would
    start every unit's attempt budget again, which is the unbounded retry this
    borrows relayout's accounting to avoid.
    """
    from ragtools.service import destructive

    existing = pending_plan(settings)
    if existing is not None:
        return existing

    intent = destructive.pending_intent(settings)
    if intent is None:
        return None

    projects = _projects_from(intent)
    if not projects:
        # A marker that says a rebuild is unresolved without saying what it
        # touched. There is nothing to re-drive, and inventing a unit list from
        # the live configuration would re-index projects the rebuild never
        # reached. Report it; do not act on it.
        logger.warning(
            "a rebuild intent marker is present but names no projects; there is "
            "nothing to re-drive automatically")
        return None

    backend = str(getattr(settings, "storage_backend", "embedded") or "embedded")
    strategy = str(getattr(settings, "collection_strategy", "shared") or "shared")
    inventory = relayout.Inventory(
        units=[relayout.Unit(relayout.KIND_PROJECT, pid) for pid in projects])
    plan_id = relayout.begin(
        settings, inventory,
        from_backend=backend, to_backend=backend,
        from_strategy=strategy, to_strategy=strategy,
        status=relayout.PLAN_RECOVERING)
    logger.warning(
        "adopting the interrupted rebuild into recovery plan %s: %s",
        plan_id, ", ".join(projects))
    return plan_id


def _unblock(settings, plan_id: int) -> list:
    """Make every parked unit runnable again, with a fresh attempt budget.

    Called only once the preconditions have been RE-TESTED and found sound. A
    blocked unit was never charged an attempt — nothing about it failed — so
    clearing the budget restores it to the state it would have been in had the
    outage never happened. Same rule, same reason, as
    :func:`ragtools.upgrade.relayout.reconcile`.
    """
    unblocked = []
    report = relayout.progress(settings, plan_id)
    if report is None or not report.blocked:
        return unblocked
    conn = relayout._connect(settings)
    try:
        rows = conn.execute(
            "SELECT kind, unit_id, points_before FROM relayout_unit"
            " WHERE plan_id=? AND status=?",
            (plan_id, relayout.STATUS_BLOCKED)).fetchall()
    finally:
        conn.close()
    for kind, unit_id, before in rows:
        unit = relayout.Unit(str(kind), str(unit_id), int(before or 0))
        relayout.mark(settings, plan_id, unit, relayout.STATUS_PENDING,
                      fresh_attempts=True)
        unblocked.append((unit.kind, unit.unit_id))
    return unblocked


# --- the tick ---------------------------------------------------------------


def drive(owner, settings=None, *, max_units: Optional[int] = None) -> RecoveryReport:
    """One recovery pass. Safe and cheap to call on a timer.

    Returns a report even when there is nothing to do, so a caller can say what
    it found rather than only what it changed.
    """
    from ragtools.service import destructive

    settings = settings if settings is not None else owner.settings
    report = RecoveryReport()

    plan_id = ensure_plan(owner, settings)
    if plan_id is None:
        return report
    report.plan_id = plan_id

    # RE-TESTED, NOT REMEMBERED. This is the whole point of the work package:
    # the blocked reason on record is a fact about the past, and the only honest
    # thing to do with it is measure the condition again.
    check = retest_preconditions(owner)
    report.precondition = check
    remember_check(check)
    if not check.ok:
        # Re-written with what is true NOW, so the persisted reason stops ageing
        # into a lie. `block_all` charges no attempt: an unreachable backend is
        # not the unit's failure.
        parked = relayout.block_all(settings, plan_id, check.reason)
        report.parked = True
        report.notes.append(
            f"{parked} unit(s) left parked; the blocker was re-tested and is "
            f"still present ({check.code})")
        logger.info("recovery plan %s: %s", plan_id, report.describe())
        return report

    unblocked = _unblock(settings, plan_id)
    if unblocked:
        report.notes.append(
            f"{len(unblocked)} unit(s) unblocked: the recorded blocker was "
            f"re-tested and no longer describes reality")

    todo = relayout.units_to_do(settings, plan_id)
    exhausted = relayout.exhausted_units(settings, plan_id)
    if exhausted:
        # Never silently capped. A bounded retry that says nothing is
        # indistinguishable from one that finished.
        report.deferred.extend((k, u) for k, u, _ in exhausted)
        report.notes.append(
            f"{len(exhausted)} unit(s) have spent their {relayout.MAX_ATTEMPTS} "
            f"automatic attempts and will not be retried on their own")

    if max_units is not None:
        todo = todo[:max_units]

    for unit in todo:
        _drive_unit(owner, settings, plan_id, unit, report)

    if relayout.units_all_done(settings, plan_id):
        relayout.finalize(settings, plan_id)
        # ONLY NOW. The marker is the only durable evidence that a rebuild did
        # not finish; clearing it on a hopeful tick is how `/health` came to
        # report `degraded: false, issues: []` over a half-rebuilt index.
        destructive.clear_intent(settings)
        report.resolved = True
        logger.info("recovery plan %s resolved; the interrupted rebuild is "
                    "no longer pending", plan_id)
    return report


def _drive_unit(owner, settings, plan_id: int, unit, report: RecoveryReport) -> None:
    """Re-drive one project through the established indexing pipeline.

    ``run_full_index`` — not a second indexer, and not the rebuild's build →
    verify → swap. A rebuild is how the collection came to be wrong; re-running
    the one indexing pipeline over the project is how it is made right, and it is
    exactly what :func:`ragtools.upgrade.relayout.run_pending` does for the same
    reason.
    """
    try:
        stats = owner.run_full_index(project_id=unit.unit_id)
    except Exception as exc:  # noqa: BLE001 — one unit, not the tick
        still = retest_preconditions(owner)
        remember_check(still)
        if not still.ok:
            # The store went away mid-run. Every remaining unit would fail for a
            # reason that has nothing to do with any of them, and charging each
            # an attempt exhausts the plan's budget over one outage.
            relayout.block_all(settings, plan_id, still.reason)
            report.parked = True
            report.precondition = still
            report.notes.append(
                f"parked mid-tick: {still.reason}")
            return
        relayout.mark(settings, plan_id, unit, relayout.STATUS_FAILED,
                      error=f"{type(exc).__name__}: {exc}", count_attempt=True)
        report.failed.append((unit.kind, unit.unit_id, f"{type(exc).__name__}: {exc}"))
        logger.warning("recovery: %s %s failed again: %s",
                       unit.kind, unit.unit_id, exc)
        return

    # A SKIPPED RUN IS NOT A COMPLETED ONE. `run_full_index` takes the index
    # mutex non-blocking and returns `{"busy": True}` when another run holds it.
    if isinstance(stats, dict) and stats.get("busy"):
        report.deferred.append((unit.kind, unit.unit_id))
        report.notes.append(
            f"{unit.kind} {unit.unit_id} was skipped: another indexing run holds "
            f"the mutex. It stays unfinished for the next tick rather than being "
            f"recorded as rebuilt")
        return

    after = relayout.points_for_unit(owner, unit.kind, unit.unit_id)
    if after == relayout.POINTS_UNKNOWN:
        # Unknown is not success and it is not loss. Leave the unit unfinished so
        # the next tick asks again, and charge the attempt so it cannot loop.
        relayout.mark(settings, plan_id, unit, relayout.STATUS_FAILED,
                      error="rebuilt, but its points could not be counted",
                      count_attempt=True)
        report.failed.append((unit.kind, unit.unit_id,
                              "its points could not be counted"))
        return

    empty_reason = ""
    if after == 0:
        disposition, reason = relayout.classify_empty(
            owner, settings, unit.kind, unit.unit_id)
        if disposition != relayout.STATUS_DONE:
            relayout.mark(settings, plan_id, unit, relayout.STATUS_FAILED,
                          error=reason, count_attempt=True)
            report.failed.append((unit.kind, unit.unit_id, reason))
            return
        empty_reason = reason

    relayout.mark(settings, plan_id, unit, relayout.STATUS_DONE,
                  points_after=after, empty_reason=empty_reason)
    report.rebuilt.append((unit.kind, unit.unit_id, after))
    logger.info("recovery: %s %s rebuilt (%s points%s)", unit.kind, unit.unit_id,
                after, f"; {empty_reason}" if empty_reason else "")


def resume(owner, settings=None) -> RecoveryReport:
    """The operator's retry: a fresh attempt budget, then a drive.

    Automatic retries are bounded precisely so a machine cannot loop. An operator
    who has fixed the cause is a different thing entirely, and only they know the
    cause was fixed — the same authority ``rag upgrade --resume`` carries for a
    migration.
    """
    settings = settings if settings is not None else owner.settings
    plan_id = pending_plan(settings)
    if plan_id is not None:
        relayout.reset_attempts(settings, plan_id)
    return drive(owner, settings)


# --- what /health shows -----------------------------------------------------

#: The most recent re-test, so a status surface can report WHEN the verdict was
#: taken without taking one itself. ``/health`` is polled; a probe there is a
#: probe per poll.
_last_check: Optional[Precondition] = None


def remember_check(check: Precondition) -> None:
    global _last_check
    _last_check = check


def last_check() -> Optional[Precondition]:
    return _last_check


def reset_for_tests() -> None:
    """Forget the cached verdict. Test-support only."""
    global _last_check
    _last_check = None


def remedy(settings) -> str:
    """What actually clears an unresolved rebuild on THIS installation.

    Never "restart the service". A restart re-drives nothing — the interrupted
    rebuild was not resumed on start either — so advertising it produced the same
    banner with a fresher timestamp and taught the user the product was lying.
    """
    return ("the service retries automatically every few minutes; nothing needs "
            "restarting. If a project has spent its automatic attempts, "
            "`rag recover --retry` (or POST /api/recovery/retry) gives it a "
            "fresh budget once the cause is fixed")


def health(settings) -> Optional[dict]:
    """The unresolved-recovery block for ``/health``, or ``None`` when clear.

    Cheap and lock-free: it reads the plan store and the cached verdict, and
    takes no probe of its own. It reports the recorded blocker AND the re-test
    side by side, deliberately — naming a persisted reason without saying when it
    was last true is the habit this work package removes.
    """
    plan_id = pending_plan(settings)
    if plan_id is None:
        return None
    report = relayout.progress(settings, plan_id)
    if report is None:
        return None

    check = last_check()
    exhausted = relayout.exhausted_units(settings, plan_id)
    return {
        "plan": plan_id,
        "state": report.describe(),
        "total": report.total,
        "done": report.done,
        "failed": report.failed,
        "pending": report.pending,
        "blocked": report.blocked,
        # Named for what it is: the reason recorded WHEN the block was written,
        # which may no longer describe the world.
        "blocked_reason_recorded": report.blocked_reason or None,
        # And what is true now, with the time it was established. `None` means no
        # tick has run yet in this process — honestly "not established", never
        # "fine".
        "precondition": None if check is None else check.as_dict(),
        "attempts_exhausted": [
            {"kind": k, "id": u, "attempts": a} for k, u, a in exhausted],
        "remedy": remedy(settings),
        "unresolved": True,
    }
