"""Surviving the transport, instead of merely not provoking it.

A 17-minute rebuild of the installed knowledge base died on ONE socket error::

    qdrant_client.http.exceptions.ResponseHandlingException:
    [WinError 10048] Only one usage of each socket address
    (protocol/network address/port) is normally permitted

raised out of ``indexer.upsert_points`` -> ``client.upsert``. Nothing about that
request was wrong. Windows had run out of ephemeral ports: the default dynamic
range is 16,384 wide and a closed socket sits in TIME_WAIT for minutes
afterwards, so a long enough run walks the whole range and the next connect
fails. The very next attempt would have worked. Instead the run ended.

Two ideas, in priority order:

1. **Survive it.** A transient socket error must cost a retry, not a rebuild.
   That is this module.
2. **Stop causing it.** :mod:`ragtools.storage` now states the HTTP connection
   pool explicitly, because qdrant-client's default for a *localhost* server is
   ``max_keepalive_connections=0`` — every single request opens and closes a
   brand-new socket, which is precisely how a range 16,384 wide gets exhausted.

Prevention is the secondary objective on purpose. It narrows the window; it
cannot close it, because the port range is shared with every other process on
the machine. Only (1) makes the failure survivable.

**Why the allow-list is explicit, and why that matters more than the retry.**
A wrong retry policy is worse than no retry at all. Retrying blindly turns a
mistake that fails in 200 ms into one that fails twenty seconds later wearing a
network error's clothes, and the operator now has to disprove a transport
problem that never existed. A dimension mismatch, a rejected API key, a
malformed filter, a 400 — these are *answers*. The server understood the
request and refused it, and it will refuse it identically four more times.

So :func:`is_retryable` NAMES what may be retried and refuses everything else,
including exception types a future client version invents. New transient class
observed in the field? Add it here, with the evidence.
"""

from __future__ import annotations

import errno
import logging
import random
import time
from typing import Any, Callable, TypeVar

import httpx
from qdrant_client.http.exceptions import ResponseHandlingException

logger = logging.getLogger("ragtools.transport")

T = TypeVar("T")

#: Windows socket errors that mean "try again", by WSA code:
#:
#: * 10048 WSAEADDRINUSE   — the ephemeral range is exhausted (the incident)
#: * 10053 WSAECONNABORTED — the local stack dropped a connection mid-flight
#: * 10054 WSAECONNRESET   — peer reset, e.g. an idle keep-alive it had closed
#: * 10060 WSAETIMEDOUT    — the connect timed out
#:
#: Read via ``getattr(exc, "winerror", None)``: the attribute does not exist on
#: POSIX, and this classifier runs on all three platforms.
_RETRYABLE_WINERRORS = frozenset({10048, 10053, 10054, 10060})

#: The same four conditions by errno, which is how they arrive on POSIX. These
#: constants resolve per-platform at import — on Windows they ARE the WSA codes
#: above (``errno.EADDRINUSE == 10048``), on Linux they are 98/103/104/110 — so
#: this set is the portable half of the pair, not a duplicate of it.
_RETRYABLE_ERRNOS = frozenset({
    errno.EADDRINUSE,
    errno.ECONNRESET,
    errno.ECONNABORTED,
    errno.ETIMEDOUT,
})

#: HTTP statuses that describe the server's CAPACITY rather than the request.
#: Every other 4xx/5xx is an answer about this request and is not retried — a
#: 400 or a 409 will be a 400 or a 409 again.
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})

#: httpx failures that happened to the connection rather than to the request.
#: Deliberately enumerated instead of catching ``httpx.TransportError``: that
#: base also covers ``ProtocolError`` variants raised by malformed traffic, and
#: a blanket parent silently adopts whatever a future httpx adds beneath it.
_RETRYABLE_HTTPX: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

#: Attempts INCLUDING the first. 5 covers a TIME_WAIT squeeze — the ports come
#: back on a timer — without turning a genuine outage into a long hang.
DEFAULT_ATTEMPTS = 5
DEFAULT_BASE_DELAY = 0.25
DEFAULT_MAX_DELAY = 8.0

#: 2**32 seconds is already past any sane cap; refuse to compute past it so a
#: caller passing a silly attempt count gets a clamp, not an OverflowError.
_MAX_SHIFT = 32


def _unwrap(exc: BaseException) -> BaseException:
    """Resolve a ``ResponseHandlingException`` to the failure it wraps.

    This wrapper is raised at TWO sites in qdrant-client with opposite meanings
    (``http/api_client.py``):

    * ``send_inner`` wraps anything the transport raised — genuinely transient,
      and the shape the WinError 10048 incident arrived in;
    * ``_parse`` wraps a pydantic ``ValidationError`` when a 2xx body does not
      match the expected model — a client/server schema mismatch that will
      reproduce exactly, forever.

    Classifying the wrapper itself cannot tell those apart, so "retry
    ``ResponseHandlingException``" would quietly retry a schema mismatch five
    times and report it as a network problem. Classify what is inside it.
    """
    seen = 0
    while isinstance(exc, ResponseHandlingException) and seen < 4:
        source = getattr(exc, "source", None)
        if not isinstance(source, BaseException):
            break
        exc = source
        seen += 1
    return exc


def is_retryable(exc: BaseException) -> bool:
    """Whether ``exc`` is a transport hiccup worth attempting again.

    An ALLOW-list. Anything not named here is treated as an answer from the
    server and re-raised immediately, so a bad request fails in one round-trip
    rather than after five sleeps wearing a transient error's disguise.
    """
    effective = _unwrap(exc)

    # Unwrapped to a bare wrapper with nothing inside. The only site that can
    # produce that is the transport send, so it is transient by construction.
    if isinstance(effective, ResponseHandlingException):
        return True

    if isinstance(effective, _RETRYABLE_HTTPX):
        return True

    if isinstance(effective, OSError):
        if getattr(effective, "winerror", None) in _RETRYABLE_WINERRORS:
            return True
        if effective.errno in _RETRYABLE_ERRNOS:
            return True

    # A 429 that carried Retry-After arrives as its own class in some client
    # versions; identify it by the header it preserved, not by an import that
    # moved between 1.14 and 1.16.
    if getattr(effective, "retry_after_s", None) is not None:
        return True

    status = getattr(effective, "status_code", None)
    if isinstance(status, int):
        return status in _RETRYABLE_STATUS

    return False


def backoff_cap(
    attempt: int,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
) -> float:
    """Upper bound on the wait before retry ``attempt`` (0-based).

    Exposed so the growth of the schedule can be asserted directly. The delay
    actually slept is a uniform sample from ``[0, cap]`` — see
    :func:`retry_call` — so the sequence of sleeps is deliberately NOT
    monotonic and cannot be pinned by a test.
    """
    return min(max_delay, base_delay * (2 ** min(attempt, _MAX_SHIFT)))


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    sleep: Callable[[float], Any] = time.sleep,
    describe: str = "",
) -> T:
    """Call ``fn``, retrying only :func:`is_retryable` failures.

    Backoff is exponential with FULL JITTER — ``uniform(0, cap)`` rather than
    ``cap`` — because the retry is provoked by *contention for a shared
    resource*. Every indexing worker that hits ephemeral-port exhaustion hits it
    at the same moment, and a fixed backoff sends them all back at the same
    moment too. Jitter is what stops the retry from rebuilding the pile-up it
    is recovering from.

    ``sleep`` is injectable so tests exercise the real schedule at zero
    wall-clock cost; nothing in production passes it.

    Raises the last exception once ``attempts`` is spent — a bounded failure,
    never a silent one, and never an unbounded loop.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")

    label = describe or getattr(fn, "__name__", "call")

    for attempt in range(attempts):
        try:
            return fn()
        # Not BaseException: a KeyboardInterrupt or a cancellation is the
        # operator asking us to stop, and retrying it would ignore them.
        except Exception as exc:  # noqa: BLE001 — re-raised unless allow-listed
            if not is_retryable(exc):
                raise
            if attempt >= attempts - 1:
                logger.warning(
                    "%s: giving up after %d attempts (%s: %s)",
                    label, attempts, type(exc).__name__, exc,
                )
                raise
            delay = random.uniform(0.0, backoff_cap(attempt, base_delay, max_delay))
            logger.warning(
                "%s: attempt %d/%d failed (%s: %s) — retrying in %.2fs",
                label, attempt + 1, attempts, type(exc).__name__, exc, delay,
            )
            sleep(delay)

    # Unreachable: the final iteration either returns or re-raises.
    raise RuntimeError(f"{label}: retry loop exited without a result")
