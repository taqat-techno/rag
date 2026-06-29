"""M3 — robust, lifecycle-owned watcher autostart.

These cover the watcher *controller* in `routes.py` directly (no HTTP), proving:
  - service-owned autostart starts the watcher when it is desired
  - an explicit user stop is respected and never auto-undone
  - no duplicate watcher thread is created
  - an autostart failure is recorded and surfaced as a lifecycle state

The controller is exercised without a real file watcher by swapping
`WatcherThread` for a lightweight fake (the controller imports it lazily).
"""

import pytest

from ragtools.config import Settings
from ragtools.service import app as app_module
from ragtools.service import routes as routes_mod


class FakeWatcher:
    """Stand-in for WatcherThread — records lifecycle without watching files."""

    instances: list = []
    _MAX_RETRIES = 5  # mirror the real class — _derive_watcher_state reads it

    def __init__(self, owner, settings):
        self.owner = owner
        self.settings = settings
        self._alive = False
        self.started = False
        self.stopped = False
        FakeWatcher.instances.append(self)

    def start(self):
        self._alive = True
        self.started = True

    def is_alive(self):
        return self._alive

    def stop(self):
        self._alive = False
        self.stopped = True

    def join(self, timeout=None):
        return None

    def get_state_snapshot(self):
        return {
            "last_started_at": "2026-01-01T00:00:00+00:00" if self.started else None,
            "last_error": None,
            "last_error_at": None,
            "consecutive_failures": 0,
        }


class BoomWatcher:
    """Construction always fails — models a watcher that can't even start."""

    def __init__(self, owner, settings):
        raise RuntimeError("cannot construct watcher")


_LIFECYCLE_GLOBALS = (
    "_watcher_thread",
    "_watcher_desired_run",
    "_watcher_autostart_error",
    "_watcher_autostart_error_at",
)


@pytest.fixture
def wenv(monkeypatch):
    """Reset the routes lifecycle globals + inject a fake watcher and owner."""
    import ragtools.service.watcher_thread as wt

    saved = {k: getattr(routes_mod, k, None) for k in _LIFECYCLE_GLOBALS}
    routes_mod._watcher_thread = None
    routes_mod._watcher_desired_run = True
    routes_mod._watcher_autostart_error = None
    routes_mod._watcher_autostart_error_at = None

    prev_o, prev_s = app_module._owner, app_module._settings
    app_module._owner = object()
    app_module._settings = Settings()

    FakeWatcher.instances = []
    monkeypatch.setattr(wt, "WatcherThread", FakeWatcher)

    yield

    for k, v in saved.items():
        setattr(routes_mod, k, v)
    app_module._owner, app_module._settings = prev_o, prev_s


# --- autostart ---------------------------------------------------------------

def test_autostart_starts_watcher_when_desired(wenv):
    result = routes_mod.autostart_watcher()
    assert result["status"] == "started"
    assert len(FakeWatcher.instances) == 1
    assert FakeWatcher.instances[0].started is True
    assert routes_mod._watcher_thread is FakeWatcher.instances[0]


def test_autostart_skipped_when_user_stopped(wenv):
    routes_mod._watcher_desired_run = False
    result = routes_mod.autostart_watcher()
    assert result["status"] == "skipped_user_stopped"
    assert FakeWatcher.instances == []  # nothing constructed
    assert routes_mod._watcher_thread is None


def test_autostart_is_idempotent_no_duplicate(wenv):
    routes_mod.autostart_watcher()
    second = routes_mod.autostart_watcher()
    assert second["status"] == "already_running"
    assert len(FakeWatcher.instances) == 1  # no duplicate thread


def test_autostart_records_error_on_failure(wenv, monkeypatch):
    import ragtools.service.watcher_thread as wt
    monkeypatch.setattr(wt, "WatcherThread", BoomWatcher)

    result = routes_mod.autostart_watcher()
    assert result["status"] == "error"
    assert routes_mod._watcher_autostart_error  # recorded, not swallowed
    assert routes_mod._watcher_thread is None
    # ...and it surfaces as a distinct lifecycle state
    snap = routes_mod._watcher_observability_snapshot()
    assert snap["state"] == "autostart_failed"


# --- user intent -------------------------------------------------------------

def test_user_stop_blocks_subsequent_autostart(wenv):
    """An explicit stop must not be undone by a later lifecycle autostart."""
    routes_mod.autostart_watcher()           # running
    routes_mod.watcher_stop()                # user stops it
    assert routes_mod._watcher_desired_run is False

    again = routes_mod.autostart_watcher()   # lifecycle tries again
    assert again["status"] == "skipped_user_stopped"
    assert routes_mod._watcher_thread is None  # stayed stopped


def test_explicit_start_rearms_after_stop(wenv):
    routes_mod.watcher_stop()                # desired -> stopped
    assert routes_mod._watcher_desired_run is False
    routes_mod.watcher_start()               # explicit user start re-arms
    assert routes_mod._watcher_desired_run is True
    assert routes_mod._watcher_thread is not None


def test_state_is_stopped_after_user_stop(wenv):
    routes_mod.autostart_watcher()
    routes_mod.watcher_stop()
    snap = routes_mod._watcher_observability_snapshot()
    # "stopped" (user intent) is now distinct from "inactive"/"exited".
    assert snap["state"] == "stopped"
    assert snap["desired"] == "stopped"


# --- pure derivation ---------------------------------------------------------

def test_derive_state_distinguishes_stopped_and_autostart_failed():
    from ragtools.service.routes import _derive_watcher_state
    # back-compat: old positional calls still resolve
    assert _derive_watcher_state({}, True) == "running"
    assert _derive_watcher_state({}, False) == "inactive"
    # new signals
    assert _derive_watcher_state({}, False, desired_run=False) == "stopped"
    assert (
        _derive_watcher_state({}, False, autostart_error="boom")
        == "autostart_failed"
    )
