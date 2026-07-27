"""Telling a slow index run apart from a dead one.

An index job that finds the mutex held has to wait — the job exists to make a
specific correction, and recording zeros as "succeeded" loses it silently. What
it must not do is guess why it is waiting.

v3.0.0 waited a flat 900 seconds and then raised *"another indexing run is
stuck"*. It had no evidence for that. During the startup sync of 25 projects a
user's `rag index` waited out the ceiling, exited 1 declaring a healthy run
stuck, and that run went on to finish normally.

Elapsed time cannot distinguish slow from dead. A heartbeat can, so the holder
publishes one and the waiter reads it.
"""

from __future__ import annotations

import pytest

from ragtools.service.job_handlers import (
    _BLIND_MAX_WAIT_SECONDS,
    _STALL_SECONDS,
    _index_activity,
    _refuse_if_wedged,
    _waiting_phase,
)


def activity(**kwargs) -> dict:
    base = {"what": "Full index", "phase": "chunk", "done": 4100, "total": 37637,
            "age": 1.0, "started_at": 0.0, "last_tick": 0.0}
    base.update(kwargs)
    return base


# --- the claim that was wrong --------------------------------------------


def test_a_progressing_run_is_waited_for_however_long_it_takes():
    """The incident, as a test.

    A first index of a large corpus legitimately runs for hours. Any ceiling on
    elapsed time fails it, and a 900 s one fails it early.
    """
    _refuse_if_wedged(activity(age=2.0), waited=6 * 3600, project=None)


def test_a_silent_run_is_refused():
    _refuse_if_wedged(activity(age=1.0), waited=0.0, project=None)

    with pytest.raises(RuntimeError, match="stalled"):
        _refuse_if_wedged(activity(age=_STALL_SECONDS + 1), waited=10.0, project=None)


def test_the_refusal_describes_the_holder_rather_than_accusing_it():
    """"Stuck" was an assertion about another run's health made from a clock.
    The replacement reports what was measured: how long it has been silent.

    Derived from `_STALL_SECONDS` rather than a literal, so tuning the threshold
    cannot quietly turn this into a test of nothing.
    """
    silence = _STALL_SECONDS + 100
    with pytest.raises(RuntimeError) as exc:
        _refuse_if_wedged(activity(age=silence), waited=silence, project="royal_preps")

    message = str(exc.value)
    assert "royal_preps" in message
    assert "Full index" in message
    assert f"{silence:.0f}s" in message, "the measured silence is not in the message"
    assert "Nothing was indexed" in message, "the outcome must stay explicit"


# --- when there is nothing to measure ------------------------------------


def test_without_a_heartbeat_it_falls_back_to_elapsed_time(monkeypatch):
    """An older owner, or a lock held outside the index path. With no liveness
    signal, elapsed time is all there is."""
    _refuse_if_wedged(None, waited=_BLIND_MAX_WAIT_SECONDS - 1, project=None)

    with pytest.raises(RuntimeError) as exc:
        _refuse_if_wedged(None, waited=_BLIND_MAX_WAIT_SECONDS, project=None)

    message = str(exc.value)
    assert "no progress information" in message
    assert "cannot be told apart" in message, (
        "with no heartbeat the code must not claim to know the run is stalled"
    )


def test_an_owner_without_the_probe_does_not_break_the_wait():
    """A test double or an older object must degrade, not raise."""
    class Bare:
        pass

    assert _index_activity(Bare()) is None


def test_a_probe_that_raises_is_treated_as_no_information():
    class Angry:
        def index_activity(self):
            raise RuntimeError("qdrant is unhappy")

    assert _index_activity(Angry()) is None


# --- what the user sees while queued -------------------------------------


def test_the_waiting_message_names_the_run_being_waited_for():
    """A bare "waiting" is indistinguishable from a hang. Naming the holder and
    its progress is what makes the pause legible."""
    phase = _waiting_phase(activity(done=4100, total=37637))

    assert "Full index" in phase
    assert "4100/37637" in phase


def test_the_waiting_message_survives_a_run_with_no_total():
    """`total` is 0 until the scan finishes; the phase must still render."""
    assert "12" in _waiting_phase(activity(done=12, total=0))


def test_the_waiting_message_falls_back_when_nothing_is_known():
    assert _waiting_phase(None) == "waiting for the running index"


# --- the owner's half of the contract ------------------------------------


def test_the_owner_publishes_and_clears_its_heartbeat(tmp_path):
    """`index_activity()` must be None when idle, or a waiter reads a finished
    run's heartbeat as a live one and waits for something already over."""
    from ragtools.config import Settings
    from ragtools.service.owner import QdrantOwner

    settings = Settings(qdrant_path=":memory:", state_db=str(tmp_path / "s.db"),
                        content_root=str(tmp_path))
    owner = QdrantOwner(settings, client=settings.get_memory_client())

    assert owner.index_activity() is None

    with owner._exclusive_index("Full index") as acquired:
        assert acquired is True
        live = owner.index_activity()
        assert live is not None
        assert live["what"] == "Full index"
        assert live["age"] >= 0.0

        owner._beat(7, 99, "chunk")
        beaten = owner.index_activity()
        assert (beaten["done"], beaten["total"], beaten["phase"]) == (7, 99, "chunk")

    assert owner.index_activity() is None, "heartbeat outlived its run"


def test_a_beat_outside_a_run_is_ignored(tmp_path):
    """Nothing should be able to fabricate liveness for a run that is not
    happening."""
    from ragtools.config import Settings
    from ragtools.service.owner import QdrantOwner

    settings = Settings(qdrant_path=":memory:", state_db=str(tmp_path / "s.db"),
                        content_root=str(tmp_path))
    owner = QdrantOwner(settings, client=settings.get_memory_client())

    owner._beat(1, 2, "chunk")

    assert owner.index_activity() is None
