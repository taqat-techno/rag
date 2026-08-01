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
Four signals, because "it did not crash" is not one:

* **S0 — TCP connections the client opens to the engine.** Counted inside the
  process at the client's own transport, so it is COMPLETE rather than sampled
  and needs no privileges anywhere. This is connection CHURN, which is the
  quantity that walks the ephemeral range in the first place: keep-alive off
  opens one connection per request, keep-alive on opens a pool's worth for the
  whole run.

  It is counted AT THE CLIENT and not at ``socket.socket.connect``. See
  :class:`ConnectCounter` — the first version patched that class method, which
  is process-global, and the measurement then broke the thing it was measuring.
* **S1 — distinct client-side ephemeral ports used to reach the engine.** The
  WINDOWS exhaustion signature, and only that. Sampled, so it is a lower bound —
  the safe direction, since a lower bound that is already large is conclusive.
* **S2 — the driving process's open socket count.** Bounded pool in, bounded
  pool out. Growth here is a genuine descriptor leak rather than a port-range
  problem.
* **S3 — TIME_WAIT sockets against the engine port, system-wide, AS A DELTA
  across the run.** The most direct evidence of the mechanism and the least
  attributable: a TIME_WAIT socket has no owning process, so this is what the
  whole machine did, not what this run did. Reported, asserted on by nothing,
  and never ``0`` when it could not be taken — that is how a measurement that
  was not taken starts reading as a healthy result.

  It is a DELTA because an absolute count is not a measurement of the run.
  Run 30676422208 printed ``S3 TIME_WAIT to port: 3035`` for BOTH the 3,000-
  request control and the 20,000-request run under test — the same number for
  two different workloads, because TIME_WAIT lasts ~60 s and the second reading
  was still counting the first run's residue.

AND WHY THERE IS A NEGATIVE CONTROL
-----------------------------------
A leak detector that cannot detect a leak passes every run and proves nothing.
So ``--negative-control`` first drives a SHORT run through a client built the way
``qdrant-client`` builds one for loopback — keep-alive disabled, the v3.4
behaviour — and REQUIRES it to look leaky. If it does not, the measurement has no
discriminating power and this script fails before it ever reports on the real
client.

WHICH SIGNAL THE CONTROL MUST TRIP IS PER PLATFORM, AND IS DECLARED
-------------------------------------------------------------------
S1 is the Windows failure signature. ``[WinError 10048]`` is ephemeral-port
exhaustion under a 16,384-wide range with a long TIME_WAIT, and on Windows the
control produces it unmistakably: **1,743 distinct ports for 3,000 requests**.

Linux does not allocate that way. On ``ubuntu-latest`` the identical
keep-alive-disabled control produced **ONE** distinct port for 3,000 requests —
a reuse factor of 3000x, which is what a *healthy* client looks like on the S1
scale. So S1 cannot see the known-bad case there, and requiring the control to
trip it fails the job on a measurement problem rather than on a leak.

The answer is not a lower threshold off Windows — that would leave a control
that still cannot see the bad case, only more quietly. It is to say which signal
each platform's control is judged on (:data:`CONTROL_SIGNALS`), and to require
the control to trip EVERY signal designated for the platform it is running on.
S1 stays exactly as strict as it is on Windows, because Windows is where the
failure happened; S0 is designated everywhere, because it discriminates
everywhere.

AND WHY THE CONTROL'S OWN REQUEST FAILURES ARE NOT FATAL
--------------------------------------------------------
The control IS the v3.4 configuration. Being leaky and unreliable under churn is
the property it exists to demonstrate, so its failed requests are corroborating
evidence — counted, reported, and never a reason to end the run. Treating them
as fatal meant the better the control was at being broken, the more certainly it
took the gate with it: ``transport stress (macos-14)`` died in the control, before
the shipped client was measured at all, in three consecutive runs.

Failures in the run UNDER TEST stay fatal, because that is the thing being
tested and it must be clean. One rule, two subjects — :func:`failure_verdict`.

Tolerance alone would be a hole, so the control must also have COMPLETED
:data:`MIN_CONTROL_COMPLETION` of its load (:func:`control_problems`). A control
that collapsed on its first request would open one connection per request and
trip S0 on the arithmetic while having demonstrated nothing. It may be
unreliable; it may not be absent.
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

#: New TCP connections the shipped client may open across the whole run. Same
#: generosity as :data:`MAX_DISTINCT_PORTS` and for the same reason: the signal
#: being tested for is three orders of magnitude away, not a few percent.
MAX_CONNECTS = 256

#: Requests per connection opened. The mirror of :data:`MIN_REUSE_FACTOR`, on a
#: signal that is counted rather than sampled.
MIN_REQUESTS_PER_CONNECT = 20

#: The signal names, so a platform's designation and the failure message that
#: quotes it cannot drift apart.
SIGNAL_CONNECTS = "S0 (connections opened)"
SIGNAL_PORTS = "S1 (distinct ephemeral ports)"

#: Which signal(s) the NEGATIVE CONTROL must trip, per platform.
#:
#: S1 belongs to Windows and stays exactly as strict there: ``[WinError 10048]``
#: is ephemeral-port exhaustion, the control produces 1,743 distinct ports for
#: 3,000 requests, and that is the failure this whole gate exists for. On Linux
#: the identical control produced ONE distinct port for 3,000 requests — the
#: kernel reuses the local port for successive connections to the same peer — so
#: S1 there cannot tell keep-alive from no-keep-alive at all.
#:
#: S0 is designated everywhere because it is the same fact one layer up:
#: connections opened, counted completely, needing no privileges and no kernel
#: port-allocation policy to be visible.
CONTROL_SIGNALS: dict[str, tuple[str, ...]] = {
    "Windows": (SIGNAL_CONNECTS, SIGNAL_PORTS),
    "Linux": (SIGNAL_CONNECTS,),
    "Darwin": (SIGNAL_CONNECTS,),
}

#: An unlisted platform gets the signal that works without any platform
#: assumption. Never the empty tuple: "no designated signal" would mean "no
#: control", which is the state this file exists to make impossible.
DEFAULT_CONTROL_SIGNALS: tuple[str, ...] = (SIGNAL_CONNECTS,)

#: The fraction of its requested load the NEGATIVE CONTROL must actually
#: complete for its signals to be worth reading.
#:
#: Required BECAUSE its request failures are tolerated. Without it, tolerance
#: would be a hole: eight workers that each died on their first request would
#: report eight connections for eight requests, "trip" S0 on a ratio of 1.0 and
#: vouch for a gate that had measured nothing. Tolerating failures and requiring
#: completion are the same rule from two sides — the control may be unreliable,
#: it may not be absent.
MIN_CONTROL_COMPLETION = 0.9


def control_signals(system: str) -> tuple[str, ...]:
    return CONTROL_SIGNALS.get(system) or DEFAULT_CONTROL_SIGNALS


def describe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def failure_verdict(failures: int, first: str, *, dispatched: int,
                    tolerated: bool) -> str:
    """The message to die with, or ``""`` to carry on and report the number.

    WHOSE failures they are decides, and that is the whole of it.

    In the run UNDER TEST a failed request is the thing being tested for: the
    v3.4 incident was one socket error ending a 17-minute rebuild, so the
    shipped client must be clean and any failure stays fatal.

    In the NEGATIVE CONTROL it is the property the control EXISTS to
    demonstrate. That client is ``max_connections=None,
    max_keepalive_connections=0`` — a fresh socket per request, the
    configuration the incident ran under — and on macOS its own churn
    intermittently produces ``[Errno 9] Bad file descriptor``, a use-after-close
    (NOT descriptor exhaustion, which is ``EMFILE``). Errno 9 is correctly
    absent from :data:`ragtools.transport._RETRYABLE_ERRNOS`, so it was raised on
    the first attempt and ended the run.

    It ended the run on macos-14 in THREE consecutive runs — 30676422208 (2
    failures), 30680889672 (1) and 30692207245 (2) — under three different
    measurement regimes, including 30676422208, at which commit the script
    contained no client instrumentation of any kind. The condition is therefore
    a property of the control's configuration and not of anything measuring it,
    and treating it as fatal means the better the control is at being broken,
    the more certainly it takes the gate with it. Two failed requests in three
    thousand are corroborating evidence; they are reported, and counted.
    """
    if not failures or tolerated:
        return ""
    return (f"{failures} of {dispatched} request(s) failed — not retryable, or "
            f"the retry budget was spent. First: {first}")


def connection_pools(obj, max_depth: int = 8) -> list:
    """Every ``httpcore`` connection pool reachable from ``obj``.

    A bounded walk by TYPE rather than one hard-coded attribute path: the pool
    a ``QdrantClient`` drives sits six layers down
    (``_client.openapi_client.client._client._transport._pool``) behind private
    names on both sides of the boundary, and a spelled-out path is a silent zero
    the day either side renames one of them.
    """
    import httpcore

    found: list = []
    seen: set[int] = set()
    stack: list[tuple[object, int]] = [(obj, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, httpcore.ConnectionPool):
            found.append(node)
            continue                    # never walk INTO a pool's live sockets
        state = getattr(node, "__dict__", None)
        if isinstance(state, dict):
            stack.extend((v, depth + 1) for v in state.values())
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, (list, tuple, set, frozenset)):
            stack.extend((v, depth + 1) for v in node)
    return found


class _CountingBackend:
    """An ``httpcore`` network backend that delegates, and counts.

    The full sync ``NetworkBackend`` surface — ``connect_tcp``,
    ``connect_unix_socket``, ``sleep`` — forwarded verbatim. It creates no
    socket, holds no socket, and closes no socket; the stream it returns is the
    one the real backend built.
    """

    def __init__(self, inner, port: int, counter: "ConnectCounter") -> None:
        self._inner = inner
        self._port = port
        self._counter = counter

    def connect_tcp(self, host, port, timeout=None, local_address=None,
                    socket_options=None):
        if port == self._port:
            self._counter.bump()
        return self._inner.connect_tcp(
            host, port, timeout=timeout, local_address=local_address,
            socket_options=socket_options)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options)

    def sleep(self, seconds):
        return self._inner.sleep(seconds)


class ConnectCounter:
    """Count the TCP connections ONE CLIENT opens to the engine.

    Counted at the client's own transport — ``httpcore``'s network backend,
    which every connection the pool opens goes through — so it is still
    COMPLETE rather than sampled and still needs no privileges. S1 has to infer
    connection churn from whatever its sampler happened to catch (33 samples
    across a 4 s run on Linux); this counts every one, and it counts the thing
    that actually exhausts a port range.

    WHY NOT ``socket.socket.connect``, WHICH IS WHAT IT DID FIRST
    ------------------------------------------------------------
    That is a process-global class attribute. Every socket in every thread —
    the client under test, the engine health check, anything a library does —
    went through one Python function taking one shared lock, swapped in and out
    by a plain assignment while eight worker threads were connecting through it.

    Run 30680889672 is the receipt. The NEGATIVE CONTROL, which is the
    keep-alive-disabled configuration and therefore opens a fresh connection for
    every one of its 3,000 requests, failed on two of three platforms with the
    same condition in each platform's spelling — an operation issued on a
    handle that is no longer a socket::

        windows-latest : [WinError 10038] An operation was attempted on
                         something that is not a socket
        macos-14       : [Errno 9] Bad file descriptor

    Neither had appeared in the run before the wrapper existed, and the wrapper
    was the only new code inside the load. It did NOT reproduce on a developer
    Windows box at the same load, with or without an injected delay in the
    wrapper — so the mechanism is attributed, not demonstrated, and the reason
    to remove the patch does not rest on pinning it: **a measurement that breaks
    the thing it measures is worse than no measurement**, and the property S0
    was introduced for — complete, unprivileged, independent of kernel port-
    allocation policy — is not what required a process-global patch. Only the
    layer did.

    So it is attached to the client, and ``attach`` RAISES when it can find no
    pool to attach to. A counter that quietly counts nothing reports zero
    connections for twenty thousand requests, which is the healthiest result
    this gate can print.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self._count = 0
        self._lock = threading.Lock()
        self._restore: list[tuple[object, object]] = []

    def bump(self) -> None:
        with self._lock:
            self._count += 1

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def attach(self, client) -> "ConnectCounter":
        """Instrument every connection pool ``client`` owns. Returns self."""
        pools = connection_pools(client)
        if not pools:
            raise SystemExit(
                f"S0 could not be measured: no httpcore connection pool was "
                f"found inside {type(client).__name__}, so nothing would be "
                f"counted. Reporting 0 connections for a whole run is the "
                f"healthiest number this gate can print, so it refuses instead.")
        for pool in pools:
            inner = pool._network_backend
            pool._network_backend = _CountingBackend(inner, self.port, self)
            self._restore.append((pool, inner))
        return self

    def detach(self) -> None:
        while self._restore:
            pool, inner = self._restore.pop()
            pool._network_backend = inner

    def __enter__(self) -> "ConnectCounter":
        return self

    def __exit__(self, *_exc) -> None:
        self.detach()


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
    #: Requests that COMPLETED. `requests + failures` is what was dispatched.
    requests: int
    seconds: float
    retries: int
    connects: int
    distinct_ports: int
    peak_open: int
    samples: int
    socket_growth: int | None
    time_wait_before: int | None
    time_wait_after: int | None
    sampler_error: str
    #: Requests that raised and were tolerated rather than fatal. Always 0 for
    #: the run under test, which does not tolerate any.
    failures: int = 0
    first_failure: str = ""

    @property
    def dispatched(self) -> int:
        return self.requests + self.failures

    @property
    def reuse(self) -> float:
        return self.requests / max(1, self.distinct_ports)

    @property
    def per_connect(self) -> float:
        """Requests served per connection OPENED. 1.0x is one per request."""
        return self.requests / max(1, self.connects)

    @property
    def time_wait_delta(self) -> int | None:
        """What THIS run added, not what the machine happens to be holding.

        Reading the absolute count twice is what made run 30676422208 print 3035
        for a 3,000-request control and 3035 again for a 20,000-request run: the
        second reading was still counting the first's residue, ~60 s of it.
        """
        if self.time_wait_before is None or self.time_wait_after is None:
            return None
        return self.time_wait_after - self.time_wait_before

    def trips(self, signal: str) -> bool:
        """Does this run look leaky on ``signal``?

        The control MUST answer yes and the shipped client MUST answer no, on
        exactly the same thresholds. Two different comparisons would mean the
        control vouched for something other than what is being asserted.
        """
        if signal == SIGNAL_CONNECTS:
            return self.connects > MAX_CONNECTS or self.per_connect < MIN_REQUESTS_PER_CONNECT
        if signal == SIGNAL_PORTS:
            return self.distinct_ports > MAX_DISTINCT_PORTS or self.reuse < MIN_REUSE_FACTOR
        raise ValueError(f"no such signal: {signal}")

    def explain(self, signal: str) -> str:
        if signal == SIGNAL_CONNECTS:
            return (f"{self.connects} connection(s) opened for {self.requests} "
                    f"request(s) ({self.per_connect:.1f} per connection), against a "
                    f"limit of {MAX_CONNECTS} / {MIN_REQUESTS_PER_CONNECT}x")
        return (f"{self.distinct_ports} distinct port(s), reuse {self.reuse:.1f}x, "
                f"against a limit of {MAX_DISTINCT_PORTS} / {MIN_REUSE_FACTOR}x")

    def describe(self, label: str) -> str:
        delta = self.time_wait_delta
        if delta is None:
            tw = "UNAVAILABLE (needs privileges this account does not have)"
        else:
            tw = (f"{delta:+d} across the run "
                  f"({self.time_wait_before} -> {self.time_wait_after}, system-wide)")
        growth = "UNAVAILABLE" if self.socket_growth is None else str(self.socket_growth)
        return (
            f"  {label}\n"
            f"    requests            : {self.requests} in {self.seconds:.1f}s "
            f"({self.requests / max(self.seconds, 1e-9):.0f}/s)\n"
            + (f"    failed requests     : {self.failures} of {self.dispatched} "
               f"dispatched — first: {self.first_failure}\n"
               if self.failures else "")
            + f"    transport retries   : {self.retries}\n"
            f"    S0 connections open : {self.connects} "
            f"({self.per_connect:.1f} requests per connection)\n"
            f"    S1 distinct ports   : {self.distinct_ports} "
            f"(reuse factor {self.reuse:.1f}x, {self.samples} samples, "
            f"peak {self.peak_open} concurrent)\n"
            f"    S2 socket growth    : {growth}\n"
            f"    S3 TIME_WAIT to port: {tw}\n"
            + (f"    sampler error       : {self.sampler_error}\n"
               if self.sampler_error else "")
        )


def control_problems(control: Outcome, *, requested: int,
                     designated: tuple[str, ...],
                     skip: tuple[str, ...] = ()) -> list[str]:
    """Everything wrong with the NEGATIVE CONTROL, in the gate's own words.

    Two independent requirements, and each is why the other is not enough.

    * It must TRIP every signal its platform designates. A measurement that
      cannot see the known-bad case cannot vouch for the good one.
    * It must have COMPLETED enough of its load for that trip to mean anything.
      Its request failures are tolerated (see :func:`failure_verdict`), and
      tolerance without this is a hole: a control that died on request one would
      still show one connection per request and trip S0 on the arithmetic alone.
    """
    problems: list[str] = []
    completion = control.requests / max(1, requested)
    if completion < MIN_CONTROL_COMPLETION:
        problems.append(
            f"the NEGATIVE CONTROL completed {control.requests} of {requested} "
            f"request(s) ({completion:.0%}, {control.failures} failed"
            + (f", first: {control.first_failure}" if control.first_failure else "")
            + f"). Below {MIN_CONTROL_COMPLETION:.0%} it has not applied the load "
            f"its signals are read from, and an unmeasurable control cannot "
            f"vouch for anything.")
    for signal in designated:
        if signal in skip:
            continue
        if not control.trips(signal):
            problems.append(
                f"the NEGATIVE CONTROL did not trip {signal}: "
                f"{control.explain(signal)}. A keep-alive-disabled client MUST "
                f"look leaky; a measurement that cannot see the known-bad case "
                f"cannot vouch for the good one.")
    return problems


def drive(url: str, port: int, *, requests: int, workers: int,
          keepalive: bool, dim: int = 8,
          tolerate_request_failures: bool = False) -> Outcome:
    """Issue ``requests`` real Qdrant operations and measure the transport.

    ``tolerate_request_failures`` is set for the NEGATIVE CONTROL and for
    nothing else — see :func:`failure_verdict` for why the same event is fatal
    in one run and expected in the other.
    """
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
    # Taken here, AFTER the warm-up and BEFORE the load, so what is reported is
    # what this run did rather than what the machine was already holding.
    time_wait_before = time_wait_against(port)

    sampler = Sampler(port=port)
    sampler.start()
    # AFTER the warm-up: what is being measured is the connections this RUN
    # opens, not the pool it inherited. Attached to this client, so nothing else
    # in the process is instrumented and nothing else can inflate the count.
    connect_counter = ConnectCounter(port).attach(client)

    counter = {"n": 0, "done": 0}
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
                with lock:
                    counter["done"] += 1
            except BaseException as exc:                # noqa: BLE001
                with lock:
                    errors.append(exc)
                # A tolerated failure costs one request, not the worker. The
                # control has to keep applying load for its signals to be worth
                # reading, and a worker that retires on its first error takes an
                # eighth of the load with it.
                if not tolerate_request_failures:
                    return

    started = time.time()
    try:
        threads = [threading.Thread(target=work, args=(w,)) for w in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        # Restored whatever happened, so the client is handed back exactly as it
        # was found — including on the path where the load raises.
        connect_counter.detach()
    elapsed = time.time() - started

    time_wait_after = time_wait_against(port)
    sampler.stop()
    final = open_sockets()

    first_failure = describe_error(errors[0]) if errors else ""
    verdict = failure_verdict(len(errors), first_failure,
                              dispatched=counter["n"],
                              tolerated=tolerate_request_failures)
    if verdict:
        client.close()
        raise SystemExit(verdict)

    growth = None if (final is None or baseline is None) else final - baseline
    client.close()
    return Outcome(
        requests=counter["done"], seconds=elapsed, retries=retries,
        connects=connect_counter.count,
        distinct_ports=len(sampler.ports), peak_open=sampler.peak_open,
        samples=sampler.samples, socket_growth=growth,
        time_wait_before=time_wait_before, time_wait_after=time_wait_after,
        sampler_error=sampler.error,
        failures=len(errors), first_failure=first_failure,
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

    designated = control_signals(platform.system())
    print(f"control  : must trip {', '.join(designated)}")

    blocked = sampling_is_possible()
    if blocked:
        if SIGNAL_PORTS in designated:
            print(f"\nTRANSPORT STRESS: FAILED before any load was applied\n"
                  f"  * {blocked}")
            return 1
        # S1 is not what this platform's control is judged on, so its absence
        # costs a reported number, not the run. S0 needs no privileges at all.
        print(f"measure  : S1/S2/S3 unavailable here ({blocked.splitlines()[0]}); "
              f"not designated on {platform.system()}, so not asserted")
    else:
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
                            workers=args.workers, keepalive=False,
                            tolerate_request_failures=True)
            print(control.describe("control (max_keepalive_connections=0)"),
                  flush=True)
            if control.failures:
                print(f"  note: {control.failures} of {control.dispatched} control "
                      f"request(s) failed. EXPECTED, and reported rather than "
                      f"fatal: this client is the v3.4 configuration and being "
                      f"unreliable under churn is the property it is here to "
                      f"demonstrate. Corroborating evidence, not noise.",
                      flush=True)

        print("UNDER TEST — the shipped pool (ragtools.storage._HTTP_LIMITS)",
              flush=True)
        result = drive(url, args.http_port, requests=args.requests,
                       workers=args.workers, keepalive=True)
        print(result.describe("shipped client"), flush=True)

        if SIGNAL_PORTS in designated:
            if result.sampler_error:
                failures.append(
                    f"S1 could not be measured ({result.sampler_error}); it is a "
                    f"designated signal on {platform.system()}, so an unmeasurable "
                    f"run fails rather than passing on the absence of evidence.")
            elif result.samples < 10:
                failures.append(
                    f"the sampler took only {result.samples} sample(s); S1 is a "
                    f"lower bound and this one is not bounded by anything.")

        if control is not None:
            # EVERY signal this platform designates, on exactly the thresholds
            # the shipped run must clear, PLUS the completion floor that keeps
            # tolerated request failures from becoming a way to pass. If a
            # designated signal cannot see the known-bad case, the gate is not
            # discriminating and every green run since would have been
            # meaningless — which is why this is a failure and not a warning.
            failures.extend(control_problems(
                control, requested=args.negative_control, designated=designated,
                # S1 unmeasurable is already reported above, as its own failure.
                skip=(SIGNAL_PORTS,) if result.sampler_error else ()))

        if result.connects > MAX_CONNECTS:
            failures.append(
                f"S0: {result.connects} connection(s) opened for "
                f"{result.requests} requests (limit {MAX_CONNECTS}). The "
                f"connection pool is not being reused — this is the churn that "
                f"exhausted the {EPHEMERAL_RANGE}-wide range in v3.4.")
        elif result.per_connect < MIN_REQUESTS_PER_CONNECT:
            failures.append(
                f"S0: {result.per_connect:.1f} request(s) per connection is below "
                f"the {MIN_REQUESTS_PER_CONNECT}x floor.")

        if SIGNAL_PORTS not in designated:
            print(f"  note: S1 is not a designated signal on {platform.system()} "
                  f"— the control there produces ~1 distinct port for 3,000 "
                  f"keep-alive-disabled requests, so it cannot discriminate. "
                  f"Reported, not asserted.", flush=True)
        elif result.distinct_ports > MAX_DISTINCT_PORTS:
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

        if result.time_wait_delta is None:
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
