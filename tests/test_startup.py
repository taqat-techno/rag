"""Service autostart, as a platform-neutral delegate.

The previous suite pinned Windows internals — a Startup-folder path, a generated
`.vbs`, its `wscript` invocation. Those are gone: the mechanism moved into
`ragtools.platform` (one implementation per OS, tested in
`test_platform_adapters`), and this module now only decides *what* to register.

What is tested here is the product-level behaviour that survived the move, plus
the one thing the old design could not do at all: report that a machine carries
several registrations for one concern.
"""

from __future__ import annotations

import pytest

from ragtools.config import Settings
from ragtools.platform import KIND_SERVICE, Registration
from ragtools.service import startup

#: The Task Scheduler path the Windows adapter registers under. Named once
#: so the backslashes are not re-escaped differently at six call sites.
TASK_PATH = r"\RAGTools\Service"


def _settings(tmp_path) -> Settings:
    return Settings(
        qdrant_path=str(tmp_path / "qdrant"),
        state_db=str(tmp_path / "state.db"),
        data_dir=str(tmp_path / "data"),
    )


class FakeAdapter:
    """Records registrations instead of touching the machine."""

    name = "fake"

    def __init__(self, existing=None, fail: str = ""):
        self.existing = list(existing or [])
        self.installed = []
        self.removed = []
        self.fail = fail

    def install_autostart(self, spec):
        if self.fail:
            raise RuntimeError(self.fail)
        self.installed.append(spec)
        return Registration(spec.name, spec.kind, "fake", " ".join(spec.argv))

    def remove_autostart(self, name):
        gone = [r for r in self.existing if r.name == name]
        self.existing = [r for r in self.existing if r.name != name]
        self.removed.extend(gone)
        return gone

    def find_autostart(self, kind=KIND_SERVICE):
        return [r for r in self.existing if r.kind == kind]

    def has_desktop_session(self):
        return True


@pytest.fixture
def fake(monkeypatch):
    """Install a fake adapter for the NAME `startup` resolved at import time.

    Patching the package attribute would leave the real adapter in play — a
    mistake that, in this suite, once reached the live Windows scheduler.
    """
    def _install(adapter):
        monkeypatch.setattr(startup, "adapter", lambda: adapter)
        return adapter

    return _install


# --- what gets registered -------------------------------------------------


def test_registration_names_the_service_command(tmp_path, fake):
    adapter = fake(FakeAdapter())
    assert startup.install_task(_settings(tmp_path)) is True

    spec = adapter.installed[0]
    assert spec.kind == KIND_SERVICE
    assert "service" in spec.argv and "run" in spec.argv


def test_registration_carries_the_installed_profile(tmp_path, fake):
    """A registration without an explicit profile inherits whatever environment
    the login session happens to have — which is how a scheduled service ends
    up writing to a development data directory."""
    adapter = fake(FakeAdapter())
    startup.install_task(_settings(tmp_path))
    assert adapter.installed[0].environment.get("RAG_PROFILE") == "installed"


def test_registration_delays_so_it_does_not_race_the_rest_of_login(tmp_path, fake):
    adapter = fake(FakeAdapter())
    startup.install_task(_settings(tmp_path), delay_seconds=45)
    assert adapter.installed[0].delay_seconds == 45


def test_registration_launches_no_interpreted_shim(tmp_path, fake):
    """The `.vbs` launcher existed only to hide a console window. Hiding a
    window is a process-creation flag; shipping a script to do it is what made
    a black box flash on the user's screen."""
    adapter = fake(FakeAdapter())
    startup.install_task(_settings(tmp_path))
    argv = [str(a).lower() for a in adapter.installed[0].argv]
    assert not any(a.endswith(".vbs") for a in argv)
    assert not any("wscript" in a for a in argv)


def test_a_failed_registration_reports_false_rather_than_raising(tmp_path, fake):
    """Autostart failure must not abort a service start — the service still
    works, it just will not come back after a reboot, and health says so."""
    fake(FakeAdapter(fail="Access is denied"))
    assert startup.install_task(_settings(tmp_path)) is False


# --- removal --------------------------------------------------------------


def test_uninstall_removes_current_and_superseded_registrations(fake):
    """The point of the rewrite: a machine carrying a scheduled task AND two
    Startup-folder scripts must end up with none of them."""
    adapter = fake(FakeAdapter(existing=[
        Registration(TASK_PATH, KIND_SERVICE, "task-scheduler", "rag.exe"),
        Registration("RAGTools Watchdog", KIND_SERVICE, "task-scheduler", "x", legacy=True),
        Registration("RAGTools.vbs", KIND_SERVICE, "startup-folder", "y", legacy=True),
    ]))
    assert startup.uninstall_task() is True
    assert {r.name for r in adapter.removed} == {
        TASK_PATH, "RAGTools Watchdog", "RAGTools.vbs"}
    assert adapter.existing == []


def test_uninstall_is_idempotent(fake):
    """An interrupted uninstall re-runs; removing nothing is success."""
    fake(FakeAdapter())
    assert startup.uninstall_task() is True
    assert startup.uninstall_task() is True


# --- reporting ------------------------------------------------------------


def test_is_installed_ignores_legacy_only_machines(fake):
    """A machine with ONLY a superseded registration is not correctly
    configured — reporting it as installed is why nothing ever cleaned up."""
    fake(FakeAdapter(existing=[
        Registration("RAGTools.vbs", KIND_SERVICE, "startup-folder", "x", legacy=True),
    ]))
    assert startup.is_task_installed() is False


def test_is_installed_true_for_a_current_registration(fake):
    fake(FakeAdapter(existing=[
        Registration(TASK_PATH, KIND_SERVICE, "task-scheduler", "rag.exe"),
    ]))
    assert startup.is_task_installed() is True


def test_info_is_none_when_nothing_is_registered(fake):
    fake(FakeAdapter())
    assert startup.get_task_info() is None


def test_info_reports_a_clean_single_registration(fake):
    fake(FakeAdapter(existing=[
        Registration(TASK_PATH, KIND_SERVICE, "task-scheduler", "rag.exe"),
    ]))
    info = startup.get_task_info()
    assert info["status"] == "Installed"
    assert info["problem"] == ""
    assert info["legacy"] == []


def test_info_surfaces_surviving_legacy_registrations(fake):
    """This is the finding the old `is_task_installed()` could never produce:
    the machine works AND is carrying junk that the next upgrade must remove."""
    fake(FakeAdapter(existing=[
        Registration(TASK_PATH, KIND_SERVICE, "task-scheduler", "rag.exe"),
        Registration("RAGTools Watchdog", KIND_SERVICE, "task-scheduler", "x", legacy=True),
    ]))
    info = startup.get_task_info()
    assert "RAGTools Watchdog" in " ".join(info["legacy"])
    assert "rag upgrade apply" in info["problem"]


def test_info_surfaces_duplicate_current_registrations(fake):
    fake(FakeAdapter(existing=[
        Registration("a", KIND_SERVICE, "task-scheduler", "x"),
        Registration("b", KIND_SERVICE, "systemd-user", "y"),
    ]))
    assert "expected exactly one" in startup.get_task_info()["problem"]
