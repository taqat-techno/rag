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
_BUSY_MAX_WAIT_SECONDS = 900.0


def _count_points(owner, project_id: str | None = None) -> int | None:
    """Count points in the collection, optionally scoped to one project.

    Returns ``None`` when the count cannot be obtained — the caller must then
    treat verification as *unproven* rather than assume success.
    """
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        flt = None
        if project_id:
            flt = Filter(must=[FieldCondition(key="project_id",
                                              match=MatchValue(value=project_id))])
        res = owner._client.count(
            collection_name=owner._settings.collection_name,
            count_filter=flt, exact=True,
        )
        return int(res.count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("point count failed (verification will be unproven): %s", exc)
        return None


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
            if waited >= _BUSY_MAX_WAIT_SECONDS:
                raise RuntimeError(
                    f"index job for {project or 'all projects'} never acquired the "
                    f"index lock ({_BUSY_MAX_WAIT_SECONDS:.0f}s); another indexing "
                    "run is stuck. Nothing was indexed."
                )
            ctx.check_cancel()
            ctx.progress(done=0, total=1, phase="waiting for the running index")
            time.sleep(_BUSY_RETRY_SECONDS)
            waited += _BUSY_RETRY_SECONDS

        if isinstance(stats.get("projects"), set):
            stats["projects"] = sorted(stats["projects"])
        return stats

    def rebuild_handler(job, ctx):
        owner = get_owner()
        ctx.progress(done=0, total=1, phase="rebuild")
        before = _count_points(owner)
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
        points = owner.get_map_points(force_recompute=True)
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
