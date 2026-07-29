"""The destructive layout transition, tracked per unit so it can be resumed.

Migrating a legacy installation to `managed + per_project` throws the old shared
index away and rebuilds every project and framework corpus under the new layout.
On a real corpus that is hours of work, and the machine will be restarted in the
middle of it — by a reboot, a crash, an impatient user, or an installer.

Three properties make that survivable, and none of them are optional:

**The inventory is captured BEFORE anything is destroyed.** What has to be
rebuilt is a fact about the old index, and the old index is what the transition
removes. Capture it afterwards and the answer is "nothing" — the transition would
declare itself complete having rebuilt none of it. This is the single most
dangerous ordering in the whole feature.

**Progress is per unit, and durable.** A project that finished must not be
re-indexed after a restart, and one that failed must not be silently skipped. So
each project and each framework corpus is its own row with its own status, and
the work to do on resume is a query rather than a guess.

**Incomplete is not ready.** While units remain, the product reports
`migration/reindex in progress` and search says so. Returning an empty result
from a half-built index, with the ordinary "no matches" shape, would tell the
user their content is gone — the one answer that is both wrong and completely
convincing.

State lives in SQLite beside the index state, not in a JSON file: the project
already decided that (`CLAUDE.md`, "Do NOT use JSON files for state"), and the
reason applies exactly here — this file is written on every unit completion,
concurrently with indexing, and a torn write loses the inventory.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ragtools.upgrade.relayout")

KIND_PROJECT = "project"
KIND_FRAMEWORK = "framework"

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

#: The storage backend was unreachable, so this unit was never attempted.
#:
#: A distinct state because it is a distinct fact, and collapsing it into
#: ``failed`` is what produced the observed CPU loop: an unreachable engine and
#: a corrupt file were recorded identically, so the retry policy could not tell
#: "try again when storage returns" from "this will never work". Blocked units
#: consume no attempts — nothing about them failed.
STATUS_BLOCKED = "blocked"

PLAN_RUNNING = "running"
PLAN_COMPLETE = "complete"

#: How many times a unit is retried automatically before it is left alone.
#: `rag upgrade --resume` clears this: an operator who has fixed the cause is
#: entitled to a fresh budget, and only they know the cause was fixed.
MAX_ATTEMPTS = 3

#: Backoff between automatic attempts. Bounded and short — the expensive part is
#: the rebuild, not the wait, and a long sleep in a startup thread is its own
#: problem.
_BACKOFF_SECONDS = (0.0, 30.0, 300.0)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS relayout_plan (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     REAL NOT NULL,
    from_backend   TEXT NOT NULL,
    to_backend     TEXT NOT NULL,
    from_strategy  TEXT NOT NULL,
    to_strategy    TEXT NOT NULL,
    status         TEXT NOT NULL,
    finished_at    REAL
);
CREATE TABLE IF NOT EXISTS relayout_unit (
    plan_id        INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    unit_id        TEXT NOT NULL,
    status         TEXT NOT NULL,
    points_before  INTEGER NOT NULL DEFAULT 0,
    points_after   INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    updated_at     REAL NOT NULL,
    PRIMARY KEY (plan_id, kind, unit_id)
);
"""


@dataclass(frozen=True)
class Unit:
    """One project or framework corpus that must exist in the new layout."""

    kind: str
    unit_id: str
    points_before: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.unit_id)


@dataclass
class Inventory:
    """Everything the OLD index held, captured before it is touched."""

    units: list[Unit] = field(default_factory=list)
    total_points: int = 0

    def __bool__(self) -> bool:
        return bool(self.units)

    def describe(self) -> str:
        projects = sum(1 for u in self.units if u.kind == KIND_PROJECT)
        frameworks = sum(1 for u in self.units if u.kind == KIND_FRAMEWORK)
        return (f"{projects} project(s), {frameworks} framework corpora, "
                f"{self.total_points:,} points")


@dataclass
class Progress:
    """What a caller needs to report, without needing to know the schema."""

    plan_id: int
    status: str
    total: int = 0
    done: int = 0
    failed: int = 0
    pending: int = 0
    blocked: int = 0
    #: Why the backend was unreachable, when anything is blocked.
    blocked_reason: str = ""
    failures: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.status == PLAN_COMPLETE

    @property
    def in_progress(self) -> bool:
        return self.status == PLAN_RUNNING

    @property
    def stalled(self) -> bool:
        """Nothing more will happen on its own. Distinct from "still working".

        An installer, `/health` and `selfcheck` all need to tell "a rebuild is
        running, wait" from "a rebuild has stopped and needs you" — those want
        opposite words and opposite remedies.
        """
        return not self.complete and bool(self.blocked or self.failed)

    def describe(self) -> str:
        if self.complete:
            return f"migration complete ({self.done}/{self.total} units)"
        parts = [f"{self.done}/{self.total} rebuilt"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.blocked:
            detail = f"{self.blocked} blocked"
            if self.blocked_reason:
                detail += f" ({self.blocked_reason})"
            parts.append(detail)
        if self.blocked and not self.done:
            return "migration/reindex BLOCKED — " + ", ".join(parts)
        return "migration/reindex in progress — " + ", ".join(parts)



class MigrationInProgress(RuntimeError):
    """Raised instead of answering a search from a half-built index.

    Returning an empty list would be indistinguishable from "your query matched
    nothing" — the ordinary, reassuring answer — at the exact moment the index
    genuinely does not contain the user's content yet. Raising forces every
    caller to say what is actually happening.
    """

    def __init__(self, report: "Progress"):
        self.report = report
        super().__init__(report.describe())


def guard_ready(settings) -> None:
    """Refuse to serve a query while the index is being rebuilt."""
    plan = active_plan(settings)
    if plan is None:
        return
    report = progress(settings, plan)
    if report is not None and not report.complete:
        raise MigrationInProgress(report)

def _db_path(settings) -> Path:
    """Beside the index state, so one data directory holds one machine's state."""
    return Path(settings.state_db).with_name("relayout.db")


def _connect(settings) -> sqlite3.Connection:
    path = _db_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.executescript(_SCHEMA)
    _add_retry_columns(conn)
    return conn


def _add_retry_columns(conn: sqlite3.Connection) -> None:
    """Bring an existing plan store up to the retry schema.

    ``CREATE TABLE IF NOT EXISTS`` does not add columns, and a machine that is
    mid-migration right now — which is exactly the machine this release is for —
    already has the table. So the columns are added here, tolerantly: a
    duplicate-column error means another process got there first, or we did on a
    previous run.
    """
    # Asked, not attempted-and-caught. `_connect` runs on every progress read,
    # every unit mark and every readiness probe, so two failing ALTER
    # transactions per connection is real churn on a hot-ish path — and
    # `/health` polls this. One PRAGMA answers it.
    try:
        present = {row[1] for row in conn.execute("PRAGMA table_info(relayout_unit)")}
    except sqlite3.Error:
        return
    if not present:
        return  # table not created yet; the schema script owns that

    for column, ddl in (
        ("attempts",
         "ALTER TABLE relayout_unit ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"),
        ("next_attempt",
         "ALTER TABLE relayout_unit ADD COLUMN next_attempt REAL"),
    ):
        if column in present:
            continue
        try:
            with conn:
                conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # another process won the race


# --- capture, which must happen before anything is destroyed --------------


def capture_inventory(settings, owner=None) -> Inventory:
    """What the CURRENT index holds, as the list of things to rebuild.

    Takes SETTINGS, not an owner, deliberately. The inventory has to be captured
    before the configuration changes, and an owner cannot exist that early —
    constructing one is what opens the store and creates collections for the new
    layout. Making this need an owner would force the capture to happen after the
    thing it is capturing has already been replaced.

    Read from the state DB rather than from Qdrant: the state DB knows which
    project each file belonged to, which is the unit of work, whereas a
    collection only knows points. It is also independent of the collection
    layout, so it answers the same before and after the switch.

    A project configured but never indexed is deliberately included — it is part
    of the inventory to validate, and re-indexing it is cheap.
    """
    from ragtools.indexing.state import IndexState

    units: list[Unit] = []
    total = 0

    if owner is not None:
        try:
            total, _per = owner._collection_points()
        except Exception:  # noqa: BLE001 — a count we cannot take is not a blocker
            total = 0

    seen: set[str] = set()
    try:
        state = IndexState(settings.state_db)
        try:
            summary = state.get_summary()
            for project_id in summary.get("projects") or []:
                records = state.get_all_for_project(project_id)
                units.append(Unit(KIND_PROJECT, project_id,
                                  sum(r["chunk_count"] for r in records)))
                seen.add(project_id)
        finally:
            state.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read the index state for inventory: %s", exc)

    # Configured but never indexed still belongs in the inventory: the
    # transition must leave every configured project queryable, and "it had no
    # rows before" is not a reason to skip validating it afterwards.
    for project in getattr(settings, "enabled_projects", []) or []:
        if project.id not in seen:
            units.append(Unit(KIND_PROJECT, project.id, 0))
            seen.add(project.id)

    # Framework corpora are indexed once and shared; they are units in their own
    # right because a project linking one is not responsible for rebuilding it.
    try:
        frameworks = getattr(owner, "_frameworks", None) if owner else None
        if frameworks is not None:
            for record in frameworks.all_frameworks():
                units.append(Unit(KIND_FRAMEWORK, str(record.get("id") or record),
                                  0))
    except Exception:  # noqa: BLE001 — no framework registry in shared mode
        pass

    return Inventory(units=units, total_points=total)


def begin(settings, inventory: Inventory, *, from_backend: str, to_backend: str,
          from_strategy: str, to_strategy: str) -> int:
    """Persist the plan and its units. Returns the plan id.

    Called BEFORE the old index is retired. If the process dies immediately
    afterwards, the next start finds a running plan with every unit pending —
    which is exactly the correct state to resume from.
    """
    conn = _connect(settings)
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO relayout_plan (created_at, from_backend, to_backend,"
                " from_strategy, to_strategy, status) VALUES (?,?,?,?,?,?)",
                (time.time(), from_backend, to_backend, from_strategy,
                 to_strategy, PLAN_RUNNING))
            plan_id = int(cursor.lastrowid or 0)
            conn.executemany(
                "INSERT OR REPLACE INTO relayout_unit"
                " (plan_id, kind, unit_id, status, points_before, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                [(plan_id, u.kind, u.unit_id, STATUS_PENDING, u.points_before,
                  time.time()) for u in inventory.units])
        logger.info("relayout plan %s: %s", plan_id, inventory.describe())
        return plan_id
    finally:
        conn.close()


def active_plan(settings) -> Optional[int]:
    """The running plan, or None. This is what "am I ready?" consults.

    Asks WITHOUT creating anything. `_connect` runs the schema script, so
    connecting to find out whether a plan exists brings the database into being
    as a side effect of the question — and on a machine that has never migrated,
    every readiness check and every `selfcheck` would leave a file behind. It
    did exactly that in this repository's own data directory during a test run.
    """
    if not _db_path(settings).exists():
        return None
    try:
        conn = _connect(settings)
    except Exception:  # noqa: BLE001 — no store means no migration in flight
        return None
    try:
        row = conn.execute(
            "SELECT id FROM relayout_plan WHERE status=? ORDER BY id DESC LIMIT 1",
            (PLAN_RUNNING,)).fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def progress(settings, plan_id: Optional[int] = None) -> Optional[Progress]:
    """Per-unit state, for reporting and for deciding what is left to do."""
    if not _db_path(settings).exists():
        return None
    conn = _connect(settings)
    try:
        if plan_id is None:
            row = conn.execute(
                "SELECT id FROM relayout_plan ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return None
            plan_id = int(row[0])
        plan = conn.execute("SELECT status FROM relayout_plan WHERE id=?",
                            (plan_id,)).fetchone()
        if not plan:
            return None

        result = Progress(plan_id=plan_id, status=str(plan[0]))
        for kind, unit_id, status, error in conn.execute(
                "SELECT kind, unit_id, status, error FROM relayout_unit"
                " WHERE plan_id=?", (plan_id,)):
            result.total += 1
            if status == STATUS_DONE:
                result.done += 1
            elif status == STATUS_FAILED:
                result.failed += 1
                result.failures.append((kind, unit_id, error or "unknown"))
            elif status == STATUS_BLOCKED:
                result.blocked += 1
                if error and not result.blocked_reason:
                    result.blocked_reason = error
            else:
                result.pending += 1
        return result
    finally:
        conn.close()


def units_to_do(settings, plan_id: int) -> list[Unit]:
    """What may be attempted right now. Completed work is never repeated.

    Bounded, where it used to be unbounded. This returned everything that was
    not ``done`` — which includes ``failed`` — and ``run_pending`` is called on
    EVERY service start, so a unit that failed was re-attempted forever. Each
    attempt is a FULL re-index of that project: the whole tree re-scanned, every
    file re-chunked and re-embedded from zero. With Task Scheduler restarting
    the service on failure, that is the observed CPU loop, and nothing in it
    ever converged.

    So a unit is offered only while it has attempts left and its backoff has
    elapsed. ``blocked`` units are always offered — nothing about them failed,
    and the caller has just proven storage is reachable again.
    """
    now = time.time()
    conn = _connect(settings)
    try:
        rows = conn.execute(
            "SELECT kind, unit_id, points_before, status, attempts, next_attempt"
            " FROM relayout_unit WHERE plan_id=? AND status!=?"
            " ORDER BY kind, unit_id",
            (plan_id, STATUS_DONE)).fetchall()
    finally:
        conn.close()

    ready: list[Unit] = []
    for kind, unit_id, points, status, attempts, next_attempt in rows:
        if status != STATUS_BLOCKED:
            if int(attempts or 0) >= MAX_ATTEMPTS:
                continue
            if next_attempt and float(next_attempt) > now:
                continue
        ready.append(Unit(str(kind), str(unit_id), int(points)))
    return ready


def exhausted_units(settings, plan_id: int) -> list[tuple[str, str, int]]:
    """Units that used up their automatic attempts. For reporting, not retrying."""
    conn = _connect(settings)
    try:
        rows = conn.execute(
            "SELECT kind, unit_id, attempts FROM relayout_unit"
            " WHERE plan_id=? AND status!=? AND attempts>=?",
            (plan_id, STATUS_DONE, MAX_ATTEMPTS)).fetchall()
        return [(str(k), str(u), int(a or 0)) for k, u, a in rows]
    finally:
        conn.close()


def reset_attempts(settings, plan_id: int) -> int:
    """Give every unfinished unit a fresh attempt budget. Returns how many.

    The human-driven retry path. Automatic retries are bounded precisely so a
    machine cannot loop; an operator who has fixed the cause is a different
    thing entirely, and only they know the cause was fixed.
    """
    conn = _connect(settings)
    try:
        with conn:
            cursor = conn.execute(
                "UPDATE relayout_unit SET attempts=0, next_attempt=NULL"
                " WHERE plan_id=? AND status!=?", (plan_id, STATUS_DONE))
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def mark(settings, plan_id: int, unit: Unit, status: str, *,
         points_after: int = 0, error: str = "", count_attempt: bool = False) -> None:
    """Record one unit's outcome. Committed immediately — a crash one unit later
    must not lose the unit that just succeeded.

    ``count_attempt`` is what bounds the retry. It is deliberately NOT set for
    ``blocked``: an unreachable backend is not the unit's failure, and charging
    it an attempt would exhaust the budget of every project on a machine whose
    engine was simply down.
    """
    conn = _connect(settings)
    try:
        with conn:
            if count_attempt:
                row = conn.execute(
                    "SELECT attempts FROM relayout_unit"
                    " WHERE plan_id=? AND kind=? AND unit_id=?",
                    (plan_id, unit.kind, unit.unit_id)).fetchone()
                attempts = int((row[0] if row else 0) or 0) + 1
                delay = _BACKOFF_SECONDS[min(attempts, len(_BACKOFF_SECONDS)) - 1]
                conn.execute(
                    "UPDATE relayout_unit SET status=?, points_after=?, error=?,"
                    " updated_at=?, attempts=?, next_attempt=?"
                    " WHERE plan_id=? AND kind=? AND unit_id=?",
                    (status, points_after, error or None, time.time(), attempts,
                     time.time() + delay, plan_id, unit.kind, unit.unit_id))
            else:
                conn.execute(
                    "UPDATE relayout_unit SET status=?, points_after=?, error=?,"
                    " updated_at=? WHERE plan_id=? AND kind=? AND unit_id=?",
                    (status, points_after, error or None, time.time(), plan_id,
                     unit.kind, unit.unit_id))
    finally:
        conn.close()


@dataclass(frozen=True)
class StorageReadiness:
    """Whether the backend can be written to at all, decided before any work."""

    ok: bool
    reason: str = ""


def preflight(owner) -> StorageReadiness:
    """Can we write to storage? Asked BEFORE anything expensive happens.

    The missing check. A rebuild used to scan a project's whole tree, chunk it,
    embed every file, and only then discover the engine was unreachable —
    throwing all of it away, then doing it again on the next start. One cheap
    round-trip answers the question first.
    """
    try:
        owner._client.get_collections()
        return StorageReadiness(True)
    except Exception as exc:  # noqa: BLE001 — any refusal is the same answer
        return StorageReadiness(False, f"{type(exc).__name__}: {exc}")


def block_all(settings, plan_id: int, reason: str) -> int:
    """Park every unfinished unit as ``blocked``. Returns how many."""
    conn = _connect(settings)
    try:
        with conn:
            cursor = conn.execute(
                "UPDATE relayout_unit SET status=?, error=?, updated_at=?"
                " WHERE plan_id=? AND status!=?",
                (STATUS_BLOCKED, reason, time.time(), plan_id, STATUS_DONE))
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def validate(owner, settings, plan_id: int) -> tuple[bool, list[str]]:
    """Is every unit genuinely present in the NEW layout?

    "The indexer reported success" is a weaker claim than "the collection holds
    points", and this is the last moment before the old storage is deleted — so
    the stronger claim is the one that gates it.
    """
    problems: list[str] = []
    state = progress(settings, plan_id)
    if state is None:
        return False, ["no plan to validate"]

    if state.pending or state.failed or state.blocked:
        problems.append(
            f"{state.pending} unit(s) not attempted, {state.failed} failed, "
            f"{state.blocked} blocked")

    conn = _connect(settings)
    try:
        rows = conn.execute(
            "SELECT kind, unit_id, points_before, points_after FROM relayout_unit"
            " WHERE plan_id=? AND status=?", (plan_id, STATUS_DONE)).fetchall()
    finally:
        conn.close()

    for kind, unit_id, before, after in rows:
        if int(after) == POINTS_UNKNOWN:
            # Not counted. That is a reason to keep the old index, and NOT a
            # reason to claim the rebuild produced nothing.
            problems.append(
                f"{kind} {unit_id}: rebuilt, but its points could not be counted")
            continue
        if int(before) > 0 and int(after) == 0:
            problems.append(
                f"{kind} {unit_id}: held {before} points before the migration and "
                f"none after")
    return (not problems), problems


def units_all_done(settings, plan_id: int) -> bool:
    """Has every unit been rebuilt? Deliberately separate from :func:`validate`.

    Two different decisions were gated on one answer, and they have very
    different costs:

    * **Is the rebuild finished?** — decides whether searches work again. Being
      wrong here disables the product.
    * **Is the new index verified?** — decides whether the OLD index may be
      deleted. Being wrong here destroys data.

    Collapsing them meant a failed point COUNT — a diagnostic — held searches
    off forever on a machine whose index was complete and correct. They are
    separated so the conservative answer on the destructive question can no
    longer take the product down with it.
    """
    report = progress(settings, plan_id)
    return bool(report and not (report.pending or report.failed or report.blocked))


def finalize(settings, plan_id: int) -> None:
    """Mark the plan complete. Only ever called after `validate` passes."""
    conn = _connect(settings)
    try:
        with conn:
            conn.execute(
                "UPDATE relayout_plan SET status=?, finished_at=? WHERE id=?",
                (PLAN_COMPLETE, time.time(), plan_id))
    finally:
        conn.close()
    logger.info("relayout plan %s complete", plan_id)


def owned_collections(owner) -> set[str]:
    """Every collection THIS installation can prove it created.

    The allow-list. Three sources, all of them records we wrote ourselves: the
    configured shared collection (the v2 index this installation built), every
    project collection in our registry including archived ones, and every
    framework corpus in our framework registry.
    """
    owned: set[str] = set()
    try:
        owned.add(owner.settings.collection_name)
    except Exception:  # noqa: BLE001
        pass
    try:
        owned |= set(owner.router.all_collections())
    except Exception:  # noqa: BLE001
        pass
    try:
        frameworks = getattr(owner, "_frameworks", None)
        if frameworks is not None:
            for record in frameworks.all_frameworks():
                name = record.get("collection_name") if isinstance(record, dict) else None
                if name:
                    owned.add(str(name))
    except Exception:  # noqa: BLE001 — no framework registry in shared mode
        pass
    return owned


def obsolete_collections(owner) -> list[str]:
    """Collections THIS installation created that the CURRENT layout no longer uses.

    An ALLOW-list, and the distinction is the whole point. This used to compute
    ``existing - current`` — every collection on the server that our registry did
    not recognise. On a shared engine, that is *the other installation's entire
    index*, and :func:`_retire_old_storage` deletes what this returns. The
    v3.1.0 incident put two services on one engine; only the fact that
    validation never passed stopped the canonical index from being destroyed.
    The safety came from an unrelated bug.

    So: delete only what we can prove we made. "I do not recognise it" is never
    a reason to delete — see :func:`unattributed_collections`, which reports
    those instead.
    """
    try:
        current = set(owner.router.all_collections())
        existing = {c.name for c in owner._client.get_collections().collections}
    except Exception:  # noqa: BLE001
        return []
    return sorted((owned_collections(owner) & existing) - current)


def unattributed_collections(owner) -> list[str]:
    """Collections on this engine that this installation cannot account for.

    Reported, never deleted. A ``proj_<uuid>`` we no longer have a registry row
    for is indistinguishable from another installation's live project, and
    guessing wrong destroys somebody's index. Naming them lets an operator
    decide; deleting them decides for everyone.
    """
    try:
        existing = {c.name for c in owner._client.get_collections().collections}
    except Exception:  # noqa: BLE001
        return []
    return sorted(existing - owned_collections(owner))


def run_pending(owner, settings, *, plan_id: Optional[int] = None,
                progress_cb=None, reset: bool = False) -> Progress:
    """Rebuild every unit that is not already done, then validate and finish.

    Idempotent and resumable: it asks the plan what is left rather than assuming
    it starts from the beginning, so a restart halfway through an eight-hour
    migration continues instead of repeating.

    One unit failing does not stop the others. A migration that aborts on the
    first bad project leaves every later project unindexed AND unreported, which
    is strictly worse than finishing the ones that can finish and naming the one
    that could not.
    """
    plan_id = plan_id if plan_id is not None else active_plan(settings)
    if plan_id is None:
        return Progress(plan_id=-1, status=PLAN_COMPLETE)

    if reset:
        reset_attempts(settings, plan_id)

    # PREFLIGHT BEFORE ANY EXPENSIVE WORK.
    #
    # This is the ordering the CPU loop was missing. A rebuild scans a whole
    # project tree, chunks it and embeds every file before its first write; with
    # the engine unreachable, all of that was discarded and then repeated on the
    # next service start, forever. One cheap round-trip answers the question
    # first, and a blocked plan costs nothing until storage returns.
    ready = preflight(owner)
    if not ready.ok:
        parked = block_all(settings, plan_id, ready.reason)
        logger.error(
            "relayout plan %s BLOCKED: storage is unreachable (%s). %d unit(s) "
            "parked; no re-indexing will be attempted until it returns. Resume "
            "with `rag upgrade --resume`.", plan_id, ready.reason, parked)
        return progress(settings, plan_id) or Progress(plan_id=plan_id,
                                                       status=PLAN_RUNNING)

    todo = units_to_do(settings, plan_id)
    exhausted = exhausted_units(settings, plan_id)
    logger.info("relayout plan %s: %d unit(s) to rebuild", plan_id, len(todo))
    if exhausted:
        # Never silently capped: a bounded retry that says nothing is
        # indistinguishable from one that finished.
        logger.warning(
            "relayout plan %s: %d unit(s) have used their %d automatic attempts "
            "and will not be retried on their own — %s. Run `rag upgrade "
            "--resume` once the cause is fixed.",
            plan_id, len(exhausted), MAX_ATTEMPTS,
            "; ".join(f"{k} {u}" for k, u, _ in exhausted[:5]))

    for unit in todo:
        if progress_cb:
            try:
                progress_cb(unit, "start")
            except Exception:  # noqa: BLE001 — reporting must not break the work
                pass
        try:
            if unit.kind == KIND_PROJECT:
                stats = owner.run_full_index(project_id=unit.unit_id)
                # A SKIPPED RUN IS NOT A COMPLETED ONE.
                #
                # `run_full_index` takes the index mutex NON-blocking and, when
                # another run holds it, returns `{"busy": True}` without raising
                # — a watcher tick is enough to cause that. This return value
                # used to be discarded, so the unit was recorded DONE having
                # been indexed zero times. Once `units_all_done` was separated
                # from `validate`, that stopped merely stalling the plan and
                # started FINISHING it: the migration completes, search comes
                # back on, and the missing project answers "no matches" in the
                # ordinary reassuring shape — the exact outcome this module
                # exists to prevent.
                if isinstance(stats, dict) and stats.get("busy"):
                    logger.warning(
                        "relayout: %s %s was SKIPPED (another indexing run holds "
                        "the mutex); leaving it unfinished for the next pass "
                        "rather than recording it as rebuilt",
                        unit.kind, unit.unit_id)
                    continue
                after = _points_for_project(owner, unit.unit_id)
            else:
                owner.sync_frameworks(refresh=True)
                after = 0
            mark(settings, plan_id, unit, STATUS_DONE, points_after=after)
            logger.info("relayout: %s %s rebuilt (%s points)",
                        unit.kind, unit.unit_id, after)
        except Exception as exc:  # noqa: BLE001 — record and carry on
            # Re-check storage before blaming the unit. A backend that died
            # mid-run makes every remaining unit fail for a reason that has
            # nothing to do with any of them, and charging them each an attempt
            # exhausts the whole plan's budget over one outage.
            still = preflight(owner)
            if not still.ok:
                parked = block_all(settings, plan_id, still.reason)
                logger.error(
                    "relayout plan %s BLOCKED mid-run: storage became "
                    "unreachable (%s); %d unit(s) parked.",
                    plan_id, still.reason, parked)
                return progress(settings, plan_id) or Progress(
                    plan_id=plan_id, status=PLAN_RUNNING)
            mark(settings, plan_id, unit, STATUS_FAILED,
                 error=f"{type(exc).__name__}: {exc}", count_attempt=True)
            logger.warning("relayout: %s %s FAILED: %s", unit.kind, unit.unit_id, exc)
        if progress_cb:
            try:
                progress_cb(unit, "done")
            except Exception:  # noqa: BLE001
                pass

    # TWO decisions, taken separately — see `units_all_done`.
    verified, problems = validate(owner, settings, plan_id)
    if units_all_done(settings, plan_id):
        if verified:
            _retire_old_storage(owner)
        else:
            # The rebuild finished; something about it could not be confirmed.
            # Keep the previous index and say so — but do NOT hold the product
            # hostage to a diagnostic.
            for problem in problems:
                logger.warning("relayout finished with an unverified unit: %s",
                               problem)
            logger.warning(
                "relayout: the previous index has been KEPT because the rebuild "
                "could not be fully verified. Reclaim it with `rag storage "
                "reclaim` once you are satisfied.")
        finalize(settings, plan_id)
    else:
        for problem in problems:
            logger.warning("relayout not complete: %s", problem)

    return progress(settings, plan_id) or Progress(plan_id=plan_id, status=PLAN_RUNNING)


#: Recorded when a unit rebuilt but its points could not be counted. Distinct
#: from 0, which means "counted, and there really are none".
POINTS_UNKNOWN = -1


def _points_for_project(owner, project_id: str) -> int:
    """How many points the rebuilt project holds, or ``POINTS_UNKNOWN``.

    This called ``owner.router.collection_for(project_id)``. **There is no such
    method on CollectionRouter** — it is ``write_collection`` — so every call
    raised ``AttributeError``, the bare ``except`` swallowed it, and every unit
    on every machine recorded 0 points.

    The consequences were not subtle. ``validate`` sees "held N points before
    and none after" for every project, refuses, ``finalize`` never runs, the
    plan stays ``running`` forever, and ``guard_ready`` therefore raises on
    EVERY search. A v3.1.0 machine that had rebuilt its index perfectly — 15
    collections, 147,105 points, all present and correct — reported
    `migration/reindex in progress` and refused to answer anything, permanently.

    Returning 0 for "I could not count" is what made a programming error look
    exactly like total data loss. It now says which it is, and says so loudly:
    a count that fails is a bug worth seeing, not a number worth inventing.
    """
    def unknown(reason: str) -> int:
        logger.warning(
            "relayout: could not count points for project %s (%s); recording the "
            "rebuild as UNVERIFIED rather than as empty", project_id, reason)
        return POINTS_UNKNOWN

    try:
        name = owner.router.write_collection(project_id)
        count = int(owner._count_points(name))
    except Exception as exc:  # noqa: BLE001
        return unknown(f"{type(exc).__name__}: {exc}")

    if count:
        return count

    # A ZERO HAS TO BE CORROBORATED BEFORE IT IS BELIEVED.
    #
    # `_count_points` swallows its own failures and returns 0 — correct for the
    # status display it was written for, where a missing collection genuinely is
    # zero. Here it is not: a 0 that means "could not ask" and a 0 that means
    # "empty" lead to opposite decisions, and taking the wrong one disables
    # search on a correctly rebuilt index. So the collection must demonstrably
    # exist before its emptiness counts as a fact.
    try:
        existing = {c.name for c in owner._client.get_collections().collections}
    except Exception as exc:  # noqa: BLE001
        return unknown(f"the collection list is unavailable ({type(exc).__name__})")
    if name not in existing:
        return unknown(f"collection {name!r} does not exist")
    return 0


def _retire_old_storage(owner) -> None:
    """Delete what the new layout does not use — only after validation passed.

    This is the destructive step, and it is deliberately the LAST one. Deleting
    the old collection before the new one is proven leaves a machine with
    neither.
    """
    for name in obsolete_collections(owner):
        try:
            owner._client.delete_collection(name)
            logger.info("relayout: retired obsolete collection %s", name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("relayout: could not retire %s: %s", name, exc)

    # Named, never deleted. A collection this installation cannot account for is
    # indistinguishable from another installation's live index, and on a shared
    # engine that is exactly what it usually is.
    strangers = unattributed_collections(owner)
    if strangers:
        logger.warning(
            "relayout: %d collection(s) on this engine were not created by this "
            "installation and were LEFT ALONE: %s. If another RAG Tools instance "
            "shares this engine, that is expected; run one canonical service per "
            "machine.", len(strangers), ", ".join(strangers[:10]))
