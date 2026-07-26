"""S6/S11 — bridge the live TOML config into the ProjectRegistry.

The service still loads projects from TOML; the registry is the v3 home for
UUID identity + collection names. This sync populates the registry FROM the
config (idempotently, identity-preserving) — the safe first step of the
collection-per-project migration that does NOT yet change the search path or
create per-project collections, so it violates no current constraint.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S6 §11; S11 §29)
"""

from dataclasses import dataclass
from typing import Optional

import pytest

from ragtools.registry import ProjectRegistry, sync_projects_from_config


@dataclass
class _Cfg:
    """Minimal stand-in for ragtools.config.ProjectConfig."""

    id: str
    path: str
    mode: str = "docs"
    name: Optional[str] = None


@pytest.fixture
def reg(tmp_path):
    return ProjectRegistry(str(tmp_path / "r.db"))


def test_sync_adds_new_projects(reg):
    result = sync_projects_from_config(
        [_Cfg("a", "/w/a"), _Cfg("b", "/w/b", mode="code")], reg
    )
    assert result == {"added": 2, "updated": 0, "unchanged": 0}
    assert reg.get("b").mode == "code"
    assert reg.get("a").collection_name  # UUID-derived collection assigned


def test_sync_is_idempotent(reg):
    cfgs = [_Cfg("a", "/w/a"), _Cfg("b", "/w/b")]
    sync_projects_from_config(cfgs, reg)
    result = sync_projects_from_config(cfgs, reg)
    assert result == {"added": 0, "updated": 0, "unchanged": 2}


def test_sync_updates_moved_path_preserving_identity(reg):
    sync_projects_from_config([_Cfg("a", "/w/old")], reg)
    uuid = reg.get("a").uuid
    result = sync_projects_from_config([_Cfg("a", "/w/new")], reg)
    assert result["updated"] == 1
    moved = reg.get("a")
    assert moved.path == "/w/new"
    assert moved.uuid == uuid                       # identity preserved
    assert moved.collection_name  # collection unchanged (derives from UUID)


def test_sync_updates_changed_mode(reg):
    sync_projects_from_config([_Cfg("a", "/w/a", mode="docs")], reg)
    result = sync_projects_from_config([_Cfg("a", "/w/a", mode="general")], reg)
    assert result["updated"] == 1
    assert reg.get("a").mode == "general"
