"""Shared test fixtures for RAG Tools."""

import os
import tempfile
from pathlib import Path

import pytest

from ragtools.config import Settings


def pytest_configure(config):
    """Isolate the suite from the developer's ``./ragtools.toml`` — before any
    fixture of any scope runs.

    This isolation used to live only in a function-scoped autouse fixture, which
    silently did NOT cover ``scope="module"`` / ``scope="session"`` fixtures:
    those are constructed before function-scoped ones, so their ``Settings()``
    still read the CWD config. The gap was invisible while the working copy had
    no config file, and appeared the moment one existed with non-default values
    — a ``collection_strategy = "per_project"`` in the dev config made four
    unrelated tests fail with empty results.

    ``pytest_configure`` runs before collection, so every ``Settings()`` in the
    process sees the same isolated environment whatever its fixture scope.
    """
    os.environ["RAG_CONFIG_PATH"] = str(
        Path(tempfile.gettempdir()) / "ragtools-tests-no-such-config.toml"
    )
    # Belt and braces: an ambient value for any of these would point tests at a
    # real engine or a real collection layout.
    for leaky in ("RAG_STORAGE_BACKEND", "RAG_COLLECTION_STRATEGY",
                  "RAG_QDRANT_BINARY", "RAG_STORAGE_URL"):
        os.environ.pop(leaky, None)


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Per-test config isolation, on top of :func:`pytest_configure`.

    Retained so an individual test can monkeypatch ``RAG_CONFIG_PATH`` and have
    it restored automatically.
    """
    fake_config = str(tmp_path / "ragtools.toml")
    monkeypatch.setenv("RAG_CONFIG_PATH", fake_config)


@pytest.fixture
def settings():
    """Create a Settings instance with defaults."""
    return Settings()


@pytest.fixture
def memory_client():
    """Create an in-memory Qdrant client for testing."""
    return Settings.get_memory_client()


@pytest.fixture
def allow_platform_writes():
    """Opt out of the machine-mutation guard.

    Only for tests that construct an adapter with INJECTED roots and a fake
    runner, so the write lands in a temp directory.
    """
    return True


@pytest.fixture(autouse=True)
def _no_real_autostart_registration(request, monkeypatch):
    """Refuse to register autostart against the real machine from a test.

    Earned the hard way: a test that patched the wrong name fell through to the
    live Windows adapter and tried to create a real scheduled task, failing only
    because the account lacked permission. On a developer machine with rights it
    would have silently altered their login items.

    Reading is left alone — `find_autostart` is how the upgrade tests observe a
    machine — but anything that WRITES raises.
    """
    if "allow_platform_writes" in request.fixturenames:
        return

    from ragtools.platform.darwin import DarwinAdapter
    from ragtools.platform.linux import LinuxAdapter
    from ragtools.platform.windows import WindowsAdapter

    def _refuse(self, *args, **kwargs):
        raise AssertionError(
            "a test reached the real platform adapter and tried to change this "
            "machine's autostart. Inject a fake adapter instead."
        )

    for cls in (WindowsAdapter, LinuxAdapter, DarwinAdapter):
        monkeypatch.setattr(cls, "install_autostart", _refuse, raising=True)
        monkeypatch.setattr(cls, "remove_autostart", _refuse, raising=True)
