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


@pytest.fixture(autouse=True, scope="session")
def _no_repository_pollution():
    """No test may leave state in the developer's own data directory.

    A test that constructs a bare ``Settings()`` resolves ``state_db`` — and
    everything derived from it — relative to the working directory, which during
    a test run is the repository. One migration test did exactly that and left a
    real ``data/relayout.db`` behind, after which ``rag selfcheck`` on this
    machine reported a pending re-index that did not exist.

    Isolating it globally (``RAG_DATA_DIR`` in the environment) looked like the
    fix and was worse: it marks ``data_dir`` as explicitly set, which defeats the
    ``_anchor_data_dir`` validator and broke 29 unrelated tests. So the rule is
    enforced rather than imposed — a test that pollutes fails, and fixes itself
    by passing an explicit path.
    """
    data = Path(__file__).resolve().parents[1] / "data"
    before = {p.name for p in data.iterdir()} if data.is_dir() else set()
    yield
    after = {p.name for p in data.iterdir()} if data.is_dir() else set()
    created = after - before
    assert not created, (
        f"the suite created {sorted(created)} in the repository's data/ "
        "directory. Pass an explicit state_db/qdrant_path to Settings() in the "
        "offending test."
    )
