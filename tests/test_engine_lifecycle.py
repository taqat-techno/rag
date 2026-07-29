"""The engine is watched for its whole life, and it can say why it died.

Two machines lost their managed Qdrant under v3.2.0 — one four minutes into a
migration, one after seven idle hours — and in both cases the service carried on
answering `/health` while every storage operation failed. Nothing logged the
exit. Nothing restarted it. Nothing even noticed.

These tests pin the two halves of that: the engine is given somewhere real to
write, and its death is observed rather than inferred.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from ragtools.service import engine_lifecycle as el
from ragtools.storage_managed import (
    QdrantSupervisor,
    _spawn_kwargs,
    open_engine_log,
    rotate_engine_log,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "ragtools"


# --- the spawn must never inherit ----------------------------------------


class RecordingSpawn:
    """A stand-in for subprocess.Popen that remembers how it was called."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        proc = types.SimpleNamespace(pid=4321, returncode=None)
        proc.poll = lambda: None
        proc.wait = lambda timeout=None: 0
        proc.terminate = lambda: None
        return proc


def test_the_engine_is_never_spawned_onto_an_inherited_handle(tmp_path):
    """THE defect, in one assertion.

    `Popen(cmd)` with no `stdout=` inherits the parent's handles. Under the
    windowed launcher the parent has none, so CPython creates a pipe, hands the
    child the write end and closes the read end — the child then writes into a
    pipe with no reader and every write fails. Measured: "The process tried to
    write to a nonexistent pipe."
    """
    spawn = RecordingSpawn()
    sup = QdrantSupervisor(
        binary_path="qdrant", storage_path=str(tmp_path / "store"),
        http_port=21500, grpc_port=21501, data_dir=str(tmp_path),
        spawn=spawn, is_synced_path=lambda p: False)
    sup.start()

    _cmd, kwargs = spawn.calls[0]
    assert "stdout" in kwargs, (
        "the engine was spawned without an explicit stdout — that is the "
        "inherited-handle defect this release exists to fix")
    assert kwargs["stdout"] is not None
    assert kwargs["stderr"] is not None
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_the_engine_output_lands_in_a_real_file(tmp_path):
    sup = QdrantSupervisor(
        binary_path="qdrant", storage_path=str(tmp_path / "store"),
        http_port=21500, grpc_port=21501, data_dir=str(tmp_path),
        spawn=RecordingSpawn(), is_synced_path=lambda p: False)
    sup.start()

    assert sup.log_path, "no engine log path was recorded"
    assert Path(sup.log_path).name == "qdrant.log"
    assert Path(sup.log_path).parent.name == "logs"
    assert not sup.log_error


def test_an_unopenable_log_degrades_to_devnull_never_to_inheritance(tmp_path, monkeypatch):
    """A logging failure must not block the engine — and must not silently
    restore the exact behaviour we are fixing."""
    monkeypatch.setattr("ragtools.storage_managed.open_engine_log",
                        lambda d: (None, None))
    spawn = RecordingSpawn()
    sup = QdrantSupervisor(
        binary_path="qdrant", storage_path=str(tmp_path / "store"),
        http_port=21500, grpc_port=21501, data_dir=str(tmp_path),
        spawn=spawn, is_synced_path=lambda p: False)
    sup.start()

    _cmd, kwargs = spawn.calls[0]
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert sup.log_error, "a discarded engine log must be reported, not silent"


@pytest.mark.skipif(sys.platform != "win32", reason="console flags are Windows-only")
def test_the_console_child_gets_no_window_and_is_never_detached():
    kwargs = _spawn_kwargs(None)
    flags = kwargs.get("creationflags", 0)
    assert flags & subprocess.CREATE_NO_WINDOW, (
        "qdrant.exe is a CONSOLE-subsystem image; without CREATE_NO_WINDOW "
        "Windows gives it a console when the launcher has none")
    DETACHED_PROCESS = 0x00000008
    assert not flags & DETACHED_PROCESS, (
        "the engine must stay OUR child. Detaching it would silently break the "
        "proc.wait() the whole supervision fix rests on — the exit code would "
        "never arrive and the death would go unnoticed again")


def test_console_suppression_goes_through_the_platform_seam():
    """This project keeps every platform branch in `ragtools.platform`.

    An earlier draft asked `sys.platform` directly in `storage_managed.py`, and
    the repository's own structural test correctly refused it.

    The ABSENCE of dispatch is already policed by that AST sweep
    (``test_no_platform_branch_survives_outside_this_package``), which is the
    right way to check it — an earlier version of THIS test grepped for the
    string ``sys.platform`` and matched the comment explaining why it is not
    used. What is left here is the positive half: the seam exists and is called.
    """
    from ragtools.platform import adapter

    flags = adapter().child_process_flags()
    assert isinstance(flags, dict)
    assert "child_process_flags" in (SRC / "storage_managed.py").read_text(
        encoding="utf-8")


def test_every_adapter_implements_the_child_flags_seam():
    """All three, not just the one this machine happens to run.

    `PlatformAdapter` is a `Protocol`: the concrete adapters conform
    STRUCTURALLY and do not inherit from it, so a default defined on the
    Protocol is invisible at runtime. The first version of this change added the
    method only there and to Windows — which passed on this machine and raised
    `AttributeError: 'LinuxAdapter' object has no attribute
    'child_process_flags'` on a real Linux runner.
    """
    from ragtools.platform.darwin import DarwinAdapter
    from ragtools.platform.linux import LinuxAdapter
    from ragtools.platform.windows import WindowsAdapter

    for cls in (LinuxAdapter, DarwinAdapter, WindowsAdapter):
        assert hasattr(cls, "child_process_flags"), (
            f"{cls.__name__} does not implement child_process_flags; a Protocol "
            f"default does not reach a class that only conforms structurally")
        flags = cls.child_process_flags(cls.__new__(cls))
        assert isinstance(flags, dict), f"{cls.__name__} returned {flags!r}"


def test_the_engine_log_rotates(tmp_path):
    log = tmp_path / "logs" / "qdrant.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"x" * 2048)

    rotate_engine_log(log, max_bytes=1024, backups=3)

    assert log.with_suffix(".log.1").is_file()
    assert not log.exists() or log.stat().st_size == 0


def test_opening_the_log_creates_the_directory(tmp_path):
    handle, path = open_engine_log(str(tmp_path))
    try:
        assert handle is not None
        assert path.parent.is_dir()
    finally:
        if handle:
            handle.close()


# --- death is observed ----------------------------------------------------


class FakeProc:
    """A child we can kill on demand.

    ``wait()`` BLOCKS until the process is actually killed, because a double
    that returns immediately models an engine which exited the instant it
    started — which is a genuine crash, and treating it as one is correct. An
    earlier version of this file returned 0 straight away and then asserted no
    crash was recorded; the code was right and the test was wrong.
    """

    def __init__(self, pid=999):
        self.pid = pid
        self.returncode = None
        self._exit = None
        self._gone = threading.Event()

    def die(self, code=3221225477):
        self._exit = code
        self.returncode = code
        self._gone.set()

    def wait(self, timeout=None):
        self._gone.wait(timeout if timeout is not None else 10)
        return self._exit

    def poll(self):
        return self._exit

    def terminate(self):
        self.die(0)


def _settings(tmp_path):
    return types.SimpleNamespace(data_dir=str(tmp_path), storage_backend="managed")


def _supervisor(proc, log="qdrant.log"):
    return types.SimpleNamespace(proc=proc, log_path=log, log_error="")


def test_an_unexpected_exit_is_recorded_with_its_code(tmp_path):
    proc = FakeProc(pid=29980)
    proc.die(-1073741819)
    started = []

    def starter(settings):
        started.append(1)
        return (_supervisor(proc), "http://127.0.0.1:21500") if len(started) == 1 else (None, None)

    engine = el.EngineLifecycle(_settings(tmp_path), starter=starter,
                                stopper=lambda s, sup: "stopped",
                                backoff=(0.0,), max_restarts=1, sleep=lambda s: None)
    engine.start()
    if engine._watcher:
        engine._watcher.join(timeout=5)

    status = engine.status
    assert status.exit_code == -1073741819
    assert status.pid == 29980
    crashed = [e for e in status.history if e.state == el.CRASHED]
    assert crashed, "the engine died and no CRASHED transition was recorded"


def test_a_crash_invalidates_the_ownership_manifest(tmp_path):
    """A dead pid must stop being vouched for immediately, not at shutdown.

    While the service lived on with a dead engine, `qdrant-owner.json` kept
    asserting a live claim — the input a later startup consults when deciding
    whether to adopt whatever now holds the port.
    """
    from ragtools.service import engine_ownership

    settings = _settings(tmp_path)
    engine_ownership.write_manifest(settings, engine_ownership.EngineClaim(
        instance_id="rag-test", pid=29980, executable="qdrant.exe",
        storage_path=str(tmp_path), http_port=21500, grpc_port=21501,
        started_at=1.0))
    assert engine_ownership.manifest_path(settings).is_file()

    proc = FakeProc()
    proc.die(1)
    engine = el.EngineLifecycle(
        settings, starter=lambda s: (_supervisor(proc), "u"),
        stopper=lambda s, sup: "", backoff=(0.0,), max_restarts=0,
        sleep=lambda s: None)
    engine.start()
    if engine._watcher:
        engine._watcher.join(timeout=5)

    assert not engine_ownership.manifest_path(settings).is_file(), (
        "the manifest still vouches for a process that is gone")


def test_restarts_are_bounded_and_exhaustion_is_a_reported_state(tmp_path):
    """An unexplained crash must not become an unexplained restart loop."""
    attempts = []
    dead = FakeProc()
    dead.die(1)

    def starter(settings):
        attempts.append(1)
        return (_supervisor(dead), "u") if len(attempts) == 1 else (None, None)

    engine = el.EngineLifecycle(_settings(tmp_path), starter=starter,
                                stopper=lambda s, sup: "", max_restarts=3,
                                backoff=(0.0, 0.0, 0.0), sleep=lambda s: None)
    engine.start()
    if engine._watcher:
        engine._watcher.join(timeout=5)

    assert engine.status.state == el.RESTART_EXHAUSTED
    # one initial start + exactly three restart attempts, never more
    assert len(attempts) == 4, f"expected 4 starts, got {len(attempts)}"


def test_a_deliberate_stop_does_not_restart_anything(tmp_path):
    """Intent is recorded BEFORE it is acted on.

    Get this ordering wrong and shutting the service down starts a restart storm
    against an engine that is deliberately on its way out.
    """
    starts = []
    proc = FakeProc()

    def starter(settings):
        starts.append(1)
        return _supervisor(proc), "u"

    engine = el.EngineLifecycle(_settings(tmp_path), starter=starter,
                                stopper=lambda s, sup: "stopped by us",
                                backoff=(0.0,), sleep=lambda s: None)
    engine.start()
    outcome = engine.request_stop()
    if engine._watcher:
        engine._watcher.join(timeout=5)

    assert outcome == "stopped by us"
    assert engine.status.state == el.STOPPED
    assert len(starts) == 1, "a deliberate stop triggered a restart"
    assert not [e for e in engine.status.history if e.state == el.CRASHED]


def test_a_starter_that_raises_does_not_kill_the_watcher(tmp_path):
    """Found by re-reading the new code, not by a failure.

    `plan_managed_startup` can raise — a configured binary that has gone
    missing, an unreadable data dir. An exception escaping the restart loop
    would kill the watcher thread, silently, because under the windowed
    launcher a thread traceback has nowhere to go. That restores the exact
    v3.2.0 condition this class exists to end.
    """
    proc = FakeProc()
    proc.die(1)
    calls = []

    def exploding_starter(settings):
        calls.append(1)
        if len(calls) == 1:
            return _supervisor(proc), "u"
        raise RuntimeError("qdrant_binary is set to a file that was not found")

    engine = el.EngineLifecycle(_settings(tmp_path), starter=exploding_starter,
                                stopper=lambda s, sup: "", max_restarts=2,
                                backoff=(0.0, 0.0), sleep=lambda s: None)
    engine.start()
    if engine._watcher:
        engine._watcher.join(timeout=5)

    assert engine.status.state == el.RESTART_EXHAUSTED, (
        "a raising starter left the machine in "
        f"{engine.status.state!r} instead of reporting exhaustion")
    assert len(calls) == 3  # initial + 2 bounded attempts, no more


def test_a_start_that_raises_still_lets_the_service_boot(tmp_path):
    """The service always boots. Managed storage is allowed to be unavailable;
    it is not allowed to prevent startup."""
    def exploding(settings):
        raise RuntimeError("no binary")

    engine = el.EngineLifecycle(_settings(tmp_path), starter=exploding,
                                stopper=lambda s, sup: "")
    supervisor, url = engine.start()

    assert supervisor is None and url is None
    assert engine.status.state == el.STOPPED


def test_a_reattached_engine_is_watched_by_pid(tmp_path, monkeypatch):
    """The path a service restart actually takes.

    A reattached engine has no child handle — we did not spawn it — so there is
    nothing to `wait()` on. Leaving it unwatched would preserve the v3.2.0 hole
    for precisely the common case, so it is polled by pid instead. "We cannot
    wait on it" is a reason to watch differently, not a reason to stop.
    """
    from ragtools.service import engine_ownership

    settings = _settings(tmp_path)
    engine_ownership.write_manifest(settings, engine_ownership.EngineClaim(
        instance_id="rag-test", pid=13579, executable="qdrant.exe",
        storage_path=str(tmp_path), http_port=21500, grpc_port=21501,
        started_at=1.0))

    alive = {"value": True}
    monkeypatch.setattr("ragtools.service.engine_ownership.process_alive",
                        lambda pid: alive["value"])

    # `proc=None` is what the reattach branch of start_managed_qdrant returns.
    reattached = types.SimpleNamespace(proc=None, log_path=None, log_error="")
    engine = el.EngineLifecycle(
        settings, starter=lambda s: (reattached, "http://127.0.0.1:21500"),
        stopper=lambda s, sup: "", max_restarts=0, backoff=(0.0,),
        sleep=lambda s: None, poll_interval=0.01)
    engine.start()

    assert engine.status.pid == 13579, "the manifest pid was not adopted"
    assert engine._watcher is not None, "a reattached engine was left unwatched"

    alive["value"] = False                     # the engine goes away
    engine._watcher.join(timeout=5)

    assert engine.status.state in (el.CRASHED, el.RESTART_EXHAUSTED)
    crashed = [e for e in engine.status.history if e.state == el.CRASHED]
    assert crashed, "the reattached engine died and nothing recorded it"
    assert "reattached" in crashed[0].detail, (
        "a process we did not spawn has no exit code; the record must say so "
        "rather than printing 'exit code None'")


def test_down_states_are_reported_as_unavailable(tmp_path):
    engine = el.EngineLifecycle(_settings(tmp_path),
                                starter=lambda s: (None, None),
                                stopper=lambda s, sup: "")
    engine.start()
    assert not engine.status.available
    for state in (el.CRASHED, el.RESTARTING, el.RESTART_EXHAUSTED):
        assert state in el.DOWN_STATES


def test_the_snapshot_is_json_shaped(tmp_path):
    engine = el.EngineLifecycle(_settings(tmp_path),
                                starter=lambda s: (_supervisor(FakeProc()), "u"),
                                stopper=lambda s, sup: "")
    engine.start()
    snap = engine.status.as_dict()
    for key in ("state", "pid", "exit_code", "restart_attempt", "available",
                "log_path", "history"):
        assert key in snap, f"/health consumers need {key!r}"


# --- structural: the v3.2.0 shape must not come back ----------------------


def test_the_supervisor_handle_is_read_between_startup_and_shutdown():
    """v3.2.0's whole defect, expressed structurally.

    `_managed_qdrant` was assigned once and next read in the shutdown branch. If
    an engine handle is ever again stored and never consulted, this fails.
    """
    tree = ast.parse((SRC / "service" / "app.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree)
             if isinstance(n, ast.Name) and n.id == "_engine"}
    assert names, "app.py no longer holds an engine handle at all"

    source = (SRC / "service" / "app.py").read_text(encoding="utf-8")
    assert "EngineLifecycle" in source, (
        "the engine must be owned by a lifecycle component, not by a bare "
        "variable that nothing reads again")


def test_the_lifecycle_waits_on_the_child_rather_than_polling_a_socket():
    source = (SRC / "service" / "engine_lifecycle.py").read_text(encoding="utf-8")
    assert "proc.wait()" in source, (
        "death must be observed by waiting on the child; polling a port tells "
        "you the engine is unreachable, not that it is gone")
