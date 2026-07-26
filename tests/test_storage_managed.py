"""S4 — managed native Qdrant lifecycle (binary-independent logic).

Full validation needs a real Qdrant binary + running server; these pin the
parts that must be correct BEFORE that: platform asset resolution (incl. the
platforms with NO published binary, e.g. Windows-ARM64), config generation
(loopback-only, telemetry off, low segment count for collection-per-project),
and the readiness/version/identity gate (subprocess + HTTP injected, so no
real process is spawned here).

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S4 -> G4)
"""

import pytest

from ragtools.storage_managed import (
    PINNED_QDRANT_VERSION,
    ManagedStartError,
    QdrantSupervisor,
    generate_qdrant_config,
    resolve_qdrant_asset,
)


# --- platform asset resolution ------------------------------------------


def test_asset_windows_x64():
    a = resolve_qdrant_asset("Windows", "AMD64")
    assert a is not None and "pc-windows-msvc" in a


def test_asset_windows_arm64_has_no_binary():
    # Official: no Windows-ARM64 build is published — must refuse, not guess.
    assert resolve_qdrant_asset("Windows", "ARM64") is None


def test_asset_macos_both_arches():
    assert "aarch64-apple-darwin" in resolve_qdrant_asset("Darwin", "arm64")
    assert "x86_64-apple-darwin" in resolve_qdrant_asset("Darwin", "x86_64")


def test_asset_linux_x64_is_gnu():
    assert "x86_64-unknown-linux-gnu" in resolve_qdrant_asset("Linux", "x86_64")


def test_asset_linux_arm64_is_musl_only():
    # Official: only a musl aarch64 build is published (no gnu).
    assert "aarch64-unknown-linux-musl" in resolve_qdrant_asset("Linux", "aarch64")


def test_asset_unknown_platform_returns_none():
    assert resolve_qdrant_asset("Plan9", "sparc") is None


def test_pinned_version_is_a_semver():
    parts = PINNED_QDRANT_VERSION.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)


# --- config generation --------------------------------------------------


def test_config_is_loopback_only_and_telemetry_off(tmp_path):
    cfg = generate_qdrant_config(
        storage_path=str(tmp_path / "st"), http_port=6333, grpc_port=6334
    )
    assert cfg["service"]["host"] == "127.0.0.1"  # never 0.0.0.0
    assert cfg["service"]["http_port"] == 6333
    assert cfg["service"]["grpc_port"] == 6334
    assert cfg["telemetry_disabled"] is True
    assert cfg["storage"]["storage_path"] == str(tmp_path / "st")


def test_config_uses_low_segment_number(tmp_path):
    # Investigation: default_segment_number=0 => one-per-CPU (~16 on a 16-core
    # box) PER collection; set 1-2 for a collection-per-project design.
    cfg = generate_qdrant_config(storage_path=str(tmp_path), http_port=1, grpc_port=2)
    assert cfg["storage"]["optimizers"]["default_segment_number"] in (1, 2)


# --- readiness / version / identity gate (mocked, no real process) ------


class _FakeProc:
    def __init__(self):
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


def _http_seq(*responses):
    """Return a fake http_get yielding the given (status, json) responses."""
    it = iter(responses)

    def _get(url):
        status, body = next(it)

        class _R:
            status_code = status

            def json(self_inner):
                return body

        return _R()

    return _get


def test_supervisor_becomes_ready_then_version_verifies(tmp_path):
    proc = _FakeProc()
    sup = QdrantSupervisor(
        binary_path="/fake/qdrant",
        storage_path=str(tmp_path / "st"),
        http_port=6333,
        grpc_port=6334,
        pinned_version=PINNED_QDRANT_VERSION,
        spawn=lambda cmd, **kw: proc,
        http_get=_http_seq(
            (200, {}),  # /readyz
            (200, {"version": PINNED_QDRANT_VERSION}),  # GET / for version
        ),
        sleep=lambda s: None,
    )
    sup.start()
    sup.wait_ready(timeout=5)
    sup.verify_version()  # must not raise
    sup.stop()
    assert proc.poll() == 0  # terminated


def test_supervisor_refuses_version_mismatch(tmp_path):
    proc = _FakeProc()
    sup = QdrantSupervisor(
        binary_path="/fake/qdrant",
        storage_path=str(tmp_path / "st"),
        http_port=6333,
        grpc_port=6334,
        pinned_version=PINNED_QDRANT_VERSION,
        spawn=lambda cmd, **kw: proc,
        http_get=_http_seq(
            (200, {}),
            (200, {"version": "0.0.1"}),  # wrong version
        ),
        sleep=lambda s: None,
    )
    sup.start()
    sup.wait_ready(timeout=5)
    with pytest.raises(ManagedStartError, match="(?i)version"):
        sup.verify_version()
    sup.stop()


def test_supervisor_refuses_synced_storage_path(tmp_path):
    synced = tmp_path / "Synced"
    (synced / ".stfolder").mkdir(parents=True)
    with pytest.raises(ManagedStartError, match="(?i)sync"):
        QdrantSupervisor(
            binary_path="/fake/qdrant",
            storage_path=str(synced / "st"),
            http_port=6333,
            grpc_port=6334,
            pinned_version=PINNED_QDRANT_VERSION,
            spawn=lambda cmd, **kw: _FakeProc(),
            http_get=_http_seq((200, {})),
            sleep=lambda s: None,
        ).start()
