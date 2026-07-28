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

PLAN_RUNNING = "running"
PLAN_COMPLETE = "complete"

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
    failures: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.status == PLAN_COMPLETE

    @property
    def in_progress(self) -> bool:
        return self.status == PLAN_RUNNING

    def describe(self) -> str:
        if self.complete:
            return f"migration complete ({self.done}/{self.total} units)"
        parts = [f"{self.done}/{self.total} rebuilt"]
        if self.failed:
            parts.append(f"{self.failed} failed")
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
    return conn


# --- capture, which must happen before anything is destroyed --------------


def capture_inventory(owner) -> Inventory:
    """What the CURRENT index holds, as the list of things to rebuild.

    Read from the state DB rather than from Qdrant: the state DB knows which
    project each file belonged to, which is the unit of work, whereas a
    collection only knows points. A project configured but never indexed is
    deliberately included — it is part of the inventory to validate, and
    re-indexing it is cheap.
    """
    from ragtools.indexing.state import IndexState

    units: list[Unit] = []
    total = 0

    try:
        total, _per = owner._collection_points()
    except Exception:  # noqa: BLE001 — a count we cannot take is not a blocker
        total = 0

    seen: set[str] = set()
    try:
        state = IndexState(owner.settings.state_db)
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
    for project in getattr(owner.settings, "enabled_projects", []) or []:
        if project.id not in seen:
            units.append(Unit(KIND_PROJECT, project.id, 0))
            seen.add(project.id)

    # Framework corpora are indexed once and shared; they are units in their own
    # right because a project linking one is not responsible for rebuilding it.
    try:
        frameworks = owner._frameworks
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
    """The running plan, or None. This is what "am I ready?" consults."""
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
            else:
                result.pending += 1
        return result
    finally:
        conn.close()


def units_to_do(settings, plan_id: int) -> list[Unit]:
    """Pending AND failed — retry only what is not finished.

    Completed work is never repeated, which is what makes an eight-hour
    migration survivable across a restart.
    """
    conn = _connect(settings)
    try:
        rows = conn.execute(
            "SELECT kind, unit_id, points_before FROM relayout_unit"
            " WHERE plan_id=? AND status!=? ORDER BY kind, unit_id",
            (plan_id, STATUS_DONE)).fetchall()
        return [Unit(str(k), str(u), int(p)) for k, u, p in rows]
    finally:
        conn.close()


def mark(settings, plan_id: int, unit: Unit, status: str, *,
         points_after: int = 0, error: str = "") -> None:
    """Record one unit's outcome. Committed immediately — a crash one unit later
    must not lose the unit that just succeeded."""
    conn = _connect(settings)
    try:
        with conn:
            conn.execute(
                "UPDATE relayout_unit SET status=?, points_after=?, error=?,"
                " updated_at=? WHERE plan_id=? AND kind=? AND unit_id=?",
                (status, points_after, error or None, time.time(), plan_id,
                 unit.kind, unit.unit_id))
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

    if state.pending or state.failed:
        problems.append(
            f"{state.pending} unit(s) not attempted, {state.failed} failed")

    conn = _connect(settings)
    try:
        rows = conn.execute(
            "SELECT kind, unit_id, points_before, points_after FROM relayout_unit"
            " WHERE plan_id=? AND status=?", (plan_id, STATUS_DONE)).fetchall()
    finally:
        conn.close()

    for kind, unit_id, before, after in rows:
        if int(before) > 0 and int(after) == 0:
            problems.append(
                f"{kind} {unit_id}: held {before} points before the migration and "
                f"none after")
    return (not problems), problems


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


def obsolete_collections(owner) -> list[str]:
    """Collections the CURRENT layout does not use — the old shared index.

    Computed from the router rather than by name-matching, so it stays correct
    if the layout gains a third form.
    """
    try:
        current = set(owner.router.all_collections())
        existing = {c.name for c in owner._client.get_collections().collections}
    except Exception:  # noqa: BLE001
        return []
    return sorted(existing - current)
