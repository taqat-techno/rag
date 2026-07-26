"""The registries are read from every service thread — they must survive it.

Found by running the wired system, not by the unit tests: the first real
indexing job on the per-project path died with

    ProgrammingError: SQLite objects created in a thread can only be used in
    that same thread. The object was created in thread id 28020 and this is
    thread id 50556.

A registry is opened once at service startup and then consulted by the request
thread (status, search), the job worker (indexing) and the watcher. Every
isolated test was single-threaded, so nothing caught it.

Two things are required, and both are tested here:
  1. ``check_same_thread=False`` — otherwise any cross-thread use raises;
  2. a lock spanning execute AND fetch — otherwise a cursor is consumed while
     another thread is mid-statement, which is the actual corruption risk that
     ``check_same_thread`` exists to prevent.

Plan: docs/planning/RAG_COLLECTION_ARCHITECTURE_IMPLEMENTATION_PLAN.md (W10)
"""

import threading

import pytest

from ragtools.registry import FrameworkRegistry, ProjectRegistry


@pytest.fixture
def registry(tmp_path):
    reg = ProjectRegistry(str(tmp_path / "registry.db"))
    for i in range(8):
        reg.add(f"proj-{i}", path=str(tmp_path / f"p{i}"), mode="docs")
    yield reg
    reg.close()


@pytest.fixture
def frameworks(tmp_path):
    fw = FrameworkRegistry(str(tmp_path / "frameworks.db"))
    yield fw
    fw.close()


def _run_concurrently(fn, threads=8, iterations=25):
    """Run ``fn`` on N threads; surface the first exception from any of them."""
    errors: list[BaseException] = []
    barrier = threading.Barrier(threads)

    def worker(n):
        try:
            barrier.wait(timeout=10)   # maximise overlap
            for i in range(iterations):
                fn(n, i)
        except BaseException as exc:  # noqa: BLE001 — re-raised in the main thread
            errors.append(exc)

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    if errors:
        raise errors[0]


def test_reads_from_many_threads_do_not_raise(registry):
    """The exact failure the live indexing job hit."""
    _run_concurrently(lambda n, i: registry.get(f"proj-{i % 8}"))


def test_list_from_many_threads_does_not_raise(registry):
    _run_concurrently(lambda n, i: registry.list())


def test_concurrent_reads_and_writes_are_serialised(registry, tmp_path):
    """Writers and readers overlap without a 'database is locked' or a torn read."""
    def work(n, i):
        if n % 2 == 0:
            registry.move(f"proj-{n}", str(tmp_path / f"moved-{n}-{i}"))
        else:
            rec = registry.get(f"proj-{n}")
            assert rec is not None
            assert rec.collection_name.startswith("proj_")

    _run_concurrently(work, threads=8, iterations=15)


def test_framework_lookup_is_thread_safe(frameworks):
    """`framework_collections_for` is on the SEARCH path — the hottest read."""
    rec, _ = frameworks.register(name="odoo", version="19.0", edition="ce",
                                 build_id="b1", canonical_root="/srv/odoo")
    for i in range(8):
        frameworks.link(f"uuid-{i}", rec.collection_name)

    _run_concurrently(
        lambda n, i: frameworks.framework_collections_for(f"uuid-{n}")
    )


def test_registry_survives_use_from_a_non_creating_thread(tmp_path):
    """Open on one thread, use on another — the literal reported error."""
    holder = {}

    def create():
        holder["reg"] = ProjectRegistry(str(tmp_path / "cross.db"))
        holder["reg"].add("alpha", path=str(tmp_path / "a"), mode="docs")

    t = threading.Thread(target=create)
    t.start()
    t.join(timeout=10)

    try:
        # This is the call that used to raise ProgrammingError.
        rec = holder["reg"].get("alpha")
        assert rec is not None and rec.project_id == "alpha"
    finally:
        holder["reg"].close()


def test_close_is_idempotent_and_releases_the_file(tmp_path):
    """An unclosed handle keeps the .db locked on Windows."""
    path = tmp_path / "closeme.db"
    reg = ProjectRegistry(str(path))
    reg.add("alpha", path=str(tmp_path / "a"))
    reg.close()
    reg.close()          # must not raise
    path.unlink()        # would raise PermissionError if the handle leaked


def test_registry_is_usable_as_a_context_manager(tmp_path):
    path = tmp_path / "ctx.db"
    with ProjectRegistry(str(path)) as reg:
        reg.add("alpha", path=str(tmp_path / "a"))
    path.unlink()
