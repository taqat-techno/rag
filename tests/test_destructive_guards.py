"""Every door that destroys, and the one gate all of them go through (WP-R09).

``service/destructive.py`` was written after the v3.2.0 rebuild incident, and it
worked — for rebuild. Three releases later the audit found the same shape
elsewhere, untouched:

* ``POST /api/projects/{id}/reindex`` and ``DELETE /api/projects/{id}`` dropped
  collections with **no gate at all**, so both would run while ``/health`` was
  reporting the store unreachable — the precise situation the module exists to
  prevent, at a different door.
* ``POST /api/shutdown`` had no authorization, no confirmation and no guard.
  Anything that could reach ``127.0.0.1:21420`` could stop the knowledge base
  for every client on the machine. Being on the loopback interface was treated
  as permission, and it is not: the MCP proxy makes a restricted client's calls
  arrive on exactly that interface.
* MCP ``add_project_ignore_rule`` / ``remove_project_ignore_rule`` enforced a
  cooldown and nothing else. A rate limit is not an authorization decision.
* The UI project delete ran in a ``threading.Timer`` whose failures went nowhere
  a person could see, so a delete that failed rendered exactly like one that
  worked.

The acceptance test is :func:`test_every_destructive_endpoint_passes_through_the_gate`.
It does not carry a list of endpoints — it DERIVES both halves from the source:
which ``QdrantOwner`` methods destroy, and which route handlers reach them. A new
destructive endpoint fails it the moment it is written, and a renamed owner
method fails the self-check rather than silently emptying the enumeration.
"""

from __future__ import annotations

import ast
import tempfile
import types
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ragtools.config import ProjectConfig, Settings
from ragtools.profiles import ClientProfile
from ragtools.service import destructive
from ragtools.service import app as app_module
from ragtools.service.app import create_app
from ragtools.service.owner import QdrantOwner

SRC = Path(__file__).resolve().parent.parent / "src" / "ragtools"
FIXTURES = Path(__file__).parent / "fixtures"


# ===========================================================================
# 1. The enumeration — derived from the source, not maintained by hand
# ===========================================================================

#: The primitive acts that destroy indexed data. Everything else in the
#: derivation is reachability from these.
_DESTROYS = {"delete_collection", "recreate_collection"}

#: The module-level names that hold the gate. ``guarded`` is the context manager
#: (it holds the destructive lock for the body); ``authorize`` runs the same
#: capability / confirmation / precondition checks for an operation that mutates
#: but does not drop, and so has no lock to hold.
_GATE_CALLS = {"guarded", "destructive_operation", "authorize"}

_ROUTE_MODULES = ("service/routes.py", "service/pages.py")
_ROUTERS = {"router", "page_router"}


def _parse(rel: str) -> ast.Module:
    return ast.parse((SRC / rel).read_text(encoding="utf-8"))


def _called_names(node: ast.AST) -> tuple[set, set]:
    """``(attribute-call names, bare-name call names)`` anywhere under ``node``."""
    attrs: set[str] = set()
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Attribute):
                attrs.add(fn.attr)
            elif isinstance(fn, ast.Name):
                names.add(fn.id)
    return attrs, names


def destructive_owner_methods() -> set:
    """``QdrantOwner`` methods that destroy indexed data, transitively.

    Seeded from the primitives (a collection drop, a filtered point delete, a
    state-DB row removal) and closed over ``self.x()`` calls, so a method that
    only destroys via a helper is still named.
    """
    tree = _parse("service/owner.py")
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "QdrantOwner")
    methods = {n.name: n for n in cls.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def destroys_directly(fn) -> bool:
        for sub in ast.walk(fn):
            if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)):
                continue
            attr = sub.func.attr
            if attr in _DESTROYS:
                return True
            # client.delete(collection_name=..., points_selector=...)
            if attr == "delete" and any(k.arg == "points_selector" for k in sub.keywords):
                return True
            # state.remove(<file_path>) — the index's memory of a file
            value = sub.func.value
            if attr == "remove" and isinstance(value, ast.Name) and value.id == "state":
                return True
        return False

    found = {name for name, fn in methods.items() if destroys_directly(fn)}
    changed = True
    while changed:
        changed = False
        for name, fn in methods.items():
            if name in found:
                continue
            for sub in ast.walk(fn):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == "self"
                        and sub.func.attr in found):
                    found.add(name)
                    changed = True
                    break
    return {n for n in found if not n.startswith("_")}


def _route_handlers() -> list:
    """``(relpath, handler_name, method, path, lineno)`` for every HTTP route."""
    out = []
    for rel in _ROUTE_MODULES:
        for node in _parse(rel).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                fn = dec.func if isinstance(dec, ast.Call) else dec
                if not (isinstance(fn, ast.Attribute)
                        and isinstance(fn.value, ast.Name)
                        and fn.value.id in _ROUTERS):
                    continue
                path = ""
                if isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Constant):
                    path = dec.args[0].value
                out.append((rel, node.name, fn.attr.upper(), path, node.lineno))
    return out


def _module_functions() -> dict:
    return {rel: {n.name: n for n in ast.walk(_parse(rel))
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for rel in _ROUTE_MODULES}


def _reaches_destruction(rel, fname, funcs, targets, seen=None) -> bool:
    """Does ``fname`` reach a destructive owner method, directly or via helpers?"""
    seen = seen if seen is not None else set()
    if (rel, fname) in seen:
        return False
    seen.add((rel, fname))
    fn = funcs[rel].get(fname)
    if fn is None:
        return False
    attrs, names = _called_names(fn)
    if attrs & targets:
        return True
    for candidate in attrs | names:
        for other in _ROUTE_MODULES:
            if candidate in funcs[other] and (other, candidate) not in seen:
                if _reaches_destruction(other, candidate, funcs, targets, seen):
                    return True
    return False


def _holds_gate(fn) -> bool:
    """Does this function call the gate itself?"""
    attrs, _ = _called_names(fn)
    return bool(attrs & _GATE_CALLS)


def _is_compliant(rel, fname, funcs, targets, seen=None) -> bool:
    """A handler is compliant if it holds the gate, or delegates only to handlers
    that do. Delegation is how the UI fragments reuse the API handlers — one
    implementation, one gate — and a redundant second gate on an exclusive
    operation would deadlock on its own non-reentrant lock."""
    seen = seen if seen is not None else set()
    if (rel, fname) in seen:
        return True
    seen.add((rel, fname))
    fn = funcs[rel].get(fname)
    if fn is None:
        return False
    if _holds_gate(fn):
        return True
    attrs, names = _called_names(fn)
    delegates = set()
    for candidate in attrs | names:
        for other in _ROUTE_MODULES:
            if candidate in funcs[other]:
                if _reaches_destruction(other, candidate, funcs, targets, set()):
                    delegates.add((other, candidate))
    if not delegates:
        return False
    return all(_is_compliant(o, c, funcs, targets, seen) for o, c in delegates)


def destructive_endpoints() -> list:
    """Every route handler that reaches a destructive owner method."""
    targets = destructive_owner_methods()
    funcs = _module_functions()
    return [entry for entry in _route_handlers()
            if _reaches_destruction(entry[0], entry[1], funcs, targets, set())]


# --- the derivation must not be able to derive nothing --------------------


def test_the_derivation_still_finds_the_owner_methods_it_is_built_on():
    """An enumeration that quietly returns the empty set proves nothing.

    These five are the destroying methods by name. If a rename or refactor makes
    one unreachable from the primitives, this fails HERE — loudly, on the
    derivation — instead of silently shrinking the endpoint list the acceptance
    test walks.
    """
    found = destructive_owner_methods()
    for expected in ("rebuild", "delete_project_data", "reindex_project",
                     "run_full_index", "run_incremental_index"):
        assert expected in found, (
            f"QdrantOwner.{expected} no longer derives as destructive; the "
            f"endpoint enumeration is now blind to every door that uses it")


def test_the_derivation_finds_the_endpoints_the_audit_named():
    """The two ungated doors from the v3.4 audit must be IN the enumeration.

    If they ever stop being enumerated, the acceptance test below would pass by
    not looking at them.
    """
    named = {(rel, fn) for rel, fn, _m, _p, _l in destructive_endpoints()}
    assert ("service/routes.py", "project_reindex_endpoint") in named
    assert ("service/routes.py", "project_delete") in named
    assert ("service/pages.py", "ui_projects_remove") in named
    # ...and a handful of endpoints must always be OUT of it, or the test is
    # just asserting "every route is guarded", which would be a different claim.
    assert ("service/routes.py", "search") not in named
    assert ("service/routes.py", "health") not in named


# --- the acceptance criterion --------------------------------------------


def test_every_destructive_endpoint_passes_through_the_gate():
    """The acceptance criterion. Derived, so it cannot go stale.

    Both halves are read from the source at run time: the destroying owner
    methods, and the route handlers that reach them. Add an endpoint that drops
    a collection and forget the gate, and this fails on the new endpoint's name
    with no list anywhere to update.
    """
    targets = destructive_owner_methods()
    funcs = _module_functions()
    offenders = []
    for rel, fname, method, path, lineno in destructive_endpoints():
        if not _is_compliant(rel, fname, funcs, targets, set()):
            offenders.append(f"{rel}:{lineno} {method} {path or fname}")
    assert not offenders, (
        "these endpoints destroy indexed data without passing through "
        "service/destructive.py: " + ", ".join(sorted(offenders))
    )


def test_an_exclusive_operation_holds_the_lock_for_its_whole_body():
    """``authorize`` is not a substitute for ``guarded`` where a lock is needed.

    An operation declared ``exclusive`` drops collections. Acquiring and
    releasing the destructive lock up front would prove only that nothing else
    was running at that instant — the ``with`` form is what keeps a second
    caller out for the duration.
    """
    operations = getattr(destructive, "OPERATIONS", None)
    assert operations, (
        "service/destructive.py declares no operation registry, so nothing "
        "states which operations must hold the lock")
    exclusive = {name for name, op in operations.items() if op.exclusive}
    assert exclusive, "no exclusive operations declared — the check is vacuous"

    funcs = _module_functions()
    offenders = []
    for rel in _ROUTE_MODULES:
        for fname, fn in funcs[rel].items():
            for sub in ast.walk(fn):
                if not (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "authorize"):
                    continue
                named = [a for a in sub.args if isinstance(a, ast.Constant)]
                for arg in named:
                    if arg.value in exclusive:
                        offenders.append(f"{rel}::{fname} -> authorize({arg.value!r})")
    assert not offenders, (
        "an exclusive operation was authorized without holding the lock; use "
        "`with destructive.guarded(...)`: " + ", ".join(sorted(offenders)))


def test_every_declared_operation_names_a_real_capability():
    """A policy entry pointing at a capability nobody can hold is a locked door
    with no key — every caller, including the owner, would be refused."""
    from ragtools.profiles import CAPABILITY_GROUPS

    operations = getattr(destructive, "OPERATIONS", None)
    assert operations, (
        "no operation registry: mutating endpoints are authorized against "
        "nothing at all")
    grantable = set().union(*CAPABILITY_GROUPS.values())
    bad = sorted(op.capability for op in operations.values()
                 if op.capability not in grantable)
    assert not bad, f"operations name capabilities no group grants: {bad}"


def test_an_undeclared_operation_is_refused_not_allowed():
    """An operation nobody declared is the one nobody reviewed."""
    lookup = getattr(destructive, "policy_for", None)
    assert lookup is not None, (
        "there is no operation lookup, so an unrecognised operation has no way "
        "to be refused — it simply is not checked")
    with pytest.raises(KeyError):
        lookup("quietly_delete_everything")


# ===========================================================================
# 2. Live endpoint behaviour
# ===========================================================================


@pytest.fixture(scope="module")
def client():
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(
            content_root=str(FIXTURES),
            state_db=str(Path(tmpdir) / "state.db"),
            data_dir=str(Path(tmpdir) / "data"),
            projects=[
                ProjectConfig(id="project_a", path=str(FIXTURES / "project_a")),
                ProjectConfig(id="project_b", path=str(FIXTURES / "project_b")),
            ],
        )
        owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        owner.run_full_index()

        app_module._owner = owner
        app_module._settings = settings
        app = create_app()
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc
        app_module._owner = None
        app_module._settings = None


@pytest.fixture(autouse=True)
def restore_fixture_projects(client):
    """Put the fixture projects back after every test.

    Load-bearing for the NEGATIVE CONTROL, not for the fix. Against the
    pre-change tree the unguarded ``DELETE /api/projects/project_a`` actually
    succeeds, and every later test in the module then reports ``404`` — a
    downstream symptom that hides the failure each test is meant to demonstrate.
    Restoring makes each control failure say what it means. Against the fixed
    tree nothing is ever deleted, so this is a no-op.
    """
    yield
    owner = app_module._owner
    settings = app_module._settings
    wanted = {"project_a", "project_b"}
    have = {p.id for p in settings.projects}
    if wanted - have:
        owner.update_projects([
            ProjectConfig(id=pid, path=str(FIXTURES / pid))
            for pid in sorted(wanted)
        ])


#: Every destructive endpoint, with a request that would otherwise succeed.
#: ``project_c`` does not exist — deliberately: a 404 would prove the guard did
#: NOT run, because the guard is checked before the project is even looked up.
_DESTRUCTIVE_CALLS = [
    ("rebuild",           "POST",   "/api/rebuild", None),
    ("project_reindex",   "POST",   "/api/projects/project_a/reindex", None),
    ("project_delete",    "DELETE", "/api/projects/project_a", None),
    ("project_set_mode",  "POST",   "/api/projects/project_a/mode", {"mode": "code"}),
    ("index",             "POST",   "/api/index", {"project": "project_a"}),
    ("dependency_delete", "DELETE", "/api/dependencies/nope", None),
    ("frameworks_sync",   "POST",   "/api/frameworks/sync", None),
]


def _call(client, method, url, body, **kw):
    if method == "POST":
        return client.post(url, json=body, **kw)
    if method == "PUT":
        return client.put(url, json=body, **kw)
    return client.request(method, url, **kw)


@pytest.mark.parametrize("name,method,url,body", _DESTRUCTIVE_CALLS,
                         ids=[c[0] for c in _DESTRUCTIVE_CALLS])
def test_a_destructive_endpoint_refuses_when_storage_is_unreachable(
        client, monkeypatch, name, method, url, body):
    """The v3.2.0 incident, re-run at every door.

    ``/health`` knew the store was gone. The rebuild took a backup and started
    dropping collections anyway. Reindex and delete still did in v3.5.0.
    """
    owner = app_module._owner
    monkeypatch.setattr(type(owner), "storage_reachable",
                        lambda self: (False, "WinError 10061"), raising=True)

    r = _call(client, method, url, body)
    assert r.status_code == 409, (
        f"{method} {url} did not refuse while storage was unreachable "
        f"(got {r.status_code})")
    assert "10061" in r.text, "the refusal does not say what is actually wrong"


@pytest.mark.parametrize("name,method,url,body", _DESTRUCTIVE_CALLS,
                         ids=[c[0] for c in _DESTRUCTIVE_CALLS])
def test_a_destructive_endpoint_refuses_while_a_migration_is_active(
        client, monkeypatch, name, method, url, body):
    """A migration owns the index until it finishes. Destroying underneath it
    throws away work already done and orphans the plan."""
    from ragtools.upgrade import relayout

    report = types.SimpleNamespace(
        complete=False, describe=lambda: "3/15 units done", plan_id="plan-1",
        done=3, total=15, blocked=0, failed=0, pending=12,
        blocked_reason="", stalled=False)
    monkeypatch.setattr(relayout, "active_plan", lambda settings: "plan-1")
    monkeypatch.setattr(relayout, "progress", lambda settings, plan: report)

    r = _call(client, method, url, body)
    assert r.status_code == 409, (
        f"{method} {url} ran while a migration held the index "
        f"(got {r.status_code})")
    assert "migration" in r.text.lower()


def test_the_guard_runs_before_the_project_is_even_resolved(client, monkeypatch):
    """Ordering, stated as a test: preconditions BEFORE anything happens.

    A 404 here would mean the handler had already begun its work. The guard has
    to be the first thing, or "checked before it mutates" is only true for
    whichever branch happens to reach it.
    """
    owner = app_module._owner
    monkeypatch.setattr(type(owner), "storage_reachable",
                        lambda self: (False, "down"), raising=True)
    r = client.post("/api/projects/no_such_project/reindex")
    assert r.status_code == 409, (
        "a nonexistent project reached the 404 path, so the guard ran after the "
        "handler had started")


def test_a_destructive_endpoint_does_not_destroy_when_it_refuses(client, monkeypatch):
    """The refusal is not cosmetic — nothing was deleted."""
    owner = app_module._owner
    called = []
    monkeypatch.setattr(type(owner), "storage_reachable",
                        lambda self: (False, "down"), raising=True)
    monkeypatch.setattr(type(owner), "delete_project_data",
                        lambda self, pid: called.append(pid), raising=True)

    r = client.request("DELETE", "/api/projects/project_a")
    # The effect first: a refusal that still deleted would be worse than no
    # refusal at all, and asserting the status code first would hide it behind
    # whatever the half-finished handler happened to raise.
    assert called == [], "the project's data was deleted despite the refusal"
    assert r.status_code == 409


# --- shutdown -------------------------------------------------------------


@pytest.fixture
def shutdown_attempts(monkeypatch):
    """Record whether the handler reached its "actually stop now" step.

    The real handler signals ``SIGINT`` to its OWN pid from a background thread
    half a second after responding — which, in a test process, is the pytest run
    itself, and an unguarded endpoint therefore ABORTS the suite instead of
    failing a test. Replacing the module's ``threading`` stops that thread from
    ever starting, and makes the observation timing-independent: the list is
    non-empty exactly when the handler decided to stop the service.
    """
    from ragtools.service import routes as routes_mod

    started = []

    class _Thread:
        def __init__(self, **kw):
            self._kw = kw

        def start(self):
            started.append(self._kw.get("target"))

    monkeypatch.setattr(routes_mod, "threading",
                        types.SimpleNamespace(Thread=_Thread), raising=True)
    monkeypatch.setattr("os.kill", lambda pid, sig: started.append(("kill", sig)))
    return started


def test_shutdown_without_confirmation_is_refused(client, shutdown_attempts):
    """No auth, no confirm, no guard — the whole defect in one call.

    A bare ``POST /api/shutdown`` stopped the service for every client on the
    machine, and reaching the loopback port was the only thing it took.
    """
    r = client.post("/api/shutdown")
    assert r.status_code == 428, (
        f"POST /api/shutdown ran with no confirmation (got {r.status_code})")
    assert shutdown_attempts == [], "the service was stopped anyway"


def test_shutdown_with_a_wrong_token_is_refused(client, shutdown_attempts):
    """A guessable literal would be theatre; the token is this process's
    instance id, which a caller has to read from /identity first."""
    r = client.post("/api/shutdown?confirm=shutdown")
    assert r.status_code == 428
    assert shutdown_attempts == []


def test_shutdown_requires_the_capability_even_with_the_right_token(
        client, monkeypatch, shutdown_attempts):
    """Confirmation proves intent, not permission. A client that may read the
    logs must not be able to stop the service by reading /identity first."""
    from ragtools.service import routes as routes_mod

    diagnostics_only = ClientProfile(
        profile_id="watcher-bot", allowed_projects=None,
        capability_groups=frozenset({"service_operations", "retrieval"}))
    monkeypatch.setattr(destructive, "request_profile",
                        lambda request: diagnostics_only, raising=False)

    r = client.post(f"/api/shutdown?confirm={routes_mod._INSTANCE_ID}")
    assert r.status_code == 403, (
        f"a diagnostics-only profile stopped the service (got {r.status_code})")
    assert shutdown_attempts == []


def test_shutdown_succeeds_for_the_owner_with_the_published_token(
        client, shutdown_attempts):
    """The contract has to be satisfiable, or `rag service stop` is broken."""
    from ragtools.service import routes as routes_mod

    ident = client.get("/identity")
    assert ident.status_code == 200
    token = ident.json()["instance_id"]
    assert token == routes_mod._INSTANCE_ID

    r = client.post(f"/api/shutdown?confirm={token}")
    assert r.status_code == 200
    assert r.json()["status"] == "shutting_down"
    assert shutdown_attempts, "the owner's confirmed shutdown did nothing"


def test_stop_service_reads_the_token_before_it_posts():
    """The CLI is a surface, not an exemption: it satisfies the same contract."""
    source = (SRC / "service" / "process.py").read_text(encoding="utf-8")
    idx = source.index("def stop_service")
    body = source[idx:idx + 1800]
    assert "/identity" in body, (
        "stop_service posts to /api/shutdown without reading the instance id, "
        "so a graceful stop can only ever fall through to force-kill")
    assert "confirm" in body


# --- capability policy: writes are default-closed -------------------------

#: The header the MCP proxy stamps on a forwarded call. A LITERAL, deliberately:
#: reading it from the module under test would make these tests pass vacuously
#: on a build that sends no header at all.
_PROFILE_HEADER = "X-Client-Profile"


def _seed_profile(profile: ClientProfile) -> None:
    """Store ``profile`` where the SERVICE looks for it, and nowhere else.

    The whole point of the header is that the service re-decides for itself, so
    these tests go through the real store rather than patching the resolver —
    on a build that ignores the header they simply succeed, which is the defect
    stated as an outcome instead of an ``AttributeError``.
    """
    from ragtools.profile_store import ProfileStore

    settings = app_module._settings
    store = ProfileStore(str(Path(settings.data_dir) / "profiles.db"))
    store.add(profile)


_RESTRICTED_WRITES = [
    ("POST",   "/api/projects/project_a/ignore", {"pattern": "*.log"}),
    ("DELETE", "/api/projects/project_a/ignore?pattern=*.log", None),
    ("POST",   "/api/projects/project_a/reindex", None),
    ("DELETE", "/api/projects/project_a", None),
    ("POST",   "/api/projects/project_a/mode", {"mode": "code"}),
    ("POST",   "/api/rebuild", None),
    ("POST",   "/api/index", {"project": "project_a"}),
    ("PUT",    "/api/config", {"top_k": 7}),
    ("POST",   "/api/dependencies", {"id": "x", "path": "."}),
]


@pytest.mark.parametrize("method,url,body", _RESTRICTED_WRITES,
                         ids=[f"{m}{u.split('?')[0]}" for m, u, _ in _RESTRICTED_WRITES])
def test_a_retrieval_only_profile_cannot_reach_any_write(client, method, url, body):
    """Default-closed, enumerated over every write door.

    A profile granted retrieval and nothing else holds no write capability, so
    every write endpoint must refuse it — including the two ignore-rule
    endpoints, whose only gate was a cooldown.
    """
    _seed_profile(ClientProfile(profile_id="reader-bot", allowed_projects=None,
                                capability_groups=frozenset({"retrieval"})))

    r = _call(client, method, url, body,
              headers={_PROFILE_HEADER: "reader-bot"})
    # 403 specifically, not "any refusal": a write that happens to be declined
    # for an unrelated reason today (a validation error, a busy index) is still
    # wide open the moment that reason goes away.
    assert r.status_code == 403, (
        f"{method} {url} was not refused on AUTHORIZATION for a retrieval-only "
        f"client (got {r.status_code})")


def test_a_project_manager_profile_still_cannot_delete_a_project(client):
    """The destructive modifier, where it matters.

    "Manage projects" is the group an owner grants a client so it can add a
    folder and edit ignore rules. Deleting one is irreversible, so it stays
    closed until the owner ticks the separate destructive box.
    """
    _seed_profile(ClientProfile(
        profile_id="pm-bot", allowed_projects=None,
        capability_groups=frozenset({"project_management"}),
        destructive_policy="forbidden"))
    headers = {_PROFILE_HEADER: "pm-bot"}

    allowed = client.post("/api/projects/project_a/ignore",
                          json={"pattern": "*.tmp"}, headers=headers)
    assert allowed.status_code == 200, "the granted capability was refused"
    client.request("DELETE", "/api/projects/project_a/ignore?pattern=*.tmp",
                   headers=headers)

    denied = client.request("DELETE", "/api/projects/project_a", headers=headers)
    assert denied.status_code == 403, (
        f"a client with destructive access OFF deleted a project "
        f"(got {denied.status_code})")
    assert "destructive" in denied.text.lower()


def test_a_client_with_the_confirm_policy_must_send_the_token(client):
    """``destructive_policy="confirm_token"`` is the opt-in an owner grants a
    client; it means "you may, once you say which"."""
    _seed_profile(ClientProfile(
        profile_id="ops-bot", allowed_projects=None,
        capability_groups=frozenset({"project_management", "indexing"}),
        destructive_policy="confirm_token"))
    headers = {_PROFILE_HEADER: "ops-bot"}

    blind = client.post("/api/projects/project_a/reindex", headers=headers)
    assert blind.status_code == 428, (
        f"a confirm_token client reindexed blind (got {blind.status_code})")

    wrong = client.post("/api/projects/project_a/reindex?confirm=project_b",
                        headers=headers)
    assert wrong.status_code == 428, "another project's id satisfied the contract"


def test_an_unknown_client_profile_fails_closed(client):
    """A named client that silently becomes the owner is the worst outcome of a
    typo — the rule ``mcp_authz.resolve_active_profile`` already enforced for the
    MCP process, applied to the service that actually performs the write."""
    r = client.post("/api/projects/project_a/ignore", json={"pattern": "*.ghost"},
                    headers={_PROFILE_HEADER: "ghost-client"})
    assert r.status_code == 403, (
        f"an unknown client profile was served as the owner (got {r.status_code})")
    assert "ghost-client" in r.text


def test_no_header_still_means_the_owner(client):
    """Backward compatibility, pinned deliberately: this passes before and after.

    The admin panel, the CLI and a single-owner MCP process send no profile
    header, and every one of them must behave exactly as it did. A gate that
    breaks the default install is not a fix.
    """
    r = client.post("/api/projects/project_a/ignore", json={"pattern": "*.owner"})
    assert r.status_code == 200
    client.request("DELETE", "/api/projects/project_a/ignore?pattern=*.owner")


# --- the MCP surface ------------------------------------------------------


def test_mcp_ignore_rule_tools_check_the_capability(monkeypatch):
    """The named defect: these enforced a cooldown and nothing else.

    A cooldown is a rate limit. It answers "how often", never "who" — so a
    retrieval-only client could shrink another client's index one pattern at a
    time, at a polite pace.
    """
    from ragtools.integration import mcp_server

    reader = ClientProfile(profile_id="reader", allowed_projects=None,
                           capability_groups=frozenset({"retrieval"}))
    monkeypatch.setattr(mcp_server, "_active_profile", lambda: reader)
    posted = []
    monkeypatch.setattr(mcp_server, "proxy_post",
                        lambda *a, **k: posted.append(a) or {"ok": True})
    monkeypatch.setattr(mcp_server, "proxy_delete",
                        lambda *a, **k: posted.append(a) or {"ok": True})

    added = mcp_server.add_project_ignore_rule("project_a", "*.log")
    assert added["ok"] is False, "a retrieval-only client added an ignore rule"
    assert added["error"]

    removed = mcp_server.remove_project_ignore_rule("project_a", "*.log")
    assert removed["ok"] is False
    assert posted == [], "the write reached the service despite the refusal"


def test_mcp_write_tools_all_check_the_capability():
    """Every registered write tool, enumerated from the registration table.

    A per-tool check is a thing to forget on the next tool, and the two that were
    forgotten are exactly the ones nobody re-read.
    """
    tree = ast.parse((SRC / "integration" / "mcp_server.py").read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    from ragtools.mcp_capabilities import tool_spec

    offenders = []
    for name, fn in funcs.items():
        spec = tool_spec(name)
        if spec is None or spec.read_only:
            continue
        attrs, names = _called_names(fn)
        if "_ops_capability_error" not in names:
            offenders.append(name)
    assert not offenders, (
        "these MCP write tools do not re-check the caller's capability: "
        + ", ".join(sorted(offenders)))


def test_the_mcp_proxy_tells_the_service_who_it_is():
    """In-process checking only binds a well-behaved client. The service performs
    the write, so the service re-decides — which it can only do if the profile id
    travels with the request."""
    import os

    from ragtools.integration import mcp_common

    build = getattr(mcp_common, "proxy_headers", None)
    assert build is not None, (
        "the MCP proxy builds its headers inline with no profile id, so every "
        "forwarded call reaches the service anonymous — and the service, having "
        "no idea who is calling, performs it as the owner")

    old = os.environ.get("RAG_CLIENT_PROFILE")
    try:
        os.environ.pop("RAG_CLIENT_PROFILE", None)
        assert _PROFILE_HEADER not in build("ab12"), (
            "a single-owner MCP process must send no profile header")
        os.environ["RAG_CLIENT_PROFILE"] = "roy"
        assert build("ab12").get(_PROFILE_HEADER) == "roy", (
            "a named client's id does not travel with its requests")
    finally:
        os.environ.pop("RAG_CLIENT_PROFILE", None)
        if old is not None:
            os.environ["RAG_CLIENT_PROFILE"] = old


# --- the UI delete --------------------------------------------------------


def test_a_failed_ui_delete_is_shown_to_the_user(client, monkeypatch):
    """The fire-and-forget timer, and why it could never report anything.

    The old handler removed the project from the config, returned the fragment,
    and only THEN deleted the data from a ``threading.Timer``. By the time the
    delete failed the response was long gone, so a delete that failed rendered
    identically to one that worked — and the config no longer had the project
    the orphaned vectors belonged to.
    """
    owner = app_module._owner

    def _boom(self, pid):
        raise RuntimeError("qdrant refused the drop")

    monkeypatch.setattr(type(owner), "delete_project_data", _boom, raising=True)

    r = client.request("DELETE", "/ui/projects/project_a/remove")
    assert r.status_code == 200          # htmx does not swap a 4xx
    assert "flash-error" in r.text, "a failed delete rendered as a success"
    assert "qdrant refused the drop" in r.text, (
        "the fragment does not say what actually went wrong")


def test_a_failed_ui_delete_leaves_the_project_configured(client, monkeypatch):
    """A failed delete must leave a project that still exists, not a config with
    no project and vectors with no owner."""
    owner = app_module._owner
    monkeypatch.setattr(type(owner), "delete_project_data",
                        lambda self, pid: (_ for _ in ()).throw(RuntimeError("nope")),
                        raising=True)

    client.request("DELETE", "/ui/projects/project_a/remove")
    configured = {p["id"] for p in client.get("/api/projects/configured").json()["projects"]}
    assert "project_a" in configured, (
        "the project was removed from the config even though its data survived")


def test_no_destructive_ui_fragment_defers_its_work_to_a_detached_timer():
    """The literal v3.5.0 shape, so it cannot come back — anywhere.

    Parsed, not grepped, for the reason ``test_architecture_boundaries`` gives:
    a text search matches the docstring explaining why the timer was removed,
    which turns the build red for a comment and teaches people to delete the
    comment.

    A ``Timer`` or a ``Thread`` started inside a request handler that destroys
    data is unreportable by construction: the response is already written by the
    time the work fails.
    """
    tree = _parse("service/pages.py")
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    targets = destructive_owner_methods()
    all_funcs = _module_functions()

    offenders = []
    for rel, fname, _method, _path, _lineno in destructive_endpoints():
        if rel != "service/pages.py":
            continue
        fn = funcs.get(fname)
        if fn is None:
            continue
        attrs, names = _called_names(fn)
        if {"Timer", "Thread"} & (attrs | names):
            offenders.append(fname)
    assert not offenders, (
        "these destructive UI fragments defer their work to a detached "
        "timer/thread, whose failures cannot reach the response that already "
        "went out: " + ", ".join(sorted(offenders)))

    remove = funcs["ui_projects_remove"]
    assert "OperationRefused" in ast.dump(remove), (
        "the UI delete does not handle a refusal")
