"""Phase 5 (W5-B) — managed Qdrant boot integration.

`QdrantSupervisor` existed but was never spawned by the service. This is the
wiring: locate a binary, start it, health-gate it, verify the pinned version —
and, crucially, **fall back to embedded with a stated reason** rather than
failing to start, because no Qdrant build exists for some platforms
(notably Windows ARM64).

Plan: docs/planning/RAG_MODERNIZATION_MASTER_PLAN.md  (Phase 5 -> G5, D5)
"""

import sys

import pytest

from ragtools.service.managed_qdrant import (
    ManagedUnavailable,
    find_qdrant_binary,
    plan_managed_startup,
)


class _S:
    """Minimal settings stand-in."""
    def __init__(self, **kw):
        self.storage_backend = kw.get("storage_backend", "managed")
        self.qdrant_binary = kw.get("qdrant_binary")
        self.data_dir = kw.get("data_dir", "/tmp/ragtools")
        self.qdrant_path = kw.get("qdrant_path", "/tmp/ragtools/qdrant")
        self.storage_url = kw.get("storage_url")
        self.service_host = "127.0.0.1"


# --- binary resolution ---------------------------------------------------


def test_explicit_binary_setting_wins(tmp_path):
    exe = tmp_path / "qdrant.exe"
    exe.write_text("x")
    assert find_qdrant_binary(_S(qdrant_binary=str(exe))) == str(exe)


def test_explicit_but_missing_binary_is_not_silently_ignored(tmp_path):
    missing = str(tmp_path / "nope.exe")
    with pytest.raises(ManagedUnavailable) as ei:
        find_qdrant_binary(_S(qdrant_binary=missing))
    assert "not found" in str(ei.value).lower()


def test_bundled_binary_is_discovered(tmp_path, monkeypatch):
    bundled = tmp_path / ("qdrant.exe" if sys.platform == "win32" else "qdrant")
    bundled.write_text("x")
    monkeypatch.setattr("ragtools.service.managed_qdrant._candidate_dirs",
                        lambda settings: [tmp_path])
    assert find_qdrant_binary(_S()) == str(bundled)


def test_no_binary_anywhere_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr("ragtools.service.managed_qdrant._candidate_dirs",
                        lambda settings: [tmp_path])
    assert find_qdrant_binary(_S()) is None


# --- startup planning (pure decision logic, no process spawned) ----------


def test_embedded_backend_does_not_plan_a_managed_start():
    plan = plan_managed_startup(_S(storage_backend="embedded"))
    assert plan.should_start is False
    assert "embedded" in plan.reason.lower()


def test_external_backend_does_not_spawn_anything():
    """An externally-managed server is someone else's process."""
    plan = plan_managed_startup(_S(storage_backend="external",
                                   storage_url="http://127.0.0.1:6333"))
    assert plan.should_start is False


def test_unsupported_platform_falls_back_to_embedded_with_a_reason(monkeypatch):
    """No Qdrant build is published for Windows ARM64 — the product must degrade
    honestly, never guess a binary."""
    monkeypatch.setattr("ragtools.service.managed_qdrant.resolve_qdrant_asset",
                        lambda system, machine: None)
    plan = plan_managed_startup(_S())
    assert plan.should_start is False
    assert plan.fallback_to_embedded is True
    assert "platform" in plan.reason.lower()


def test_missing_binary_falls_back_to_embedded_with_a_reason(monkeypatch, tmp_path):
    monkeypatch.setattr("ragtools.service.managed_qdrant._candidate_dirs",
                        lambda settings: [tmp_path])
    plan = plan_managed_startup(_S())
    assert plan.should_start is False
    assert plan.fallback_to_embedded is True
    assert "binary" in plan.reason.lower()


def test_ready_plan_carries_ports_and_url(monkeypatch, tmp_path):
    exe = tmp_path / "qdrant.exe"
    exe.write_text("x")
    plan = plan_managed_startup(_S(qdrant_binary=str(exe)))
    assert plan.should_start is True
    assert plan.binary == str(exe)
    assert plan.http_port and plan.grpc_port
    assert plan.url == f"http://127.0.0.1:{plan.http_port}"
    assert plan.http_port != plan.grpc_port
