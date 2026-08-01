"""One gate in front of every operation that can destroy an index.

v3.2.0 had four independent doors into ``owner.rebuild()`` — the UI fragment, the
JSON API, the CLI and the job worker — and not one of them asked whether the
operation could succeed. The service already *knew* storage was down; ``/health``
was reporting ``storage_unreachable`` at that moment. The rebuild took a backup,
started dropping collections, and surfaced the refusal as ``HTTP 500``.

The rule this module enforces: **a destructive operation must prove its
preconditions before it mutates anything at all** — before the backup, which was
previously the first thing to happen.

It is deliberately ONE function consulted by every caller rather than a check
copied four times. Four copies is how three of them stay correct.

Refusal is a *conflict*, not an error: the request was well-formed and the server
is temporarily unable to honour it. So HTTP gets 409, the CLI gets a categorised
exit code, MCP gets a structured error, and the UI gets a sentence a person can
act on. None of them gets a traceback.

Authorization (v3.5.1, WP-R09)
------------------------------
The preconditions were only half the gate. ``POST /api/projects/{id}/reindex``
and ``DELETE /api/projects/{id}`` dropped collections with no gate at all, and
``POST /api/shutdown`` had no capability check, no confirmation and no guard:
anything that could reach the port could stop the service or erase a project.

**Local access is not authorization.** "It is on 127.0.0.1" describes the network
path, not who is calling — and the MCP proxy makes a restricted client's calls
arrive on exactly that interface. So every mutating operation is named in
:data:`OPERATIONS` with three facts, and :func:`guarded` enforces all three in
one place for all five surfaces (UI, HTTP API, CLI, MCP direct, MCP proxy):

* **capability** — the tool name whose grant permits it, re-checked server-side
  against the caller's :class:`~ragtools.profiles.ClientProfile`. Writes are
  default-closed: a profile is built from explicitly granted capability groups,
  and a *destructive* operation additionally needs the destructive opt-in, which
  :class:`~ragtools.profiles.ClientProfile` defaults to ``forbidden``.
* **confirmation** — ``confirm == subject`` (the project id, the dependency id,
  the service instance id), the convention already used by the MCP write tools.
  Required whenever the profile's ``destructive_policy`` is ``confirm_token``,
  and unconditionally for operations declaring ``confirm="always"``.
* **preconditions** — the original check: storage reachable, no migration
  holding the index, and for exclusive operations no other index run and no
  other destructive operation.

An operation not in the table raises. An unrecognised operation must never fall
through to "allowed", which is the same rule ``onboarding.policy_for`` follows.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ragtools.service")

#: Written before an irreversible rebuild step and removed after. Its presence at
#: startup means a rebuild was interrupted partway, which is a fact worth
#: surfacing — an empty index with no explanation is how a user concludes the
#: product lost their data.
INTENT_NAME = "rebuild-intent.json"

#: Process-wide. Two destructive operations must never interleave, whatever
#: doors they came through.
_operation_lock = threading.Lock()


class OperationRefused(RuntimeError):
    """A destructive operation cannot run right now, and why.

    Carries a machine-readable ``code`` so each surface can render its own
    idiom without re-deriving the reason from a message string.
    """

    def __init__(self, reason: str, code: str = "unavailable"):
        self.reason = reason
        self.code = code
        super().__init__(reason)


# --- the operation registry ----------------------------------------------


@dataclass(frozen=True)
class Operation:
    """One mutating operation, and everything the gate needs to decide it.

    ``capability`` is a tool name from
    :data:`ragtools.profiles.CAPABILITY_GROUPS` — the same vocabulary the MCP
    tools are authorized against, so a client that may not call
    ``reindex_project`` over MCP cannot reach the same effect over HTTP either.
    """

    name: str
    capability: str
    #: Storage/migration preconditions apply — the operation writes or deletes
    #: vectors, so it cannot succeed against a store that is not there.
    index_mutating: bool = False
    #: Additionally take the process-wide destructive lock and refuse while an
    #: index run is active. For operations that drop collections outright.
    exclusive: bool = False
    #: "never" | "policy" (when the profile's destructive_policy says so) |
    #: "always".
    confirm: str = "never"
    summary: str = ""


def _op(name, capability, **kw) -> Operation:
    return Operation(name=name, capability=capability, **kw)


#: Every mutating operation the product exposes, whatever surface reaches it.
#: The AST test in ``tests/test_destructive_guards.py`` derives the destructive
#: ROUTE HANDLERS from the source and asserts each one is guarded, so a new
#: endpoint cannot quietly appear without an entry here.
OPERATIONS: dict[str, Operation] = {
    # --- rebuild / index -------------------------------------------------
    "rebuild": _op("rebuild", "rebuild_index", index_mutating=True,
                   exclusive=True, confirm="policy",
                   summary="drop every collection and re-index from scratch"),
    "index": _op("index", "run_index", index_mutating=True,
                 summary="index or re-index content"),
    # --- projects --------------------------------------------------------
    "project_create": _op("project_create", "add_project", index_mutating=True,
                          summary="add a project and index it"),
    "project_update": _op("project_update", "update_project", index_mutating=True,
                          summary="edit a project's configuration"),
    "project_delete": _op("project_delete", "delete_project", index_mutating=True,
                          exclusive=True, confirm="policy",
                          summary="remove a project and delete its indexed data"),
    "project_reindex": _op("project_reindex", "reindex_project", index_mutating=True,
                           exclusive=True, confirm="policy",
                           summary="drop and rebuild one project's index"),
    "project_set_mode": _op("project_set_mode", "set_project_mode",
                            index_mutating=True, confirm="policy",
                            summary="change what a project indexes"),
    "project_toggle": _op("project_toggle", "update_project",
                          summary="enable or disable a project"),
    # Config-only. Deliberately NOT index_mutating: an ignore rule is a
    # declaration about future runs, and refusing to edit it because the engine
    # is down would take away the one repair a user can make while it is down.
    "project_ignore_add": _op("project_ignore_add", "add_project_ignore_rule",
                              summary="add an ignore pattern"),
    "project_ignore_remove": _op("project_ignore_remove", "remove_project_ignore_rule",
                                 summary="remove an ignore pattern"),
    # --- shared dependencies --------------------------------------------
    "dependency_create": _op("dependency_create", "add_dependency",
                             summary="register a shared dependency"),
    "dependency_update": _op("dependency_update", "update_dependency",
                             index_mutating=True,
                             summary="re-point or disable a shared dependency"),
    "dependency_delete": _op("dependency_delete", "remove_dependency",
                             index_mutating=True, confirm="policy",
                             summary="delete a shared dependency"),
    "project_dependencies_set": _op("project_dependencies_set",
                                    "set_project_dependencies",
                                    index_mutating=True,
                                    summary="change a project's dependency links"),
    "frameworks_sync": _op("frameworks_sync", "reindex_framework",
                           index_mutating=True,
                           summary="reconcile framework corpora"),
    # --- configuration ---------------------------------------------------
    "update_config": _op("update_config", "update_config",
                         summary="change the service settings"),
    # --- collections -----------------------------------------------------
    "collection_reclaim": _op("collection_reclaim", "delete_collection",
                              index_mutating=True, exclusive=True,
                              confirm="policy",
                              summary="drop collections the layout no longer uses"),
    # --- service ---------------------------------------------------------
    # Not index_mutating on purpose: refusing to STOP the service because its
    # storage is unreachable would make a broken installation unstoppable.
    # `confirm="always"` because there is no undo and no owner dialog behind it —
    # the token is the service's own instance id, which a caller has to read from
    # /identity, so a blind call cannot produce it.
    "service_shutdown": _op("service_shutdown", "shutdown_service",
                            confirm="always",
                            summary="stop the service"),
}


def policy_for(name: str) -> Operation:
    """The :class:`Operation` for ``name``; ``KeyError`` if unknown.

    A ``KeyError`` rather than a permissive default: an operation nobody
    declared is exactly the one nobody reviewed. Named after
    :func:`ragtools.onboarding.policy_for`, which draws the same line for the
    onboarding flow and for the same reason — and not ``operation``, which is
    already the parameter name three functions below use for a *label*.
    """
    return OPERATIONS[name]


# --- who is calling -------------------------------------------------------


def owner_profile():
    """The implicit owner profile — all groups, all projects."""
    from ragtools.integration.mcp_authz import DEFAULT_OWNER_PROFILE

    return DEFAULT_OWNER_PROFILE


#: The MCP proxy stamps this on every forwarded request when it was spawned for
#: a named client (``RAG_CLIENT_PROFILE``). Without it the caller is the owner —
#: the admin panel, the CLI, and a single-owner MCP process all are.
PROFILE_HEADER = "X-Client-Profile"


def request_profile(request):
    """Resolve the :class:`~ragtools.profiles.ClientProfile` behind a request.

    No header → the owner, which is what the admin panel and the CLI are, and
    what a single-owner MCP process is. A header naming a profile the store does
    not know **fails closed** — it never degrades to owner, for the same reason
    ``mcp_authz.resolve_active_profile`` refuses: a named client that silently
    becomes the owner is the worst possible outcome of a typo.

    The store is opened in a ``with``, and that is load-bearing rather than
    tidy. This function is called on EVERY request that carries the header, so
    an unclosed :class:`~ragtools.profile_store.ProfileStore` is one SQLite
    handle on ``profiles.db`` per request. It used to be left to refcounting,
    which works right up until the refusal path: ``raise`` while ``store`` is
    still a live local attaches this frame to the exception's traceback, and the
    handle then survives for as long as anything holds the exception — a
    traceback/frame cycle the CYCLIC collector frees, on no schedule.

    POSIX hides it (an open file can still be unlinked). Windows cannot, so it
    surfaced as ``PermissionError: [WinError 32]`` removing a test's temp
    directory — a teardown ERROR on ``windows-latest``, on a test that passed,
    green on the other two runners.
    """
    if request is None:
        return owner_profile()
    pid = ""
    try:
        pid = (request.headers.get(PROFILE_HEADER) or "").strip()
    except Exception:  # noqa: BLE001 — a header read must never fail a request
        pid = ""
    if not pid:
        return owner_profile()
    if pid == "owner":
        return owner_profile()

    try:
        from pathlib import Path as _P

        from ragtools.profile_store import ProfileStore
        from ragtools.service.app import get_settings

        with ProfileStore(str(_P(get_settings().data_dir) / "profiles.db")) as store:
            profile = store.get(pid)
    except Exception as exc:  # noqa: BLE001
        raise OperationRefused(
            f"the client profile {pid!r} could not be resolved ({exc}); refusing "
            "rather than assuming the owner", code="profile_unknown")
    if profile is None:
        raise OperationRefused(
            f"{pid!r} is not a known client profile; refusing rather than "
            "assuming the owner", code="profile_unknown")
    return profile


# --- capability + confirmation -------------------------------------------


def check_capability(profile, op: Operation) -> None:
    """Raise :class:`OperationRefused` unless ``profile`` may run ``op``.

    Two checks, in the order :mod:`ragtools.authz` established: the capability
    grant, then the destructive modifier. The modifier is what makes deletion
    default-closed — a profile can hold "Manage projects" and still not be able
    to delete one until the owner ticks the destructive box.
    """
    from ragtools.mcp_capabilities import destructive_tools
    from ragtools.profiles import is_tool_allowed

    if not is_tool_allowed(profile, op.capability):
        raise OperationRefused(
            f"client profile {profile.profile_id!r} may not {op.summary or op.name} "
            f"(requires the {op.capability!r} capability)",
            code="capability_denied")

    if op.capability in destructive_tools():
        if getattr(profile, "destructive_policy", "forbidden") == "forbidden":
            raise OperationRefused(
                f"{op.name} is destructive and client profile "
                f"{profile.profile_id!r} has destructive access turned off",
                code="destructive_forbidden")


def confirmation_required(profile, op: Operation) -> bool:
    """Whether ``op`` needs ``confirm == subject`` for this caller.

    ``always`` means always. ``policy`` defers to the profile's
    ``destructive_policy``: ``confirm_token`` is the opt-in an owner grants a
    client, and ``owner_approval`` (the owner's own default) means the person
    clicking the button IS the approval — the panel already asks.
    """
    if op.confirm == "always":
        return True
    if op.confirm != "policy":
        return False
    return getattr(profile, "destructive_policy", "forbidden") == "confirm_token"


def check_confirm(profile, op: Operation, *, subject: str, confirm) -> None:
    """Raise unless the confirmation contract is satisfied for this caller."""
    if not confirmation_required(profile, op):
        return
    if confirm is None or str(confirm) != str(subject):
        raise OperationRefused(
            f"{op.name} requires confirmation: pass confirm={subject!r}. "
            "A blind call cannot produce a token it never read.",
            code="confirm_required")


def blocking_reason(owner, *, operation: str = "rebuild",
                    exclusive: bool = True) -> tuple[str, str]:
    """``(code, reason)`` if ``operation`` must be refused, else ``("", "")``.

    Cheap by construction: it reads cached lifecycle state and one already-cached
    storage probe. A guard that itself blocks on a dead engine is not a guard.

    ``exclusive=False`` keeps the storage and migration checks but drops the
    "another index is running" refusal — an ordinary index run is allowed to
    queue behind another one, and always has been. Only an operation that DROPS
    collections must refuse, because there it would be dropping them out from
    under a live writer.
    """
    # 1. The engine, from cached lifecycle state — never by reaching for it.
    try:
        from ragtools.service.app import storage_is_down

        down = storage_is_down()
        if down:
            return "engine_down", down
    except Exception:  # noqa: BLE001 — no app context (CLI/tests); fall through
        pass

    # 2. The store itself. `storage_reachable` is TTL-cached, so this is free on
    #    any path that has already rendered a status page.
    try:
        ok, detail = owner.storage_reachable()
        if not ok:
            return "storage_unreachable", (
                f"the vector store is not reachable ({detail}). "
                f"{operation} would drop collections it could not rebuild.")
    except Exception as exc:  # noqa: BLE001
        return "storage_unreachable", f"the vector store could not be probed: {exc}"

    # 3. A migration owns the index until it finishes. Rebuilding underneath it
    #    destroys the work it has already done and orphans its plan.
    try:
        from ragtools.upgrade import relayout

        plan = relayout.active_plan(owner.settings)
        if plan is not None:
            report = relayout.progress(owner.settings, plan)
            if report is not None and not report.complete:
                return "migration_active", (
                    f"a layout migration is in progress ({report.describe()}). "
                    f"Wait for it to finish before running {operation}.")
    except Exception:  # noqa: BLE001 — no migration store means no migration
        pass

    # 4. Another indexing run. `rebuild` drops every collection and deletes the
    #    state DB; doing that under a running index leaves two writers, one of
    #    them destructive.
    if exclusive:
        try:
            if owner.indexing:
                return "index_busy", (
                    f"an indexing run is in progress. {operation} would drop the "
                    f"collections it is writing into.")
        except Exception:  # noqa: BLE001
            pass

    return "", ""


def assert_allowed(owner, *, operation: str = "rebuild",
                   exclusive: bool = True) -> None:
    """Raise :class:`OperationRefused` unless ``operation`` may proceed."""
    code, reason = blocking_reason(owner, operation=operation, exclusive=exclusive)
    if code:
        raise OperationRefused(reason, code=code)


@contextmanager
def destructive_operation(owner, *, operation: str = "rebuild"):
    """Hold the destructive lock for the whole operation, or refuse.

    Non-blocking on purpose: a caller who waits behind a half-hour rebuild has
    almost certainly clicked twice, and telling them so is better than queueing
    a second index-destroying operation behind the first.
    """
    if not _operation_lock.acquire(blocking=False):
        raise OperationRefused(
            f"another destructive operation is already running; {operation} was "
            f"not started", code="operation_in_progress")
    try:
        assert_allowed(owner, operation=operation)
        yield
    finally:
        _operation_lock.release()


def authorize(owner, name: str, *, request=None, profile=None,
              subject: str = "", confirm=None):
    """The gate, without holding a lock. Returns the resolved profile.

    Order is the point. Authorization is cheapest and most fundamental, so it
    goes first and a caller who may not do this at all learns so without any
    state being probed. Confirmation next, because a missing token is the
    caller's mistake and does not depend on the server's condition. Preconditions
    last but still **before the body** — the v3.2.0 rebuild took its backup and
    started dropping collections first, and only then discovered it could not
    finish.

    Use this for an operation that mutates but does not DROP: it may run
    alongside an index run, so there is nothing to hold. An operation declared
    ``exclusive`` must use :func:`guarded` instead, which holds the lock for the
    whole body — acquiring and releasing it up front would prove nothing.

    ``owner`` may be ``None`` for operations that are not ``index_mutating``.
    """
    op = policy_for(name)
    resolved = profile if profile is not None else request_profile(request)
    check_capability(resolved, op)
    check_confirm(resolved, op, subject=subject, confirm=confirm)
    if op.index_mutating and not op.exclusive:
        assert_allowed(owner, operation=op.name, exclusive=False)
    return resolved


@contextmanager
def guarded(owner, name: str, *, request=None, profile=None,
            subject: str = "", confirm=None):
    """:func:`authorize`, plus the destructive lock held for the whole body."""
    op = policy_for(name)
    resolved = authorize(owner, name, request=request, profile=profile,
                         subject=subject, confirm=confirm)
    if not op.exclusive:
        yield resolved
        return
    with destructive_operation(owner, operation=op.name):
        yield resolved


# --- interrupted-rebuild marker ------------------------------------------


def _intent_path(settings) -> Path:
    return Path(settings.data_dir) / INTENT_NAME


def record_intent(settings, payload: dict) -> None:
    """Persist what a rebuild is about to do, before it becomes irreversible."""
    try:
        path = _intent_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**payload, "at": time.time()}, indent=2),
                        encoding="utf-8")
    except OSError as exc:
        logger.warning("could not record rebuild intent: %s", exc)


def clear_intent(settings) -> None:
    try:
        _intent_path(settings).unlink(missing_ok=True)
    except OSError:
        pass


def pending_intent(settings) -> Optional[dict]:
    """A rebuild that started and never finished, or None."""
    try:
        path = _intent_path(settings)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
