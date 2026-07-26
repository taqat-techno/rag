"""The suite must never read the developer's ``./ragtools.toml``.

Isolation used to live only in a function-scoped autouse fixture. Fixtures with
``scope="module"`` are constructed BEFORE function-scoped ones, so their
``Settings()`` bypassed it entirely. Nothing noticed while the working copy had
no config file; the moment a real one existed with

    collection_strategy = "per_project"
    storage_backend     = "managed"

four unrelated tests began failing with empty results — because their fixtures
had quietly adopted the developer's collection layout and storage engine.

These tests pin the isolation at every fixture scope, so the gap cannot reopen.

Plan: docs/planning/RAG_COLLECTION_ARCHITECTURE_IMPLEMENTATION_PLAN.md (W12)
"""

import os
from pathlib import Path

import pytest

from ragtools.config import Settings, _find_config_path


# Module-scoped: built BEFORE any function-scoped fixture. This is precisely
# the scope the old isolation missed.
@pytest.fixture(scope="module")
def settings_built_at_module_scope():
    return Settings()


@pytest.fixture(scope="session")
def settings_built_at_session_scope():
    return Settings()


def test_no_ambient_config_file_is_resolved():
    """RAG_CONFIG_PATH points at a file that does not exist, so no TOML loads."""
    assert os.environ.get("RAG_CONFIG_PATH"), "config isolation env var is unset"
    assert _find_config_path() is None, (
        "the suite resolved a real config file — tests would inherit whatever "
        "the developer happens to have configured"
    )


def test_module_scoped_settings_use_defaults(settings_built_at_module_scope):
    s = settings_built_at_module_scope
    assert s.collection_strategy == "shared"
    assert s.storage_backend == "embedded"
    assert s.collection_name == "markdown_kb"


def test_session_scoped_settings_use_defaults(settings_built_at_session_scope):
    s = settings_built_at_session_scope
    assert s.collection_strategy == "shared"
    assert s.storage_backend == "embedded"


def test_function_scoped_settings_use_defaults():
    s = Settings()
    assert s.collection_strategy == "shared"
    assert s.storage_backend == "embedded"


def test_the_worktree_config_is_not_what_tests_see():
    """A direct check against the actual file, if the developer has one.

    If ./ragtools.toml exists and configures something non-default, the suite
    must NOT be seeing it. This is the exact failure that motivated the fix.
    """
    dev_config = Path("ragtools.toml")
    if not dev_config.is_file():
        pytest.skip("no developer ragtools.toml in the working directory")
    text = dev_config.read_text(encoding="utf-8")
    if "per_project" in text:
        assert Settings().collection_strategy == "shared", (
            "the developer's per_project config leaked into the test suite"
        )
    if "managed" in text:
        assert Settings().storage_backend == "embedded", (
            "the developer's managed-storage config leaked into the test suite"
        )


@pytest.mark.parametrize("var", ["RAG_STORAGE_BACKEND", "RAG_COLLECTION_STRATEGY",
                                 "RAG_QDRANT_BINARY", "RAG_STORAGE_URL"])
def test_storage_env_vars_are_cleared(var):
    """An ambient value here would point tests at a real engine."""
    assert var not in os.environ
