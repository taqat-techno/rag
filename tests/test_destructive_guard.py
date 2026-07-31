"""A destructive operation must prove it can succeed before it destroys anything.

On the incident machine the user clicked *Rebuild the knowledge base* while
`/health` was already reporting `storage_unreachable`. The service took a backup,
started dropping collections, hit a refused connection and returned
`HTTP 500 — Internal Server Error`. Every fact needed to decline politely was
already in hand.

Four separate doors reached `owner.rebuild()` and not one of them asked.
"""

from __future__ import annotations

import ast
import types
from pathlib import Path

import pytest

from ragtools.service import destructive

SRC = Path(__file__).resolve().parent.parent / "src" / "ragtools"


class FakeOwner:
    def __init__(self, *, reachable=True, detail="", indexing=False, plan=None):
        self._reachable = reachable
        self._detail = detail
        self.indexing = indexing
        self.settings = types.SimpleNamespace(data_dir="", state_db="")
        self._plan = plan

    def storage_reachable(self):
        return self._reachable, self._detail


# --- the guard ------------------------------------------------------------


def test_an_unreachable_store_blocks_a_rebuild():
    code, reason = destructive.blocking_reason(
        FakeOwner(reachable=False, detail="WinError 10061"))
    assert code == "storage_unreachable"
    assert "10061" in reason


def test_a_running_index_blocks_a_rebuild():
    code, reason = destructive.blocking_reason(FakeOwner(indexing=True))
    assert code == "index_busy"
    assert "indexing run" in reason


def test_a_healthy_owner_is_allowed():
    code, reason = destructive.blocking_reason(FakeOwner())
    assert code == "" and reason == ""


def test_a_refusal_carries_a_machine_readable_code():
    with pytest.raises(destructive.OperationRefused) as excinfo:
        destructive.assert_allowed(FakeOwner(reachable=False, detail="down"))
    assert excinfo.value.code == "storage_unreachable"
    assert excinfo.value.reason


def test_two_destructive_operations_cannot_interleave():
    owner = FakeOwner()
    with destructive.destructive_operation(owner):
        with pytest.raises(destructive.OperationRefused) as excinfo:
            with destructive.destructive_operation(owner):
                pass
    assert excinfo.value.code == "operation_in_progress"


def test_the_lock_is_released_even_when_the_body_raises():
    owner = FakeOwner()
    with pytest.raises(ValueError):
        with destructive.destructive_operation(owner):
            raise ValueError("boom")
    # If the lock leaked, this would refuse.
    with destructive.destructive_operation(owner):
        pass


def test_the_guard_refuses_before_the_body_runs():
    """The precondition is checked BEFORE anything happens — the backup used to
    be the first thing that ran."""
    ran = []
    with pytest.raises(destructive.OperationRefused):
        with destructive.destructive_operation(
                FakeOwner(reachable=False, detail="down")):
            ran.append(1)
    assert ran == [], "the operation body ran despite a refusal"


def test_an_embedded_fallback_is_not_reported_as_storage_down():
    """A degraded-to-embedded machine has perfectly good storage.

    The lifecycle object survives a failed managed start in state `stopped`,
    which is a DOWN state — so keeping it would make `storage_is_down()` refuse
    every rebuild on a machine whose store is fine. `None` is the truth after a
    fallback, and the lifespan must clear it.
    """
    import ragtools.service.app as app_mod

    source = Path(app_mod.__file__).read_text(encoding="utf-8")
    idx = source.index("Storage degraded to embedded")
    branch = source[idx - 900:idx]
    assert "_engine = None" in branch, (
        "the engine lifecycle is not cleared when managed storage falls back to "
        "embedded; storage_is_down() would then refuse every destructive "
        "operation on a healthy machine")


def test_no_managed_engine_means_storage_is_not_down():
    import ragtools.service.app as app_mod

    assert app_mod.get_engine() is None
    assert app_mod.storage_is_down() == ""
    assert app_mod.engine_status() is None


# --- the interrupted-rebuild marker --------------------------------------


def test_an_interrupted_rebuild_leaves_a_marker(tmp_path):
    settings = types.SimpleNamespace(data_dir=str(tmp_path))
    assert destructive.pending_intent(settings) is None
    destructive.record_intent(settings, {"operation": "rebuild",
                                         "collections": ["a", "b"]})
    pending = destructive.pending_intent(settings)
    assert pending is not None
    assert pending["collections"] == ["a", "b"]
    destructive.clear_intent(settings)
    assert destructive.pending_intent(settings) is None


# --- every door uses the same gate ---------------------------------------


def _calls_rebuild_under_guard(path: Path, func_name: str) -> bool:
    """Does ``func_name`` reach owner.rebuild() inside a destructive_operation?"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != func_name:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.With):
                continue
            for item in inner.items:
                call = item.context_expr
                if (isinstance(call, ast.Call)
                        and getattr(call.func, "attr", "") == "destructive_operation"):
                    # rebuild() must be INSIDE this with-block
                    for deeper in ast.walk(inner):
                        if (isinstance(deeper, ast.Call)
                                and getattr(deeper.func, "attr", "") == "rebuild"):
                            return True
    return False


@pytest.mark.parametrize("relpath,func", [
    ("service/pages.py", "ui_rebuild"),
    ("service/routes.py", "rebuild"),
])
def test_the_http_rebuild_entry_points_are_guarded(relpath, func):
    """Both HTTP doors. `ui_rebuild` called `owner.rebuild()` bare — any
    exception escaped to Starlette as a 500."""
    assert _calls_rebuild_under_guard(SRC / relpath, func), (
        f"{relpath}::{func} reaches owner.rebuild() without holding the "
        f"destructive-operation guard")


def test_the_job_handler_is_guarded():
    """A queued rebuild re-checks at the moment it RUNS, not when it was
    enqueued — storage state is not a property of the queue."""
    source = (SRC / "service" / "job_handlers.py").read_text(encoding="utf-8")
    assert "destructive_operation" in source


def test_the_ui_route_no_longer_calls_rebuild_bare():
    """The literal v3.2.0 shape, so it cannot come back."""
    source = (SRC / "service" / "pages.py").read_text(encoding="utf-8")
    idx = source.index("def ui_rebuild")
    body = source[idx:idx + 1600]
    assert "OperationRefused" in body, "ui_rebuild does not handle a refusal"
    assert "flash-error" in body, "a refusal is not shown to the user"


def test_the_api_route_returns_409_not_500():
    source = (SRC / "service" / "routes.py").read_text(encoding="utf-8")
    idx = source.index('@router.post("/api/rebuild")')
    body = source[idx:idx + 1600]
    assert "409" in body, (
        "a refused rebuild must be a 409 Conflict — the request is well-formed "
        "and the server is temporarily unable to honour it")


# --- rebuild ordering -----------------------------------------------------


def test_the_rebuild_destroys_nothing_it_has_not_already_replaced():
    """The v3.4 ordering rule, superseded by an absence (v3.5 WP-06).

    v3.4 recreated every collection and deleted the state DB up front, and the
    most that could be asked of it was that the delete came after the
    collections had been PROVEN present. The safe rebuild does neither: each
    project is built into a new collection, verified, swapped in with one
    atomic registry UPDATE, and only then is its predecessor dropped — so the
    state DB survives the run and no live collection is ever recreated.

    Both literals below are present in the v3.4 body, so this test fails
    against the version it was written for.
    """
    source = (SRC / "service" / "owner.py").read_text(encoding="utf-8")
    idx = source.index("def _rebuild_locked")
    body = source[idx:source.index("\n    def get_status")]

    assert "state_path.unlink()" not in body, (
        "the rebuild still deletes the state DB — the only record of what was "
        "ever indexed, and the thing a failed run needs most")
    assert "recreate_collection" not in body, (
        "the rebuild still drops a live collection before its replacement "
        "exists, let alone passes verification")
    assert "set_active_collection" in body, (
        "the rebuild does not swap through the registry, so there is no atomic "
        "handover from the old collection to the verified new one")


def test_rebuild_excludes_indexing_not_merely_other_rebuilds():
    """`rebuild` took `self._lock`; `run_full_index` takes `_index_mutex`. Two
    different primitives meant a rebuild could drop every collection underneath
    a running migration at a window boundary."""
    source = (SRC / "service" / "owner.py").read_text(encoding="utf-8")
    idx = source.index("    def rebuild(self)")
    body = source[idx:idx + 1400]
    assert "_exclusive_index" in body, (
        "rebuild does not hold the index mutex, so it can interleave with a "
        "running index at a window boundary")
