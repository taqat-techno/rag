"""Recovering from a handled failure without restarting anything (WP-R05).

A rebuild that ends with failures re-writes ``rebuild-intent.json`` rather than
clearing it, naming the projects it could not finish, and ``/health`` reports
``rebuild_interrupted``. That was the whole of it: nothing re-drove those
projects, nothing re-tested why they had failed, and nothing ever removed the
marker. The product's own words for the remedy were "Use Rebuild or restart the
service to recover" — and a restart re-drove nothing either, so a user who
followed the advice got the same banner back with a fresher timestamp.

Three properties are pinned here, and each of them is a sentence the old build
could not have said truthfully:

* recovery happens **on the ordinary maintenance tick**, inside a process nobody
  restarted;
* ``/health`` keeps reporting the unresolved plan **until it is genuinely
  resolved**, not until something hoped it was;
* a blocker that is still real is reported as **re-tested**, with the time it was
  measured — never as the record that was written down when it first happened.

**How they are asked.** Through the surfaces that exist in both trees: the
service's ``/health`` payload and the product's own maintenance table. A control
run against the pre-change tree fails on the answer — no ``recovery`` key, a
marker that survives the tick — rather than on an import.
"""

from __future__ import annotations

import ast
import json
import re
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import ragtools.service.app as app_module
from ragtools.config import ProjectConfig, Settings
from ragtools.service import destructive
from ragtools.service.app import create_app
from ragtools.service.maintenance import MaintenanceScheduler, build_default_tasks
from ragtools.service.owner import QdrantOwner
from ragtools.upgrade import relayout

SRC = Path(__file__).resolve().parents[1] / "src" / "ragtools"


# --- an installation with one project and one interrupted rebuild ------------


class Install:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        project = tmp_path / "alpha"
        project.mkdir()
        (project / "readme.md").write_text(
            "# Alpha\n\nThe project the rebuild could not finish.\n",
            encoding="utf-8")
        self.settings = Settings(
            content_root=str(tmp_path),
            qdrant_path=str(tmp_path / "qdrant"),
            state_db=str(tmp_path / "state.db"),
            projects=[ProjectConfig(id="alpha", path=str(project))],
        )
        self.owner = QdrantOwner(settings=self.settings,
                                 client=Settings.get_memory_client())
        self.indexed: list = []
        self._real_index = self.owner.run_full_index

        def _counting(*args, **kwargs):
            self.indexed.append(kwargs.get("project_id"))
            return self._real_index(*args, **kwargs)

        self.owner.run_full_index = _counting

    def interrupt(self, *, failed=("alpha",)) -> None:
        """Leave the marker a rebuild that ended with failures leaves."""
        destructive.record_intent(self.settings, {
            "operation": "rebuild",
            "collections": [self.settings.collection_name],
            "state_db": str(self.settings.state_db),
            "projects": ["alpha"],
            "status": "completed_with_failures",
            "failed_projects": list(failed),
        })

    def block_storage(self, detail: str) -> None:
        self.owner.storage_reachable = lambda: (False, detail)

    def unblock_storage(self) -> None:
        self.owner.storage_reachable = lambda: (True, "")

    def close(self):
        self.owner.close()


def _forget_cached_verdict() -> None:
    """Drop any precondition verdict a previous test left cached.

    Tolerant of the module being absent so the fixture itself can never be what
    fails: a control run has to reach the assertions.
    """
    try:
        from ragtools.service import recovery
    except ImportError:
        return
    recovery.reset_for_tests()


@pytest.fixture
def install(tmp_path):
    _forget_cached_verdict()
    inst = Install(tmp_path)
    try:
        yield inst
    finally:
        inst.close()
        _forget_cached_verdict()


@pytest.fixture
def api(install, monkeypatch):
    monkeypatch.setattr(app_module, "_owner", install.owner, raising=False)
    monkeypatch.setattr(app_module, "_settings", install.settings, raising=False)
    return TestClient(create_app())


@pytest.fixture
def scheduler(install):
    """The product's own maintenance table, driven by hand instead of a thread."""
    return MaintenanceScheduler(
        build_default_tasks(install.owner),
        lock_held=lambda: install.owner.indexing,
    )


def recovery_task(scheduler):
    for task in scheduler.tasks:
        if task.name == "rebuild-recovery":
            return task
    raise AssertionError(
        "this installation has no unattended recovery for an interrupted "
        f"rebuild, so the only remedy is an operator: "
        f"{[t.name for t in scheduler.tasks]}")


def health(api) -> dict:
    response = api.get("/health")
    assert response.status_code == 200, response.text[:300]
    return response.json()


def recovery_block(api):
    body = health(api)
    assert "recovery" in body, (
        "/health says nothing about an unresolved rebuild, so there is no way "
        "to tell one that is being re-driven from one nobody will ever touch")
    return body["recovery"]


def unresolved(api) -> dict:
    """The recovery block, asserted to be present. Fails loudly when it is not."""
    block = recovery_block(api)
    assert block is not None, (
        "an interrupted rebuild is not reported as an unresolved plan, so "
        "nothing distinguishes one that is being re-driven from one nobody will "
        "ever touch")
    return block


def retry(api):
    """The operator's retry, asked the way an operator asks."""
    response = api.post("/api/recovery/retry")
    assert response.status_code in (200, 202), (
        f"this installation offers no way to retry an unresolved rebuild: "
        f"{response.status_code} {response.text[:300]}")
    return response.json()


# --- 1. it recovers on the tick, with nothing restarted ---------------------


def test_an_interrupted_rebuild_is_re_driven_on_the_maintenance_tick(
        install, scheduler, api):
    """No restart. The same process, the same owner, one ordinary tick.

    `rebuild-recovery` is a startup task, so the first tick is the one that runs
    it — the most likely moment to find an interrupted rebuild is right after the
    thing that interrupted it.
    """
    install.interrupt()
    assert destructive.pending_intent(install.settings) is not None
    before = app_module._owner

    scheduler.tick()

    assert install.indexed == ["alpha"], (
        "the project the rebuild could not finish was never re-driven")
    assert destructive.pending_intent(install.settings) is None, (
        "the interrupted-rebuild marker survived a completed recovery")
    assert app_module._owner is before, "the service was replaced, not recovered"


def test_recovery_runs_from_the_startup_tick_not_only_a_later_one(scheduler):
    task = recovery_task(scheduler)
    assert task.run_at_startup is True
    assert task.lock == "index", (
        "an unattended re-index must skip while an index already holds the mutex")


def test_a_marker_that_names_nothing_is_not_turned_into_a_re_index(
        install, scheduler, api):
    """A hard kill can leave a marker that says a rebuild is unresolved without
    saying what it touched. Inventing a unit list from the live configuration
    would re-index projects the rebuild never reached."""
    recovery_task(scheduler)
    destructive.record_intent(install.settings, {"operation": "rebuild"})

    scheduler.tick()

    assert install.indexed == []
    assert destructive.pending_intent(install.settings) is not None
    assert recovery_block(api) is None, (
        "a marker naming nothing was adopted into a plan anyway")


def test_nothing_happens_when_no_rebuild_is_unresolved(install, scheduler, api):
    """The sweep exists and is a no-op on a healthy machine — it must not
    re-index a corpus because it ran."""
    recovery_task(scheduler)
    scheduler.tick()

    assert install.indexed == []
    assert recovery_block(api) is None


# --- 2. /health reports it until it is genuinely resolved -------------------


def test_health_reports_the_unresolved_plan_while_it_is_blocked(
        install, scheduler, api):
    install.interrupt()
    install.block_storage("[WinError 10061] the target machine actively refused it")

    scheduler.tick()

    block = unresolved(api)
    assert block["unresolved"] is True
    assert block["blocked"] >= 1
    assert "recovery_unresolved" in health(api)["issues"]
    assert install.indexed == [], "it re-indexed while the store was unreachable"


def test_health_stops_reporting_it_only_once_the_work_is_done(
        install, scheduler, api):
    install.interrupt()
    install.block_storage("[WinError 10061] the target machine actively refused it")
    scheduler.tick()
    unresolved(api)

    install.unblock_storage()
    # `due()` is interval-driven; the task has already run once this tick, so it
    # is re-armed by hand rather than by sleeping five minutes.
    recovery_task(scheduler).last_run = None
    recovery_task(scheduler).run_at_startup = True
    scheduler.tick()

    assert install.indexed == ["alpha"]
    body = health(api)
    assert body["recovery"] is None, (
        "/health still reports an unresolved rebuild after it was resolved")
    assert "recovery_unresolved" not in body["issues"]
    assert "rebuild_interrupted" not in body["issues"]


def test_the_report_names_the_remedy_that_works_here(install, scheduler, api):
    install.interrupt()
    install.block_storage("the engine is not listening")
    scheduler.tick()

    block = unresolved(api)

    assert block["remedy"], "an unresolved rebuild is reported with no remedy"
    assert "restart" not in block["remedy"].lower().replace(
        "nothing needs restarting", "")


# --- 3. re-tested, not recalled ---------------------------------------------


def test_a_still_blocked_precondition_is_reported_as_re_tested(
        install, scheduler, api):
    """The rule: a persisted `blocked_reason` is a fact about the past. Nothing
    about that record expires, and a two-hour-old `WinError 10061` shown beside a
    healthy engine is how a health payload loses its credibility."""
    install.interrupt()
    install.block_storage("[WinError 10061] actively refused")
    scheduler.tick()
    first = unresolved(api)
    assert first["precondition"] is not None, (
        "the blocker is reported only as it was recorded, with nothing saying "
        "whether it is still true")
    assert first["precondition"]["ok"] is False
    first_at = first["precondition"]["retested_at"]
    assert first_at > 0

    time.sleep(0.01)
    install.block_storage("the engine socket closed")
    recovery_task(scheduler).last_run = None
    scheduler.tick()

    second = unresolved(api)
    assert second["precondition"]["retested_at"] > first_at, (
        "the verdict was replayed rather than re-measured")
    assert "socket closed" in second["blocked_reason_recorded"], (
        "the recorded reason is the one written at the first failure, so it "
        "ages into a statement that is no longer true")


def test_a_blocker_that_lifted_is_not_reported_as_still_blocking(
        install, scheduler, api):
    install.interrupt()
    install.block_storage("[WinError 10061] actively refused")
    scheduler.tick()
    assert unresolved(api)["precondition"]["ok"] is False

    install.unblock_storage()
    recovery_task(scheduler).last_run = None
    scheduler.tick()

    assert recovery_block(api) is None
    assert install.indexed == ["alpha"]


def test_the_recorded_block_is_re_tested_rather_than_trusted(
        install, scheduler, api):
    """`relayout.block_all` writes why a unit was parked, and nothing about that
    record expires. Re-driving on it — instead of on a fresh measurement — is how
    a plan stays parked for hours after the outage ended."""
    install.interrupt()
    install.block_storage("storage went away")
    scheduler.tick()
    block = unresolved(api)
    assert block["blocked"] == 1
    assert "storage went away" in block["blocked_reason_recorded"]

    install.unblock_storage()
    recovery_task(scheduler).last_run = None
    scheduler.tick()

    assert install.indexed == ["alpha"], (
        "the unit stayed parked on a blocker that had lifted")
    assert recovery_block(api) is None


# --- bounded backoff, borrowed rather than reinvented -----------------------


def test_a_unit_that_keeps_failing_is_retried_a_bounded_number_of_times(
        install, scheduler, monkeypatch):
    """The retry policy is relayout's, not a second one. That module already
    learned — from an observed CPU loop — that an unbounded retry of a full
    re-index never converges."""
    monkeypatch.setattr(relayout, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    attempts: list = []

    def _always_fails(*args, **kwargs):
        attempts.append(kwargs.get("project_id"))
        raise RuntimeError("the project path went missing")

    install.owner.run_full_index = _always_fails
    install.interrupt()

    task = recovery_task(scheduler)
    for _ in range(relayout.MAX_ATTEMPTS + 3):
        task.last_run = None
        scheduler.tick()

    assert len(attempts) == relayout.MAX_ATTEMPTS, (
        f"the failing unit was attempted {len(attempts)} times against a bound "
        f"of {relayout.MAX_ATTEMPTS}")
    assert destructive.pending_intent(install.settings) is not None


def test_an_exhausted_unit_is_named_rather_than_silently_dropped(
        install, scheduler, api, monkeypatch):
    monkeypatch.setattr(relayout, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    install.owner.run_full_index = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("nope"))
    install.interrupt()

    task = recovery_task(scheduler)
    for _ in range(relayout.MAX_ATTEMPTS + 1):
        task.last_run = None
        scheduler.tick()

    block = unresolved(api)
    assert [u["id"] for u in block["attempts_exhausted"]] == ["alpha"]


def test_an_operator_retry_restores_the_attempt_budget(
        install, scheduler, api, monkeypatch):
    """Automatic retries are bounded precisely so a machine cannot loop; an
    operator who has fixed the cause is a different thing entirely, and only they
    know the cause was fixed."""
    monkeypatch.setattr(relayout, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    attempts: list = []

    def _always_fails(*args, **kwargs):
        attempts.append(1)
        raise RuntimeError("nope")

    install.owner.run_full_index = _always_fails
    install.interrupt()
    task = recovery_task(scheduler)
    for _ in range(relayout.MAX_ATTEMPTS + 2):
        task.last_run = None
        scheduler.tick()
    assert len(attempts) == relayout.MAX_ATTEMPTS
    assert unresolved(api)["attempts_exhausted"]

    install.owner.run_full_index = install._real_index
    retry(api)

    deadline = time.time() + 30
    while time.time() < deadline:
        if destructive.pending_intent(install.settings) is None:
            break
        time.sleep(0.05)
    assert destructive.pending_intent(install.settings) is None, (
        "an operator retry did not restore the attempt budget, so the only "
        "remaining remedy is to restart something")


def test_recovery_does_not_disable_search_for_every_other_project(
        install, scheduler, api):
    """A recovery plan is not a layout migration. `guard_ready` refuses EVERY
    search while a plan is `running` — right when the index really is half-built,
    and the v3.1.0 disproportion if one project's failed rebuild took retrieval
    down for the other twenty-four."""
    install.interrupt()
    install.block_storage("the engine is down")
    scheduler.tick()

    assert unresolved(api)["plan"] is not None
    assert relayout.active_plan(install.settings) is None, (
        "the recovery plan is indistinguishable from a layout migration, so it "
        "disables search for the whole installation")
    body = health(api)
    assert body["status"] == "ready"
    assert "reindex_in_progress" not in body["issues"]


# --- the acceptance criterion ------------------------------------------------


#: Phrases that OFFER a service restart. `restarts the engine` and `service
#: restarted while this job was active` are deliberately not matched: one
#: describes what the service does by itself, the other is a cause, not a cure.
_OFFERS_A_RESTART = re.compile(
    r"restart(?:ing)?\s+(?:the\s+)?(?:rag\s*tools\s+)?service", re.IGNORECASE)

#: Messages that name a restart for a reason that is NOT a recoverable failure.
#: Listed as the distinctive phrase of each sanctioned message rather than the
#: whole literal, because an f-string's constant parts are folded by the parser
#: and would otherwise pin the surrounding markup. Tight enough that a new
#: remedy cannot slip through: none of these appears in a sentence offering a
#: restart as a cure.
_NOT_A_FAILURE_REMEDY = (
    # A configuration change. The service reads config at startup; there is no
    # failure here to recover from.
    "Restart the service for the new configuration to take effect",
    "Restart the service, then run `rag index` to rebuild",
    # The supervisor's own verdict, as a banner TITLE. It reports what stopped,
    # not what the user should do about it.
    "Supervisor stopped restarting the service",
    # Describes automatic behaviour the product performs, not an instruction.
    "Supervisor will auto-restart the service on crash",
    # A tray menu ITEM — an action offered, not a remedy prescribed.
    "Restart service",
)


def _runtime_strings(path: Path):
    """Every string literal that is not a docstring, with its line number.

    Parsed rather than grepped, for the same reason
    ``test_architecture_boundaries`` parses: a regex version matches its own
    explanatory prose and turns the build red for a comment, which teaches people
    to delete the comment. Docstrings are excluded because this module's own
    prose has to be able to describe the advice it removed.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            yield node.lineno, node.value


def test_no_user_facing_string_offers_a_restart_as_the_remedy():
    """"Restart the service" must never be the documented remedy.

    It asks the user to discard the process holding the diagnosis, and here it
    did not even work: neither an interrupted rebuild nor an exhausted migration
    plan is re-driven on start, so the advice returned the same banner with a
    fresher timestamp.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        for lineno, text in _runtime_strings(path):
            if not _OFFERS_A_RESTART.search(text):
                continue
            if any(allowed in text for allowed in _NOT_A_FAILURE_REMEDY):
                continue
            offenders.append(f"{path.relative_to(SRC)}:{lineno}: {text!r}")
    assert not offenders, (
        "these strings offer a service restart as the remedy:\n  "
        + "\n  ".join(offenders))


def test_every_remedy_the_product_computes_avoids_a_restart():
    """The source scan pins the literals; this pins the values they compute."""
    import types

    from ragtools.service import errors
    from ragtools.service.app import migration_remedy

    computed = [
        migration_remedy(types.SimpleNamespace(storage_backend="managed")),
        migration_remedy(types.SimpleNamespace(storage_backend="embedded")),
        errors._remedy(),
    ]
    for remedy in computed:
        assert remedy, "a remedy came back empty"
        assert not _OFFERS_A_RESTART.search(
            remedy.replace("nothing needs restarting", "")), remedy


def test_the_health_payload_never_advertises_a_restart(install, scheduler, api):
    """The whole payload, with an unresolved rebuild in it — the state in which
    the old build printed the advice."""
    install.interrupt()
    install.block_storage("the engine is down")
    scheduler.tick()
    unresolved(api)

    body = json.dumps(health(api))

    assert not _OFFERS_A_RESTART.search(body.replace(
        "nothing needs restarting", "")), body[:600]
