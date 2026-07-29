"""A port that answers is not an engine you own.

The v3.1.0 incident, reproduced as a test and then prevented. Two RAG Tools
services ran on one machine. Both generated a managed-Qdrant config on the same
hardcoded port. The second engine failed to bind, its child exited — and the
second SERVICE then polled ``/readyz`` on that port, got 200 from the FIRST
instance's engine, matched the pinned version (both ship 1.15.5, so the version
discriminates nothing), and wrote its collections into a store it did not own.

Every test here is written so it FAILS against the shipped v3.1.0 behaviour.
``test_the_incident_itself`` is the one that matters: it constructs exactly the
observed situation and asserts the new code refuses it.
"""

from __future__ import annotations

import json
import socket
import types
from pathlib import Path

import pytest

from ragtools.service import engine_ownership as own
from ragtools.storage_managed import ManagedStartError, QdrantSupervisor


class FakeSettings:
    """Just enough Settings for the ownership surface."""

    def __init__(self, tmp_path, **kw):
        self.data_dir = str(tmp_path)
        self.qdrant_http_port = kw.get("qdrant_http_port")
        self.qdrant_grpc_port = kw.get("qdrant_grpc_port")
        self.instance_id = kw.get("instance_id")
        self.storage_backend = kw.get("storage_backend", "managed")


class DeadChild:
    """A spawned process that has already exited — address-in-use, in one word."""

    def __init__(self, pid=4242, returncode=1):
        self.pid = pid
        self.returncode = returncode

    def poll(self):
        return self.returncode


class LiveChild:
    def __init__(self, pid=4242):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return None


class Answering:
    """An HTTP double that always answers 200 — i.e. SOMEBODY is on the port."""

    status_code = 200

    def __call__(self, url, **kwargs):
        return self

    def json(self):
        return {"version": "1.15.5", "title": "qdrant - vector search engine"}


# --- the incident -----------------------------------------------------------


def test_the_incident_itself():
    """Our child is dead; the port answers 200 with the pinned version.

    That is the whole of it. v3.1.0 returned True here and went on to adopt
    another installation's database.
    """
    answering = Answering()
    supervisor = QdrantSupervisor(
        binary_path="qdrant.exe", storage_path="/tmp/s",
        http_port=21500, grpc_port=21501,
        spawn=lambda cmd: DeadChild(), http_get=answering, sleep=lambda s: None,
        is_synced_path=lambda p: False,
    )
    supervisor.start()

    with pytest.raises(ManagedStartError) as caught:
        supervisor.wait_ready(timeout=5)

    message = str(caught.value)
    assert "exited" in message, message
    # The operator must learn that a RIVAL engine holds the port — not that
    # theirs was slow. Those have different causes and different fixes.
    assert "21500" in message and "another process" in message


def test_a_live_child_that_answers_is_accepted():
    """The fix must not break the ordinary case it protects."""
    supervisor = QdrantSupervisor(
        binary_path="qdrant.exe", storage_path="/tmp/s",
        http_port=21500, grpc_port=21501,
        spawn=lambda cmd: LiveChild(), http_get=Answering(), sleep=lambda s: None,
        is_synced_path=lambda p: False,
    )
    supervisor.start()

    assert supervisor.wait_ready(timeout=5) is True


def test_the_version_check_cannot_save_us():
    """Documents WHY the child check had to exist.

    Every instance ships the same pinned build, so a version match proves
    nothing about whose engine answered. This test asserts the weakness on
    purpose: if `verify_version` ever starts discriminating, the reasoning
    behind the child check needs revisiting.
    """
    supervisor = QdrantSupervisor(
        binary_path="qdrant.exe", storage_path="/tmp/s",
        http_port=21500, grpc_port=21501,
        spawn=lambda cmd: DeadChild(), http_get=Answering(), sleep=lambda s: None,
        is_synced_path=lambda p: False,
    )
    assert supervisor.verify_version() == "1.15.5"


def test_a_child_that_dies_while_we_wait_is_caught():
    """Bind failures are not always instant."""
    states = iter([None, None, 1, 1, 1, 1, 1, 1])

    class Dying:
        pid = 99

        def poll(self):
            return next(states, 1)

    class NotYet:
        status_code = 503

        def __call__(self, url, **kwargs):
            return self

    supervisor = QdrantSupervisor(
        binary_path="q", storage_path="/tmp/s", http_port=21500, grpc_port=21501,
        spawn=lambda cmd: Dying(), http_get=NotYet(), sleep=lambda s: None,
        is_synced_path=lambda p: False,
    )
    supervisor.start()

    with pytest.raises(ManagedStartError, match="exited"):
        supervisor.wait_ready(timeout=5)


# --- the API key ------------------------------------------------------------


def test_the_generated_config_carries_the_api_key():
    from ragtools.storage_managed import generate_qdrant_config

    cfg = generate_qdrant_config(storage_path="/s", http_port=1, grpc_port=2,
                                 api_key="secret-value")

    assert cfg["service"]["api_key"] == "secret-value"


def test_no_key_means_no_key_field():
    """An engine started without one must not be sent an empty string."""
    from ragtools.storage_managed import generate_qdrant_config

    cfg = generate_qdrant_config(storage_path="/s", http_port=1, grpc_port=2)

    assert "api_key" not in cfg["service"]


def test_the_key_travels_on_every_request():
    seen = []

    class Recorder:
        status_code = 200

        def __call__(self, url, headers=None):
            seen.append(headers)
            return self

        def json(self):
            return {"version": "1.15.5"}

    supervisor = QdrantSupervisor(
        binary_path="q", storage_path="/s", http_port=1, grpc_port=2,
        api_key="k", http_get=Recorder(), sleep=lambda s: None,
        is_synced_path=lambda p: False,
    )
    supervisor.wait_ready(timeout=1)
    supervisor.verify_version()

    assert seen and all(h == {"api-key": "k"} for h in seen), seen


def test_an_identity_is_generated_once_and_reused(tmp_path):
    settings = FakeSettings(tmp_path)

    first = own.engine_identity(settings)
    second = own.engine_identity(settings)

    assert first["api_key"] == second["api_key"], (
        "a key that changes every boot locks the installation out of its own engine"
    )
    assert first["instance_id"] == second["instance_id"]
    assert len(first["api_key"]) >= 32


def test_a_configured_instance_id_wins(tmp_path):
    own.engine_identity(FakeSettings(tmp_path))
    named = own.engine_identity(FakeSettings(tmp_path, instance_id="sandbox-a"))

    assert named["instance_id"] == "sandbox-a"


# --- ports ------------------------------------------------------------------


def test_ports_default_to_the_product_values(tmp_path):
    http, grpc, secondary = own.resolve_engine_ports(
        FakeSettings(tmp_path), default_http=21500, default_grpc=21501)

    assert (http, grpc, secondary) == (21500, 21501, False)


def test_config_can_move_the_ports(tmp_path):
    """v3.1.0 could not: they were reachable only through environment variables
    nothing set, which is why every instance targeted the same port."""
    settings = FakeSettings(tmp_path, qdrant_http_port=31500, qdrant_grpc_port=31501)

    http, grpc, _ = own.resolve_engine_ports(settings, default_http=21500,
                                             default_grpc=21501)

    assert (http, grpc) == (31500, 31501)


def test_the_environment_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_QDRANT_HTTP_PORT", "41500")
    settings = FakeSettings(tmp_path, qdrant_http_port=31500)

    http, _, _ = own.resolve_engine_ports(settings, default_http=21500,
                                          default_grpc=21501)

    assert http == 41500, "CI sets the environment; it must keep working"


def test_a_deliberate_secondary_must_say_so_twice(tmp_path):
    """Non-default ports alone, or an id alone, is an accident. Both is a decision."""
    ports_only = FakeSettings(tmp_path, qdrant_http_port=31500)
    id_only = FakeSettings(tmp_path, instance_id="sandbox")
    both = FakeSettings(tmp_path, qdrant_http_port=31500, instance_id="sandbox")

    assert own.resolve_engine_ports(ports_only, default_http=21500,
                                    default_grpc=21501)[2] is False
    assert own.resolve_engine_ports(id_only, default_http=21500,
                                    default_grpc=21501)[2] is False
    assert own.resolve_engine_ports(both, default_http=21500,
                                    default_grpc=21501)[2] is True


# --- the pre-spawn decision -------------------------------------------------


@pytest.fixture
def occupied():
    """A real listening socket, so `port_is_free` answers from the kernel.

    ``listen(16)``, not ``listen(1)``. With a backlog of one, the first probe
    fills the accept queue and the SECOND is refused — so a test that probes
    twice saw the same port as occupied and then free. That is the documented
    weakness of connect-probing, and it is real; it is just not what a running
    Qdrant looks like, and pinning it here would be testing the fixture. The
    weakness itself is covered by
    `test_a_wrongly_free_port_is_caught_by_the_child_check`.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    yield sock.getsockname()[1]
    sock.close()


def test_a_port_freed_moments_ago_reads_as_free():
    """The error the port check must NOT make.

    A false "occupied" is unrecoverable in the moment — managed mode refuses,
    the service degrades to embedded, and the index looks empty. A port our own
    engine released seconds ago is the ordinary restart case, and a `bind` probe
    calls it occupied on Linux and the BSDs while accepted connections linger in
    TIME_WAIT. That was tried here and reverted.
    """
    import socket as _socket
    import time as _time

    srv = _socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    client = _socket.socket()
    client.connect(("127.0.0.1", port))
    accepted, _ = srv.accept()
    client.sendall(b"x")
    accepted.recv(1)
    client.close()
    accepted.close()
    srv.close()
    _time.sleep(0.2)

    assert own.port_is_free(port) is True, (
        "a port released moments ago was reported occupied; an ordinary restart "
        "would refuse managed storage and degrade to an empty-looking index")


def test_a_wrongly_free_port_is_caught_by_the_child_check(tmp_path):
    """Why biasing toward "free" is safe, stated as a test.

    The probe is allowed to miss an occupied port — a wedged server whose accept
    backlog is full can refuse connections and read as free. Nothing is lost,
    because the spawn is self-verifying: our child cannot bind, it exits, and
    `wait_ready` refuses instead of adopting whatever is there. That is the D1
    fix doing the work the probe deliberately does not.
    """
    supervisor = QdrantSupervisor(
        binary_path="q", storage_path="/s", http_port=21500, grpc_port=21501,
        spawn=lambda cmd: DeadChild(), http_get=Answering(), sleep=lambda s: None,
        is_synced_path=lambda p: False,
    )
    supervisor.start()

    with pytest.raises(ManagedStartError, match="another process"):
        supervisor.wait_ready(timeout=2)


def test_a_live_listener_is_still_seen_as_occupied(tmp_path, occupied):
    """The ordinary case must keep working: a healthy engine holds its port."""
    assert own.port_is_free(occupied) is False
    assert own.inspect_port(FakeSettings(tmp_path), occupied).action == "refuse"


def test_the_boundary_holds_without_psutil(tmp_path, occupied, monkeypatch):
    """`psutil` is not a declared dependency, so proofs 3 and 4 are absent on a
    packaged install. The refusal must not depend on them."""
    monkeypatch.setattr(own, "listener_identity", lambda port: None)
    settings = FakeSettings(tmp_path)

    assert own.inspect_port(settings, occupied).action == "refuse"

    own.write_manifest(settings, own.EngineClaim(
        instance_id="me", pid=999_999, executable="q", storage_path="s",
        http_port=occupied, grpc_port=occupied + 1, started_at=0.0))
    assert own.inspect_port(settings, occupied).action == "refuse"


def test_a_free_port_is_spawned_on(tmp_path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    assert own.inspect_port(FakeSettings(tmp_path), port).action == "spawn"


def test_an_occupied_port_with_no_manifest_is_refused(tmp_path, occupied):
    """The decisive case. Something is there; we have no record of starting it;
    therefore it is not ours, and adopting it is what destroyed the boundary."""
    verdict = own.inspect_port(FakeSettings(tmp_path), occupied)

    assert verdict.action == "refuse"
    assert "no record" in verdict.reason


def test_an_occupied_port_whose_recorded_engine_is_dead_is_refused(tmp_path, occupied):
    settings = FakeSettings(tmp_path)
    own.write_manifest(settings, own.EngineClaim(
        instance_id="me", pid=999_999, executable="q", storage_path="s",
        http_port=occupied, grpc_port=occupied + 1, started_at=0.0))

    verdict = own.inspect_port(settings, occupied)

    assert verdict.action == "refuse"
    assert "gone" in verdict.reason or "another process" in verdict.reason


def test_our_own_live_engine_is_reattached_to(tmp_path, occupied, monkeypatch):
    settings = FakeSettings(tmp_path)
    own.write_manifest(settings, own.EngineClaim(
        instance_id="me", pid=4242, executable="q", storage_path="s",
        http_port=occupied, grpc_port=occupied + 1, started_at=0.0))
    monkeypatch.setattr(own, "process_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(own, "listener_identity", lambda port: (4242, "q"))

    assert own.inspect_port(settings, occupied).action == "reattach"


def test_a_manifest_for_a_different_port_does_not_vouch(tmp_path, occupied, monkeypatch):
    settings = FakeSettings(tmp_path)
    own.write_manifest(settings, own.EngineClaim(
        instance_id="me", pid=4242, executable="q", storage_path="s",
        http_port=occupied + 500, grpc_port=1, started_at=0.0))
    monkeypatch.setattr(own, "process_alive", lambda pid: True)

    assert own.inspect_port(settings, occupied).action == "refuse"


def test_an_unreadable_manifest_vouches_for_nothing(tmp_path, occupied):
    settings = FakeSettings(tmp_path)
    own.manifest_path(settings).write_text("{ not json", encoding="utf-8")

    assert own.read_manifest(settings) is None
    assert own.inspect_port(settings, occupied).action == "refuse"


# --- ownership verification -------------------------------------------------


def _claim(**kw):
    base = dict(instance_id="me", pid=100, executable="/opt/qdrant",
                storage_path="/s", http_port=21500, grpc_port=21501,
                started_at=0.0)
    base.update(kw)
    return own.EngineClaim(**base)


def test_a_dead_child_fails_verification():
    with pytest.raises(own.NotOurEngine, match="exited"):
        own.verify_ownership(_claim(), proc=DeadChild())


def test_a_foreign_listener_fails_verification(monkeypatch):
    monkeypatch.setattr(own, "listener_identity", lambda port: (777, "/other/qdrant"))

    with pytest.raises(own.NotOurEngine, match="777"):
        own.verify_ownership(_claim(), proc=LiveChild(pid=100))


def test_a_different_executable_fails_verification(monkeypatch):
    monkeypatch.setattr(own, "listener_identity",
                        lambda port: (100, "/somewhere/else/qdrant"))

    with pytest.raises(own.NotOurEngine, match="not the binary"):
        own.verify_ownership(_claim(), proc=LiveChild(pid=100))


def test_our_own_listener_passes(monkeypatch):
    monkeypatch.setattr(own, "listener_identity", lambda port: (100, "/opt/qdrant"))

    own.verify_ownership(_claim(), proc=LiveChild(pid=100))  # does not raise


def test_being_unable_to_look_is_not_a_failure(monkeypatch):
    """psutil missing, or permission denied. The API key still gates requests,
    so an unanswerable question must not block a legitimate start."""
    monkeypatch.setattr(own, "listener_identity", lambda port: None)

    own.verify_ownership(_claim(), proc=LiveChild(pid=100))


# --- termination ------------------------------------------------------------


def test_nothing_is_killed_without_a_manifest(tmp_path):
    assert "nothing" in own.release(FakeSettings(tmp_path), None)


def test_a_process_the_manifest_does_not_name_is_not_killed(tmp_path, monkeypatch):
    """The guarantee in one test: one installation must not stop another's
    database server, whatever is on the port."""
    killed = []
    monkeypatch.setattr(own, "process_alive", lambda pid: True)
    monkeypatch.setattr(own, "listener_identity", lambda port: (888, "/other"))
    monkeypatch.setattr("ragtools.platform.adapter",
                        lambda: types.SimpleNamespace(
                            terminate=lambda pid, force=False: killed.append(pid)))

    outcome = own.release(FakeSettings(tmp_path), _claim(pid=100))

    assert killed == [], "it terminated a process the manifest did not vouch for"
    assert "refusing" in outcome


def test_our_own_engine_is_stopped_and_the_record_cleared(tmp_path, monkeypatch):
    settings = FakeSettings(tmp_path)
    claim = _claim(pid=100)
    own.write_manifest(settings, claim)
    monkeypatch.setattr(own, "process_alive", lambda pid: True)
    monkeypatch.setattr(own, "listener_identity", lambda port: (100, "/opt/qdrant"))

    outcome = own.release(settings, claim, proc=None)

    assert "stopped engine pid=100" in outcome
    # Attribution: the shutdown line used to name nothing at all.
    assert "/opt/qdrant" in outcome and "21500" in outcome
    assert not own.manifest_path(settings).exists()


def test_an_already_dead_engine_is_reaped_quietly(tmp_path, monkeypatch):
    settings = FakeSettings(tmp_path)
    own.write_manifest(settings, _claim(pid=100))
    monkeypatch.setattr(own, "process_alive", lambda pid: False)

    assert "already exited" in own.release(settings, _claim(pid=100))
    assert not own.manifest_path(settings).exists()


# --- the manifest round trip ------------------------------------------------


def test_the_manifest_survives_a_round_trip(tmp_path):
    settings = FakeSettings(tmp_path)
    claim = _claim(pid=31337, storage_path=str(tmp_path / "qdrant-server"))

    own.write_manifest(settings, claim)

    assert own.read_manifest(settings) == claim


@pytest.mark.parametrize("backend,expect", [
    ("external", "managed outside ragtools"),
    ("embedded", "nothing to start"),
])
def test_a_server_we_do_not_own_gets_no_lifecycle(tmp_path, backend, expect):
    """`external` means "a server you run yourself". Nothing here may start,
    verify, adopt, key or stop it — and the plan refusing to start is what makes
    every later stage unreachable for it."""
    from ragtools.service.managed_qdrant import plan_managed_startup

    settings = FakeSettings(tmp_path)
    settings.storage_backend = backend

    plan = plan_managed_startup(settings)

    assert plan.should_start is False
    assert expect in plan.reason
    assert plan.api_key is None, "an engine we do not own was issued our key"


def test_an_external_engine_is_never_stopped(tmp_path, monkeypatch):
    """Belt and braces on the termination path itself."""
    from ragtools.service import managed_qdrant as mq

    settings = FakeSettings(tmp_path)
    settings.storage_backend = "external"
    monkeypatch.setattr(own, "process_alive", lambda pid: True)

    supervisor, url = mq.start_managed_qdrant(settings)

    assert (supervisor, url) == (None, None)
    assert not own.manifest_path(settings).exists()


def test_boot_refuses_a_port_held_by_a_stranger(tmp_path, occupied, monkeypatch):
    """End to end through the real boot function.

    The service must degrade to embedded rather than write into a store it does
    not own — and, because the refusal happens BEFORE the spawn, there is no
    failed child to clean up and no cleanup path that could reach the canonical
    engine.
    """
    from ragtools.service import managed_qdrant as mq

    settings = FakeSettings(tmp_path)
    spawned = []
    monkeypatch.setattr(mq, "plan_managed_startup", lambda s: mq.ManagedPlan(
        should_start=True, reason="test", binary="q",
        http_port=occupied, grpc_port=occupied + 1,
        storage_path=str(tmp_path / "qdrant-server"),
        url=f"http://127.0.0.1:{occupied}", api_key="k", instance_id="me"))
    monkeypatch.setattr(
        "ragtools.storage_managed.QdrantSupervisor",
        lambda **kw: spawned.append(kw))

    supervisor, url = mq.start_managed_qdrant(settings)

    assert (supervisor, url) == (None, None)
    assert spawned == [], "it spawned an engine onto an occupied port"
    assert not own.manifest_path(settings).exists(), (
        "it claimed ownership of a port it refused")


def test_boot_writes_a_manifest_on_a_clean_start(tmp_path, monkeypatch):
    from ragtools.service import managed_qdrant as mq

    settings = FakeSettings(tmp_path)
    free = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()

    class Supervisor:
        def __init__(self, **kw):
            self.kw = kw

        def start(self):
            return LiveChild(pid=5150)

        def wait_ready(self, timeout=60):
            return True

        def verify_version(self):
            return "1.15.5"

        def stop(self):
            pass

    monkeypatch.setattr("ragtools.storage_managed.QdrantSupervisor", Supervisor)
    monkeypatch.setattr(own, "listener_identity", lambda p: (5150, "/opt/q"))
    monkeypatch.setattr(mq, "plan_managed_startup", lambda s: mq.ManagedPlan(
        should_start=True, reason="test", binary="/opt/q",
        http_port=port, grpc_port=port + 1,
        storage_path=str(tmp_path / "qdrant-server"),
        url=f"http://127.0.0.1:{port}", api_key="k", instance_id="me"))

    supervisor, url = mq.start_managed_qdrant(settings)

    assert supervisor is not None and url.endswith(str(port))
    claim = own.read_manifest(settings)
    assert claim is not None and claim.pid == 5150
    assert claim.instance_id == "me" and claim.executable == "/opt/q"


def test_a_silent_fallback_to_embedded_is_explained():
    """A machine that fell back must not look like one configured that way.

    The reason existed as a local variable that was assigned, logged once and
    dropped — so `/health` reported `storage_backend: "embedded"` with an index
    that simply looked empty, and nothing anywhere said why. Refusing an
    occupied port makes this path more likely, which makes saying why part of
    the fix rather than a nicety.
    """
    import ragtools.service.app as app_module

    assert callable(app_module.storage_degradation)
    assert app_module.storage_degradation() == "", "a clean boot claims degradation"

    source = (Path(__file__).resolve().parents[1] / "src" / "ragtools" /
              "service" / "routes.py").read_text(encoding="utf-8")
    assert "storage_degraded_reason" in source, (
        "/health does not carry the reason the configured engine is not running")


def test_the_manifest_is_readable_by_a_human(tmp_path):
    """It is the evidence an operator reads when two services are fighting."""
    settings = FakeSettings(tmp_path)
    own.write_manifest(settings, _claim())

    body = json.loads(own.manifest_path(settings).read_text(encoding="utf-8"))

    assert set(body) >= {"instance_id", "pid", "executable", "storage_path",
                         "http_port", "grpc_port", "started_at"}
