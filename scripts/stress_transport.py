"""Prove the transport survives the scale that killed a 17-minute rebuild.

WP-R10. The v3.4 field failure was::

    qdrant_client.http.exceptions.ResponseHandlingException:
    [WinError 10048] Only one usage of each socket address
    (protocol/network address/port) is normally permitted

Nothing about the request was wrong. Windows' default dynamic port range is
**16,384** wide and a closed socket sits in TIME_WAIT for minutes, so a long
enough run walks the whole range and the next connect fails. The cause was that
``qdrant-client`` substitutes ``max_keepalive_connections=0`` when the host is
loopback — every request opening and closing a brand-new socket — and the fix is
:data:`ragtools.storage._HTTP_LIMITS` plus :mod:`ragtools.transport`.

Neither half was exercised by anything. No test reaches that scale, and the unit
tests for ``retry_call`` inject a fake sleep and a fake failure. So this script
exists to drive a REAL engine past the wall and MEASURE the thing that broke,
rather than observing that nothing crashed.

WHY IT RUNS ON ALL THREE PLATFORMS
----------------------------------
The failure was Windows-specific, so a Linux-only stress job would have proved
nothing about it. But the fix is not Windows-specific — the pool limits and the
retry allow-list are the same code everywhere — and a leak that appears on Linux
or macOS would be a regression in shared code. Running everywhere costs three
runners and removes an argument.

WHAT IS ACTUALLY MEASURED
-------------------------
Three signals, because "it did not crash" is not one:

* **S1 — distinct client-side ephemeral ports used to reach the engine.** This
  IS the exhaustion signal: with keep-alive the whole run fits in a pool-sized
  handful; without it the count tracks the request count. Sampled, so it is a
  lower bound, which is the safe direction — a lower bound that is already large
  is conclusive.
* **S2 — the driving process's open socket count.** Bounded pool in, bounded
  pool out. Growth here is a genuine descriptor leak rather than a port-range
  problem.
* **S3 — TIME_WAIT sockets against the engine port, system-wide.** The most
  direct evidence, and the least portable: enumerating other processes' sockets
  needs privileges macOS does not grant unelevated. When it cannot be taken it
  is reported as UNAVAILABLE and asserted on by nothing — never as ``0``, which
  is how a measurement that was not taken starts reading as a healthy result.

AND WHY THERE IS A NEGATIVE CONTROL
-----------------------------------
A leak detector that cannot detect a leak passes every run and proves nothing.
So ``--negative-control`` first drives a SHORT run through a client built the way
``qdrant-client`` builds one for loopback — keep-alive disabled, the v3.4
behaviour — and REQUIRES S1 to trip. If the control does not trip, the
measurement has no discriminating power and this script fails before it ever
reports on the real client.
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
# The repo root as well, so `scripts.fetch_qdrant` resolves whether this is run
# as `python scripts/stress_transport.py` (sys.path[0] is scripts/) or as
# `python -m scripts.stress_transport`.
sys.path.insert(0, str(ROOT))

#: The Windows default dynamic port range. A run that does not exceed it has not
#: reached the wall and cannot say anything about it.
EPHEMERAL_RANGE = 16384

#: The pool this product states (`ragtools.storage._HTTP_LIMITS`) is 32
#: connections. Allow generous slack for churn across a long run and for the
#: sampler catching a connection mid-replacement — the signal being tested for
#: is three orders of magnitude away, not a few percent.
MAX_DISTINCT_PORTS = 256

#: With keep-alive working, every request after the first few reuses a
#: connection. 20 is a deliberately weak floor: the observed ratio is in the
#: hundreds, and a threshold set near the observation is a threshold that fails
#: on a slow runner.
MIN_REUSE_FACTOR = 20

#: Sockets the driving process may still hold at the end, over its warm-up
#: baseline. The pool is 32; anything beyond this is not a pool.
MAX_SOCKET_GROWTH = 64


@dataclass
class Sampler:
    """Watch our own TCP connections to the engine while the load runs."""

    port: int
    interval: float = 0.01
    ports: set[int] = field(default_factory=set)
    peak_open: int = 0
    samples: int = 0
    error: str = ""
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def _sample(self) -> None:
        import psutil

        proc = psutil.Process(os.getpid())
        while not self._stop.is_set():
            try:
                conns = proc.net_connections(kind="tcp")
            except Exception as exc:                    # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
                return
            open_now = 0
            for conn in conns:
                raddr, laddr = conn.raddr, conn.laddr
                if raddr and getattr(raddr, "port", None) == self.port:
                    open_now += 1
                    if laddr and getattr(laddr, "port", None):
                        self.ports.add(laddr.port)
            self.peak_open = max(self.peak_open, open_now)
            self.samples += 1
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)


def sampling_is_possible() -> str:
    """Probe the measurement BEFORE spending twenty thousand requests on it.

    ``psutil.Process.net_connections()`` is unelevated for one's own process on
    all three platforms, but macOS is the one where per-process socket
    enumeration is most likely to be refused. Finding that out after the load
    has run wastes the run and, worse, presents as a mysterious late failure.
    Returns "" when the signal can be taken, or the reason it cannot.
    """
    try:
        import psutil
    except Exception as exc:                            # noqa: BLE001
        return f"psutil is not importable: {exc}"
    try:
        psutil.Process(os.getpid()).net_connections(kind="tcp")
    except Exception as exc:                            # noqa: BLE001
        return (f"this runner refuses per-process socket enumeration "
                f"({type(exc).__name__}: {exc}). S1 is the signal this gate "
                f"asserts on, so it cannot report a result here. Run the job "
                f"with the privileges psutil needs, or move this platform to a "
                f"best-effort leg with that decision written down — do not let "
                f"it pass without a measurement.")
    return ""


def open_sockets() -> int | None:
    """How many TCP sockets this process holds, or None if it cannot be taken."""
    try:
        import psutil

        return len(psutil.Process(os.getpid()).net_connections(kind="tcp"))
    except Exception:                                   # noqa: BLE001
        return None


def time_wait_against(port: int) -> int | None:
    """System-wide TIME_WAIT sockets to ``port``, or None when not measurable.

    None, never 0. A count that could not be taken is not a count of zero — see
    the ``_count_points`` rule in CLAUDE.md, which this repeats deliberately.
    """
    try:
        import psutil

        return sum(
            1 for c in psutil.net_connections(kind="tcp")
            if c.status == "TIME_WAIT" and c.raddr
            and getattr(c.raddr, "port", None) == port
        )
    except Exception:                                   # noqa: BLE001
        return None


def build_client(url: str, *, keepalive: bool):
    """The client under test, or the one the product used to get by default."""
    import httpx
    from qdrant_client import QdrantClient

    if keepalive:
        from ragtools.storage import _HTTP_LIMITS

        return QdrantClient(url=url, limits=_HTTP_LIMITS)
    # The negative control: exactly what `QdrantRemote.__init__` substitutes for
    # a loopback host, which is the configuration the incident ran under.
    return QdrantClient(
        url=url,
        limits=httpx.Limits(max_connections=None, max_keepalive_connections=0),
    )


@dataclass
class Outcome:
    requests: int
    seconds: float
    retries: int
    distinct_ports: int
    peak_open: int
    samples: int
    socket_growth: int | None
    time_wait: int | None
    sampler_error: str

    @property
    def reuse(self) -> float:
        return self.requests / max(1, self.distinct_ports)

    def describe(self, label: str) -> str:
        tw = "UNAVAILABLE (needs privileges this account does not have)" \
            if self.time_wait is None else str(self.time_wait)
        growth = "UNAVAILABLE" if self.socket_growth is None else str(self.socket_growth)
        return (
            f"  {label}\n"
            f"    requests            : {self.requests} in {self.seconds:.1f}s "
            f"({self.requests / max(self.seconds, 1e-9):.0f}/s)\n"
            f"    transport retries   : {self.retries}\n"
            f"    S1 distinct ports   : {self.distinct_ports} "
            f"(reuse factor {self.reuse:.1f}x, {self.samples} samples, "
            f"peak {self.peak_open} concurrent)\n"
            f"    S2 socket growth    : {growth}\n"
            f"    S3 TIME_WAIT to port: {tw}\n"
            + (f"    sampler error       : {self.sampler_error}\n"
               if self.sampler_error else "")
        )


def drive(url: str, port: int, *, requests: int, workers: int,
          keepalive: bool, dim: int = 8) -> Outcome:
    """Issue ``requests`` real Qdrant operations and measure the transport."""
    from qdrant_client.models import Distance, PointStruct, VectorParams

    from ragtools import transport

    client = build_client(url, keepalive=keepalive)
    collection = f"stress_{uuid.uuid4().hex[:8]}"
    client.create_collection(
        collection_name=collection,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )

    retries = 0
    lock = threading.Lock()
    real_sleep = time.sleep

    def counting_sleep(delay: float) -> None:
        nonlocal retries
        with lock:
            retries += 1
        real_sleep(delay)

    # Warm up so the baseline is "a pool that has been used", not "a process
    # that has not connected yet" — otherwise every measured socket looks new.
    # Never a zero vector: cosine distance is undefined for one and the server
    # refuses it, which would fail the gate for a reason with nothing to do
    # with the transport.
    for i in range(workers * 4):
        client.upsert(collection_name=collection, points=[
            PointStruct(id=1_000_000 + i, vector=[0.01 * (i + 1)] * dim,
                        payload={"warm": True})])
    baseline = open_sockets()

    sampler = Sampler(port=port)
    sampler.start()

    counter = {"n": 0}
    errors: list[BaseException] = []

    def work(worker: int) -> None:
        while True:
            with lock:
                if counter["n"] >= requests:
                    return
                counter["n"] += 1
                mine = counter["n"]
            try:
                # Alternating write and read, both real server round-trips.
                # A read-only load would not exercise the call site the
                # incident was raised from (`indexer.upsert_points`).
                if mine % 2:
                    vec = [0.1 + (mine % 97) / 97.0] * dim
                    transport.retry_call(
                        lambda: client.upsert(
                            collection_name=collection,
                            points=[PointStruct(id=mine, vector=vec,
                                                payload={"w": worker})]),
                        sleep=counting_sleep, describe="stress.upsert")
                else:
                    vec = [0.1 + (mine % 89) / 89.0] * dim
                    transport.retry_call(
                        lambda: client.query_points(
                            collection_name=collection, query=vec, limit=1),
                        sleep=counting_sleep, describe="stress.query")
            except BaseException as exc:                # noqa: BLE001
                with lock:
                    errors.append(exc)
                return

    started = time.time()
    threads = [threading.Thread(target=work, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - started

    time_wait = time_wait_against(port)
    sampler.stop()
    final = open_sockets()

    if errors:
        client.close()
        raise SystemExit(
            f"{len(errors)} request(s) failed after the retry budget was spent. "
            f"First: {type(errors[0]).__name__}: {errors[0]}")

    growth = None if (final is None or baseline is None) else final - baseline
    client.close()
    return Outcome(
        requests=counter["n"], seconds=elapsed, retries=retries,
        distinct_ports=len(sampler.ports), peak_open=sampler.peak_open,
        samples=sampler.samples, socket_growth=growth, time_wait=time_wait,
        sampler_error=sampler.error,
    )


def start_engine(binary: Path, root: Path, http_port: int, grpc_port: int):
    import httpx
    import yaml

    from ragtools.storage_managed import (
        QdrantSupervisor,
        generate_qdrant_config,
        resolve_qdrant_asset,
    )

    if resolve_qdrant_asset(platform.system(), platform.machine()) is None:
        # NOT a skip. A platform where the managed engine cannot be provisioned
        # is a platform where this gate cannot run, and a gate that excuses
        # itself is the thing WP-R10 exists to remove.
        raise SystemExit(
            f"no managed-engine asset for {platform.system()}/"
            f"{platform.machine()}; this stress gate cannot run here and will "
            f"not pretend otherwise.")

    storage = root / "storage"
    snapshots = root / "snapshots"
    storage.mkdir(parents=True, exist_ok=True)
    snapshots.mkdir(parents=True, exist_ok=True)
    config = generate_qdrant_config(
        storage_path=str(storage), http_port=http_port, grpc_port=grpc_port,
        snapshots_path=str(snapshots))
    config_path = root / "qdrant.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False),
                           encoding="utf-8")

    sup = QdrantSupervisor(
        binary_path=str(binary), storage_path=str(storage),
        http_port=http_port, grpc_port=grpc_port, config_path=str(config_path),
        data_dir=str(root), http_get=httpx.get, sleep=time.sleep)
    sup.start()
    if sup.wait_ready(timeout=90, interval=0.5) is not True:
        sup.stop()
        raise SystemExit("the pinned engine never became ready")
    return sup


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=None,
                        help="the pinned qdrant executable; fetched if omitted")
    parser.add_argument("--engine-dir", type=Path, default=Path("engine"),
                        help="where to fetch/find the engine (default: ./engine)")
    parser.add_argument("--root", type=Path, default=None,
                        help="working root for engine storage (default: temp)")
    parser.add_argument("--requests", type=int, default=20000,
                        help=f"must exceed {EPHEMERAL_RANGE}, the range v3.4 walked")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--http-port", type=int, default=26433)
    parser.add_argument("--grpc-port", type=int, default=26434)
    parser.add_argument("--negative-control", type=int, default=3000,
                        help="requests for the keep-alive-disabled control; "
                             "0 disables it (and with it, any proof the "
                             "measurement discriminates)")
    args = parser.parse_args(argv)

    if args.requests <= EPHEMERAL_RANGE:
        raise SystemExit(
            f"--requests {args.requests} does not exceed the {EPHEMERAL_RANGE}-wide "
            f"ephemeral range that v3.4 exhausted. A stress run below the wall "
            f"cannot say anything about the wall.")

    binary = args.binary
    if binary is None:
        # The ONE provisioning mechanism. `fetch_qdrant.py` already pins the
        # asset and verifies its sha256 against a digest committed here; a
        # second downloader would be a second thing to keep in step with
        # PINNED_QDRANT_VERSION.
        from scripts.fetch_qdrant import fetch

        binary = fetch(args.engine_dir.resolve())
    binary = Path(binary).resolve()
    if not binary.is_file():
        raise SystemExit(f"no engine at {binary}")

    import tempfile

    root = (args.root or Path(tempfile.mkdtemp(prefix="ragtools-stress-"))).resolve()
    root.mkdir(parents=True, exist_ok=True)

    print(f"engine   : {binary}")
    print(f"root     : {root}")
    print(f"platform : {platform.system()} {platform.machine()}")
    print(f"load     : {args.requests} requests across {args.workers} workers")

    blocked = sampling_is_possible()
    if blocked:
        print(f"\nTRANSPORT STRESS: FAILED before any load was applied\n"
              f"  * {blocked}")
        return 1
    print("measure  : per-process socket enumeration available")
    print("", flush=True)

    sup = start_engine(binary, root, args.http_port, args.grpc_port)
    url = sup.base_url
    failures: list[str] = []
    try:
        control: Outcome | None = None
        if args.negative_control > 0:
            print("NEGATIVE CONTROL — keep-alive disabled, the v3.4 configuration",
                  flush=True)
            control = drive(url, args.http_port, requests=args.negative_control,
                            workers=args.workers, keepalive=False)
            print(control.describe("control (max_keepalive_connections=0)"),
                  flush=True)

        print("UNDER TEST — the shipped pool (ragtools.storage._HTTP_LIMITS)",
              flush=True)
        result = drive(url, args.http_port, requests=args.requests,
                       workers=args.workers, keepalive=True)
        print(result.describe("shipped client"), flush=True)

        if result.sampler_error:
            failures.append(
                f"S1 could not be measured ({result.sampler_error}); this gate "
                f"asserts on a measurement, so an unmeasurable run fails rather "
                f"than passing on the absence of evidence.")
        elif result.samples < 10:
            failures.append(
                f"the sampler took only {result.samples} sample(s); S1 is a "
                f"lower bound and this one is not bounded by anything.")

        if control is not None and not result.sampler_error:
            # The control must trip the SAME threshold the real run must clear.
            # If it does not, the threshold is not discriminating and every
            # green run since would have been meaningless.
            tripped = (control.distinct_ports > MAX_DISTINCT_PORTS
                       or control.reuse < MIN_REUSE_FACTOR)
            if not tripped:
                failures.append(
                    f"the NEGATIVE CONTROL did not trip: {control.distinct_ports} "
                    f"distinct port(s), reuse {control.reuse:.1f}x, against a "
                    f"limit of {MAX_DISTINCT_PORTS} / {MIN_REUSE_FACTOR}x. A "
                    f"keep-alive-disabled client MUST look leaky; a measurement "
                    f"that cannot see the known-bad case cannot vouch for the "
                    f"good one.")

        if result.distinct_ports > MAX_DISTINCT_PORTS:
            failures.append(
                f"S1: {result.distinct_ports} distinct ephemeral ports for "
                f"{result.requests} requests (limit {MAX_DISTINCT_PORTS}). The "
                f"connection pool is not being reused — this is the shape that "
                f"exhausted the {EPHEMERAL_RANGE}-wide range in v3.4.")
        elif result.reuse < MIN_REUSE_FACTOR:
            failures.append(
                f"S1: reuse factor {result.reuse:.1f}x is below the "
                f"{MIN_REUSE_FACTOR}x floor.")

        if result.socket_growth is None:
            print("  note: S2 was not measurable on this runner; not asserted.",
                  flush=True)
        elif result.socket_growth > MAX_SOCKET_GROWTH:
            failures.append(
                f"S2: the driving process ended holding {result.socket_growth} "
                f"more socket(s) than its warmed-up baseline (limit "
                f"{MAX_SOCKET_GROWTH}). A bounded pool does not grow.")

        if result.time_wait is None:
            print("  note: S3 was not measurable on this runner; not asserted.",
                  flush=True)
    finally:
        sup.stop()

    print("", flush=True)
    if failures:
        print(f"TRANSPORT STRESS: FAILED — {len(failures)} problem(s)")
        for problem in failures:
            print(f"  * {problem}")
        return 1
    print(f"TRANSPORT STRESS: {args.requests} requests survived on "
          f"{platform.system()} with a bounded connection pool.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
