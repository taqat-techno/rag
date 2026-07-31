"""One transient socket error must not cost a 17-minute rebuild.

The incident: a full rebuild of the installed knowledge base died in
``indexer.upsert_points`` -> ``client.upsert`` on a single

    ResponseHandlingException: [WinError 10048] Only one usage of each socket
    address (protocol/network address/port) is normally permitted

Windows' dynamic port range is 16,384 wide and a closed socket lingers in
TIME_WAIT for minutes, so a long run walks the range and the next connect
fails. The request was fine. The next attempt would have worked. Nothing
retried, so the run ended there.

``_DELETE_BATCH`` in the indexer already names this exact failure class — the
DELETE path was hardened against provoking it and the UPSERT path never was,
and neither path could survive one. Batching narrows the window; it cannot
close it, because the port range belongs to the whole machine.

These tests pin both halves of the fix and, more importantly, pin the SHAPE of
the retry policy. A blanket retry would satisfy "it survives the socket error"
while quietly making every genuine rejection — a dimension mismatch, a bad key,
a 400 — take five sleeps and arrive disguised as a network fault. The
non-retryable tests assert an exact call count of 1 so that regression cannot
pass.
"""

from __future__ import annotations

import errno
import random

import httpx
import pytest
from qdrant_client.http.exceptions import (
    ResponseHandlingException,
    UnexpectedResponse,
)
from qdrant_client.models import PointStruct

from ragtools.chunking.common import make_chunk_id
from ragtools.indexing import indexer
from ragtools.indexing.indexer import upsert_points
from ragtools.transport import backoff_cap, is_retryable, retry_call


# --- helpers ---------------------------------------------------------------


def _winsock_error(winerror: int, *, errno_: int | None = None) -> OSError:
    """The OSError the Windows socket stack raises, built portably.

    On Windows the real exception carries the WSA code in BOTH ``errno`` and
    ``winerror`` (``errno.EADDRINUSE == 10048`` there). On POSIX there is no
    ``winerror`` member at all — but an exception instance has a ``__dict__``,
    so the attribute can still be set, and the classifier reads it with
    ``getattr``. Callers here pass a deliberately unrelated ``errno_`` so the
    assertion can only pass through the ``winerror`` branch, on every platform.
    """
    exc = OSError(
        winerror if errno_ is None else errno_,
        "Only one usage of each socket address "
        "(protocol/network address/port) is normally permitted",
    )
    exc.winerror = winerror
    return exc


def _dimension_mismatch() -> UnexpectedResponse:
    """A 400 the server MEANT. It will mean it again in 200ms, and in 20s."""
    return UnexpectedResponse(
        status_code=400,
        reason_phrase="Bad Request",
        content=b'{"status":{"error":"Vector dimension error: expected dim: 384, got 768"}}',
        headers=httpx.Headers(),
    )


def _point(index: int, *, dims: int = 8, text: str = "chunk text") -> PointStruct:
    return PointStruct(
        id=make_chunk_id("proj", "docs/guide.md", index),
        vector=[0.0] * dims,
        payload={"text": text, "file_path": "docs/guide.md", "project_id": "proj"},
    )


class _FlakyClient:
    """Counts upserts and fails the first ``fail_times`` of them.

    Deliberately not a MagicMock: the properties under test are the call COUNT
    and the points each call carried, and a mock would happily pretend to both.
    """

    def __init__(self, fail_times: int = 0, error: BaseException | None = None):
        self.calls = 0
        self.batches: list[list] = []
        self._remaining = fail_times
        self._error = error if error is not None else _winsock_error(10048)

    def upsert(self, *, collection_name, points):
        self.calls += 1
        self.batches.append(list(points))
        if self._remaining > 0:
            self._remaining -= 1
            raise self._error
        return "acknowledged"


def _never_sleeps(_seconds: float) -> None:
    """Injected in place of ``time.sleep``: the schedule runs, the clock doesn't."""


# --- classification: what may be retried -----------------------------------


def test_a_winerror_10048_is_retryable():
    """The exact error that ended the rebuild must be recognised as transient.

    Asserted through the ``winerror`` branch alone — the fixture's ``errno`` is
    ``EINVAL``, which is in no retryable set on any platform — so this passes on
    Linux and macOS for the same reason it passes on Windows, rather than
    passing on Windows by the coincidence that ``errno.EADDRINUSE`` is 10048
    there and failing everywhere else.
    """
    assert is_retryable(_winsock_error(10048, errno_=errno.EINVAL))


def test_the_incident_arrives_wrapped_and_is_still_recognised():
    """WinError 10048 reached us inside a qdrant wrapper, not bare.

    The traceback said ``ResponseHandlingException``; the socket error was the
    ``source`` it carried. A classifier that only understood bare OSErrors would
    have looked correct in isolation and retried nothing in production.
    """
    wrapped = ResponseHandlingException(_winsock_error(10048, errno_=errno.EINVAL))
    assert is_retryable(wrapped)
    assert is_retryable(ResponseHandlingException(httpx.ConnectError("connect failed")))


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connect failed"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.ReadTimeout("read timed out"),
        httpx.WriteTimeout("write timed out"),
        httpx.PoolTimeout("no pool slot"),
        httpx.RemoteProtocolError("server closed the keep-alive connection"),
        _winsock_error(10053, errno_=errno.EINVAL),
        _winsock_error(10054, errno_=errno.EINVAL),
        _winsock_error(10060, errno_=errno.EINVAL),
        OSError(errno.ECONNRESET, "connection reset by peer"),
        OSError(errno.ETIMEDOUT, "connection timed out"),
    ],
)
def test_connection_level_failures_are_retryable(exc):
    """Everything that happened to the CONNECTION rather than to the request.

    ``RemoteProtocolError`` earns its place specifically: now that the client
    keeps connections alive, it will occasionally check out one the server has
    already closed on its own idle timeout. Leaving that off the list would
    trade port exhaustion for a new intermittent failure.
    """
    assert is_retryable(exc)


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_capacity_statuses_are_retryable(status):
    assert is_retryable(
        UnexpectedResponse(status_code=status, reason_phrase="", content=b"",
                           headers=httpx.Headers())
    )


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 500, 501])
def test_answers_about_the_request_are_not_retryable(status):
    """A rejection is an answer. Sleeping on it five times changes nothing.

    500 and 501 are in this list on purpose: an unhandled server-side error or
    an unimplemented endpoint is deterministic for the same request, unlike the
    502/503/504 that describe a gateway or capacity condition.
    """
    assert not is_retryable(
        UnexpectedResponse(status_code=status, reason_phrase="", content=b"",
                           headers=httpx.Headers())
    )


def test_a_schema_mismatch_inside_the_wrapper_is_not_retryable():
    """``ResponseHandlingException`` is raised at two sites that mean opposites.

    ``send_inner`` wraps a transport failure — transient. ``_parse`` wraps a
    pydantic ``ValidationError`` when a 2xx body does not match the model — a
    client/server schema mismatch that reproduces forever. "Retry
    ResponseHandlingException" reads as a precise allow-list and is in fact a
    blanket one: it would sleep five times over a permanent version skew and
    then report it as a network problem.
    """
    from pydantic import BaseModel, ValidationError

    class _Model(BaseModel):
        count: int

    try:
        _Model(count="not a number")
    except ValidationError as ve:
        validation_error = ve

    assert not is_retryable(ResponseHandlingException(validation_error))


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("malformed filter"),
        KeyError("collection_name"),
        RuntimeError("something else entirely"),
        OSError(errno.ENOENT, "no such file"),
    ],
)
def test_unknown_failures_are_not_retryable(exc):
    """The list is an ALLOW-list, so anything new fails fast by default.

    A future client version inventing an exception class must not be adopted
    into the retry policy by accident.
    """
    assert not is_retryable(exc)


# --- retry_call: the policy ------------------------------------------------


def test_a_transient_failure_is_retried_and_the_upsert_succeeds():
    """The whole point: the run continues instead of ending.

    One WinError 10048 on the first attempt used to abort a 17-minute rebuild
    and lose every project after it. Here it costs one extra request.
    """
    client = _FlakyClient(fail_times=1)
    points = [_point(i) for i in range(10)]

    written = upsert_points(client, "proj_x", points, sleep=_never_sleeps)

    assert written == 10
    assert client.calls == 2, "the failed attempt was not retried"


def test_a_non_retryable_upsert_fails_on_the_first_attempt():
    """Exactly ONE call — so a future blanket retry turns this test red.

    A dimension mismatch means the collection was built under a different
    embedding model. Retrying it wastes four backoffs and then reports the
    schema error as a transport error, which is the one place an operator will
    not look for it.
    """
    client = _FlakyClient(fail_times=99, error=_dimension_mismatch())

    with pytest.raises(UnexpectedResponse):
        upsert_points(client, "proj_x", [_point(0)], sleep=_never_sleeps)

    assert client.calls == 1, (
        f"{client.calls} attempts for a 400 — a rejected request must fail fast"
    )


def test_attempts_are_bounded():
    """A permanent outage must end the call, not occupy the thread forever."""
    calls = {"n": 0}

    def _always_fails():
        calls["n"] += 1
        raise _winsock_error(10048, errno_=errno.EINVAL)

    with pytest.raises(OSError):
        retry_call(_always_fails, attempts=4, sleep=_never_sleeps)

    assert calls["n"] == 4, "the attempt budget is not being honoured"


def test_the_last_exception_is_what_surfaces():
    """Exhaustion must re-raise the real failure, not a wrapper hiding it."""
    final = _winsock_error(10048, errno_=errno.EINVAL)

    def _always_fails():
        raise final

    with pytest.raises(OSError) as caught:
        retry_call(_always_fails, attempts=2, sleep=_never_sleeps)

    assert caught.value is final


def test_backoff_grows_and_is_jittered():
    """Fixed backoff re-synchronises the pile-up it is recovering from.

    Ephemeral-port exhaustion is contention for a machine-wide resource: every
    caller hits it in the same instant, and a fixed delay sends them all back in
    the same instant too. Full jitter — ``uniform(0, cap)``, not ``cap`` —
    spreads the second wave out.

    So the growth lives in the CAP, and "is it jittered" cannot be asserted by
    "are the delays different from each other": a plain exponential backoff also
    produces 0.25, 0.5, 1.0, ... which are all different. (A first draft of this
    test asserted exactly that and stayed green against a no-jitter mutant.)
    The two properties that actually separate them are that each delay lands
    STRICTLY BELOW its cap instead of on it, and that two callers retrying at
    the same moment get DIFFERENT schedules.
    """
    caps = [backoff_cap(n, 0.25, 8.0) for n in range(8)]
    assert caps[:6] == [0.25, 0.5, 1.0, 2.0, 4.0, 8.0], "backoff is not exponential"
    assert caps[6] == caps[7] == 8.0, "the cap is unbounded"

    def _always_fails():
        raise _winsock_error(10048, errno_=errno.EINVAL)

    def _schedule() -> list[float]:
        slept: list[float] = []
        with pytest.raises(OSError):
            retry_call(_always_fails, attempts=8, base_delay=0.25, max_delay=8.0,
                       sleep=slept.append)
        return slept

    random.seed(20260729)
    first = _schedule()
    second = _schedule()          # not reseeded: this is the next caller along

    assert len(first) == 7, "one sleep per retry, none after the final attempt"

    for n, delay in enumerate(first):
        assert delay <= 8.0, "a delay exceeded max_delay"
        assert 0.0 <= delay < caps[n], (
            f"sleep {n} landed on its cap ({delay}) — that is the un-jittered "
            "schedule, not a sample from it"
        )

    assert first != second, (
        "two callers retrying at the same moment got an identical schedule — "
        "a fixed backoff rebuilds the very pile-up it is recovering from"
    )
    assert max(first[4:]) > max(first[:2]), "later waits are not longer"


def test_no_sleep_happens_when_the_call_succeeds():
    slept: list[float] = []
    assert retry_call(lambda: "ok", sleep=slept.append) == "ok"
    assert slept == []


# --- idempotence: why a retry is safe with no bookkeeping ------------------


def test_chunk_ids_are_stable_so_a_retried_batch_cannot_duplicate():
    """Re-upserting is safe BY CONSTRUCTION — there is nothing to de-duplicate.

    A point id is ``sha256(project::path::index)`` formatted as a UUID, so a
    resent batch overwrites exactly the ids it wrote before. This is the
    property that lets the retry above exist without a dedup mechanism, an
    idempotency key, or a read-back; assert it rather than building one.
    """
    first = [make_chunk_id("proj", "docs/guide.md", i) for i in range(50)]
    second = [make_chunk_id("proj", "docs/guide.md", i) for i in range(50)]
    assert first == second
    assert len(set(first)) == 50, "chunk ids collide within one file"

    # Identity is per (project, path, index) — nothing else may shift it.
    assert make_chunk_id("other", "docs/guide.md", 0) != first[0]
    assert make_chunk_id("proj", "docs/other.md", 0) != first[0]


def test_a_retried_batch_resends_the_same_points_unchanged():
    """The retry must resend the batch, not a re-derived or partial one."""
    client = _FlakyClient(fail_times=1)
    points = [_point(i) for i in range(25)]

    upsert_points(client, "proj_x", points, sleep=_never_sleeps)

    assert client.calls == 2
    attempted, resent = client.batches
    assert [p.id for p in attempted] == [p.id for p in resent]
    assert [p.id for p in resent] == [p.id for p in points]


def test_upserting_the_same_points_twice_writes_the_same_ids():
    """Two identical runs must address the same rows, not accumulate rows."""
    points = [_point(i) for i in range(10)]

    first = _FlakyClient()
    upsert_points(first, "proj_x", points, sleep=_never_sleeps)
    second = _FlakyClient()
    upsert_points(second, "proj_x", [_point(i) for i in range(10)], sleep=_never_sleeps)

    ids_first = [p.id for batch in first.batches for p in batch]
    ids_second = [p.id for batch in second.batches for p in batch]
    assert ids_first == ids_second


# --- batching: both ceilings -----------------------------------------------


def test_the_point_count_ceiling_still_holds():
    """The existing parameter must keep working — callers and tests rely on it."""
    client = _FlakyClient()
    points = [_point(i) for i in range(250)]

    assert upsert_points(client, "proj_x", points, batch_size=100) == 250
    assert [len(b) for b in client.batches] == [100, 100, 50]


def test_the_payload_byte_ceiling_splits_a_batch_the_count_would_not():
    """100 points is not a bound on the REQUEST, only on the row count.

    100 chunks of prose is about a megabyte; 100 chunks holding whole vendored
    source classes is many times that. It matters more now that a failed batch
    is RESENT: a retry of an oversized body pays the whole cost again, and an
    oversized body is exactly the one whose write is slow enough to meet a
    timeout in the first place.
    """
    big = "x" * (64 * 1024)
    client = _FlakyClient()
    points = [_point(i, text=big) for i in range(100)]

    assert upsert_points(client, "proj_x", points, batch_size=100) == 100

    assert len(client.batches) > 1, (
        "a 100-point batch of 64 KiB chunks went out as one request — the byte "
        "ceiling is not being applied"
    )
    for batch in client.batches:
        assert len(batch) <= 100
        size = sum(indexer._point_bytes(p) for p in batch)
        assert size <= indexer._UPSERT_BATCH_BYTES, (
            f"a batch of {size} bytes exceeded the {indexer._UPSERT_BATCH_BYTES} ceiling"
        )

    # Nothing may be dropped or duplicated by the split.
    sent = [p.id for batch in client.batches for p in batch]
    assert sent == [p.id for p in points]


def test_a_single_oversized_point_is_still_sent_alone():
    """Refusing it would drop content; shrinking it would never terminate."""
    client = _FlakyClient()
    huge = _point(0, text="y" * (indexer._UPSERT_BATCH_BYTES + 1))

    assert upsert_points(client, "proj_x", [huge, _point(1)], batch_size=100) == 2
    assert [len(b) for b in client.batches] == [1, 1]


def test_an_empty_upsert_issues_no_request():
    """Zero points must cost zero sockets, not one empty round-trip."""
    client = _FlakyClient()
    assert upsert_points(client, "proj_x", []) == 0
    assert client.calls == 0


# --- prevention: the connection pool, stated rather than inherited ---------


class _RecordingClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False

    def close(self):
        self.closed = True


@pytest.mark.parametrize("backend_name", ["managed", "external"])
def test_the_server_backends_state_their_connection_pool(monkeypatch, backend_name):
    """Inheriting qdrant-client's localhost default is what burned the ports.

    ``QdrantRemote.__init__`` substitutes
    ``Limits(max_connections=None, max_keepalive_connections=0)`` whenever the
    host is ``localhost`` or ``127.0.0.1`` — which is every managed engine we
    run. Keep-alive disabled means one fresh TCP socket per request, each
    parked in TIME_WAIT afterwards, against a Windows range 16,384 wide. The
    retry above makes that survivable; passing explicit limits is what stops it
    happening in the first place.

    Asserted on the constructor call rather than on a live pool so the test
    opens no socket at all.
    """
    from ragtools import storage

    monkeypatch.setattr(storage, "QdrantClient", _RecordingClient)
    cls = storage.ManagedBackend if backend_name == "managed" else storage.ExternalBackend
    client = cls("http://127.0.0.1:21500").client()

    limits = client.kwargs.get("limits")
    assert limits is not None, (
        f"{backend_name} builds its client with no explicit limits — it "
        "inherits the keep-alive-disabling localhost default"
    )
    assert limits.max_keepalive_connections > 0, (
        "keep-alive is still disabled, so every request costs a new port"
    )
    assert limits.max_connections is not None, "the pool size is unbounded"
    assert limits.max_keepalive_connections <= limits.max_connections
    assert limits.keepalive_expiry and limits.keepalive_expiry > 5.0, (
        "an idle connection expires faster than the gap between two batches, "
        "so it is never actually reused"
    )


@pytest.mark.parametrize("backend_name", ["managed", "external"])
def test_closing_a_server_backend_releases_the_pool(monkeypatch, backend_name):
    """Sockets held open by keep-alive must be released, not left to the GC."""
    from ragtools import storage

    monkeypatch.setattr(storage, "QdrantClient", _RecordingClient)
    cls = storage.ManagedBackend if backend_name == "managed" else storage.ExternalBackend
    backend = cls("http://127.0.0.1:21500")
    client = backend.client()

    backend.close()
    assert client.closed, "close() left the httpx pool open"
    backend.close()  # idempotent
