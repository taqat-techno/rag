"""Job handlers — the long operations, run under the job engine (Phase 2 / W2).

Each handler receives ``(job, ctx)`` and returns a result dict. ``ctx.progress``
is throttled; raising from it (or from ``ctx.check_cancel()``) cancels at a safe
boundary.

The purge handler is the important one: it **verifies** its effect by counting
points before and after. Today `owner.delete_project_data` catches the Qdrant
failure, logs a *success*-shaped activity line, and returns a count of state-DB
rows rather than vectors — so a failed purge is reported as done while the
vectors remain searchable.

Plan: docs/planning/RAG_MODERNIZATION_MASTER_PLAN.md  (Phase 2 -> G2, J-6)
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("ragtools.jobs")

#: How long an index job waits for the index mutex before giving up. The usual
#: holder is the watcher reacting to the same edit that queued this job, so the
#: wait is short in practice; the ceiling exists so a genuinely stuck indexer
#: surfaces as a failed job instead of a thread parked forever.
_BUSY_RETRY_SECONDS = 5.0

#: Give up on SILENCE, not on elapsed time.
#:
#: This was a flat 900 s ceiling, and the message on hitting it asserted that
#: "another indexing run is stuck". It had no evidence for that. A first index
#: of a large corpus legitimately runs for hours — during the 3.0.0 startup sync
#: of 25 projects a user's `rag index` waited out the ceiling and exited 1
#: declaring a perfectly healthy run stuck, while that run went on to finish.
#:
#: Elapsed time cannot distinguish slow from dead; a heartbeat can. The holder
#: reports progress through `QdrantOwner._beat` at every file and every upsert
#: batch, so a run that has said nothing for this long is genuinely wedged, and
#: one that is still ticking is worth waiting for however long it takes.
#:
#: Same magnitude as the ceiling it replaces, deliberately. Ticks are frequent
#: once indexing starts, but the scan that precedes them is one call over the
#: whole corpus and reports nothing while it walks 37,000 files. Measuring
#: SILENCE rather than total elapsed time is the fix; choosing a tighter
#: threshold on top of it would trade a false "stuck" on a slow run for a false
#: "stalled" on a slow scan. At this value the rule is strictly more permissive
#: than v3.0.0 in every case and never less.
_STALL_SECONDS = 900.0

#: Fallback ceiling for the case where the holder publishes no heartbeat at all
#: — an older owner, or a lock held by something outside the index path. Without
#: a liveness signal there is nothing to distinguish slow from dead, so the old
#: time-based rule is all that is left. The message says which rule fired.
_BLIND_MAX_WAIT_SECONDS = 900.0


def _count_points(owner, project_id: str | None = None) -> int | None:
    """Count points for a project, or across the whole index.

    Returns ``None`` when the count cannot be obtained — the caller must then
    treat verification as *unproven* rather than assume success.

    Routed, not hard-coded. This counted ``settings.collection_name`` with a
    ``project_id`` payload filter — the v2 question. Under ``per_project`` that
    collection does not exist, so the count raised, every rebuild and reindex
    reported ``verified=False`` forever, and ``purge_handler`` raised
    "the vectors may still be present" on drops that had in fact succeeded.
    """
    try:
        router = owner._router
        if project_id:
            targets = [router.write_collection(project_id)]
        else:
            targets = list(router.all_collections())

        total = 0
        for name in targets:
            count = owner._count_points(name)
            if count is None:
                # One unknown makes the total unknown. A partial sum would be
                # indistinguishable from a real one.
                logger.warning(
                    "point count unavailable for %s (verification unproven)", name)
                return None
            total += count
        return total
    except Exception as exc:  # noqa: BLE001
        logger.warning("point count failed (verification will be unproven): %s", exc)
        return None


def _index_activity(owner) -> dict | None:
    """The running index's heartbeat, or ``None`` if it publishes none.

    Tolerant of an owner without the method so a stub or an older object in a
    test double does not turn a wait into an AttributeError.
    """
    probe = getattr(owner, "index_activity", None)
    if probe is None:
        return None
    try:
        return probe()
    except Exception:  # noqa: BLE001 — liveness is advisory; never fatal
        return None


def _describe(active: dict) -> str:
    """The running index in one clause, for a human reading a job error."""
    what = active.get("what") or "an indexing run"
    done, total = active.get("done") or 0, active.get("total") or 0
    where = f"{done}/{total}" if total else f"{done}"
    return f"{what} ({active.get('phase') or 'working'}, {where})"


def _waiting_phase(active: dict | None) -> str:
    """Progress text while queued. Names the holder so the UI shows a live run
    rather than an unexplained pause."""
    if active is None:
        return "waiting for the running index"
    return f"waiting for {_describe(active)}"


def _refuse_if_wedged(active: dict | None, waited: float, project) -> None:
    """Fail the job only when the holder is genuinely not moving.

    Two rules, and the error says which one fired — because "I waited a fixed
    period" and "the other run stopped responding" are different claims, and
    v3.0.0 made the second one on the evidence of the first.
    """
    scope = project or "all projects"

    if active is None:
        # No heartbeat to read: fall back to elapsed time, and claim only that.
        if waited >= _BLIND_MAX_WAIT_SECONDS:
            raise RuntimeError(
                f"index job for {scope} did not acquire the index lock within "
                f"{_BLIND_MAX_WAIT_SECONDS:.0f}s, and the run holding it reports "
                "no progress information, so it cannot be told apart from a "
                "stalled one. Nothing was indexed."
            )
        return

    age = active.get("age") or 0.0
    if age >= _STALL_SECONDS:
        raise RuntimeError(
            f"index job for {scope} was not started: {_describe(active)} holds "
            f"the index lock and has reported no progress for {age:.0f}s, so it "
            "appears stalled. Nothing was indexed."
        )


def make_handlers(get_owner) -> dict:
    """Build the kind -> handler map. ``get_owner`` is injected for testability."""

    def index_handler(job, ctx):
        owner = get_owner()
        project = job.scope.get("project")
        full = bool(job.scope.get("full"))

        def progress(done, total, phase):
            ctx.check_cancel()
            ctx.progress(done=done, total=total, phase=phase)

        # An index run refuses to start while another holds the index mutex and
        # returns a `busy` no-op. For the watcher that is right — don't pile up
        # runs. For a JOB it is not: the job exists to make a specific
        # correction, and recording zeros as "succeeded" loses that correction
        # with no signal anywhere. It bit exactly where it hurts most — the
        # reindex that restores a project's files after a dependency is
        # un-declared ran while the watcher (restarted by the same config write)
        # held the lock, so the files stayed missing from search and the job log
        # said success. There is no retry layer underneath, so wait here.
        waited = 0.0
        while True:
            if full:
                stats = owner.run_full_index(project_id=project, progress=progress)
            else:
                # Incremental gets the same callback. A routine incremental is
                # instant, but one forced to re-index everything (storage or
                # layout change) runs for many minutes — and with no progress
                # the UI showed 0/None throughout, which reads as a hung job.
                stats = owner.run_incremental_index(project_id=project, progress=progress)
            if not stats.get("busy"):
                break

            active = _index_activity(owner)
            _refuse_if_wedged(active, waited, project)
            ctx.check_cancel()
            ctx.progress(done=0, total=1, phase=_waiting_phase(active))
            time.sleep(_BUSY_RETRY_SECONDS)
            waited += _BUSY_RETRY_SECONDS

        if isinstance(stats.get("projects"), set):
            stats["projects"] = sorted(stats["projects"])
        return stats

    def rebuild_handler(job, ctx):
        from ragtools.service import destructive

        owner = get_owner()
        ctx.progress(done=0, total=1, phase="rebuild")
        before = _count_points(owner)
        # Same gate as every other door. A queued rebuild that runs hours later
        # must re-check its preconditions at the moment it runs, not at the
        # moment it was enqueued — storage state is not a property of the queue.
        with destructive.destructive_operation(owner, operation="rebuild"):
            stats = owner.rebuild()
        after = _count_points(owner)
        if isinstance(stats.get("projects"), set):
            stats["projects"] = sorted(stats["projects"])
        # A rebuild is verified when we can see the collection repopulated to the
        # count the run itself reported.
        ctx.verified = (after is not None and after >= int(stats.get("chunks_indexed", 0) or 0))
        return {**stats, "points_before": before, "points_after": after}

    def purge_handler(job, ctx):
        """Delete a project's indexed data — and PROVE it.

        Verification is the point: success is reported only when the project's
        point count actually reached zero.
        """
        owner = get_owner()
        project = job.scope["project"]
        ctx.progress(done=0, total=1, phase="purge")

        before = _count_points(owner, project)
        result = owner.delete_project_data(project)
        after = _count_points(owner, project)

        ctx.verified = (after == 0)
        payload = {
            "project": project,
            "files_deleted": result.get("files_deleted", 0),
            "points_before": before,
            "points_after": after,
        }
        if after is None:
            raise RuntimeError(
                f"purge of {project!r} could not be verified — the point count "
                "was unavailable; the vectors may still be present"
            )
        if after != 0:
            raise RuntimeError(
                f"purge of {project!r} did NOT remove all vectors: "
                f"{after} points remain (was {before})"
            )
        return payload

    def reindex_handler(job, ctx):
        owner = get_owner()
        project = job.scope["project"]
        ctx.progress(done=0, total=1, phase="reindex")
        stats = owner.reindex_project(project)
        if isinstance(stats.get("projects"), set):
            stats["projects"] = sorted(stats["projects"])
        after = _count_points(owner, project)
        ctx.verified = after is not None
        return {**stats, "points_after": after}

    def map_recompute_handler(job, ctx):
        """Refresh the semantic map off the request path."""
        owner = get_owner()
        ctx.progress(done=0, total=1, phase="map")
        result = owner.get_map_points(force_recompute=True)
        points = result.get("points", []) if isinstance(result, dict) else result
        return {"points": len(points)}

    def sync_frameworks_handler(job, ctx):
        """Index and link the framework corpora projects declare.

        Runs under the job engine rather than inline because a declared
        dependency can be a 38k-file framework core: indexing it is minutes of
        work that must not block the request that declared it, and it needs the
        durable record, progress and cancellation every other long op gets.

        Idempotent — re-running re-uses already-registered corpora (``created``
        false) and re-purges nothing, so a retry after a crash is safe.
        """
        owner = get_owner()

        def progress(done, total, phase):
            ctx.check_cancel()
            ctx.progress(done=done, total=total, phase=phase)

        ctx.progress(done=0, total=1, phase="frameworks")
        synced = owner.sync_frameworks(progress=progress)
        # Verified when every declared root ended up in a collection: the
        # link is what makes the corpus reachable from the project's search.
        ctx.verified = all(entry.get("collection") for entry in synced)
        return {
            "linked": len(synced),
            "collections": sorted({e["collection"] for e in synced}),
            "files_indexed": sum(e.get("files_indexed", 0) for e in synced),
            "chunks_indexed": sum(e.get("chunks_indexed", 0) for e in synced),
            "purged_from_projects": sum(e.get("purged_from_project", 0) for e in synced),
            "entries": synced,
        }

    return {
        "map_recompute": map_recompute_handler,
        "index": index_handler,
        "rebuild": rebuild_handler,
        "purge": purge_handler,
        "reindex": reindex_handler,
        "sync_frameworks": sync_frameworks_handler,
    }
