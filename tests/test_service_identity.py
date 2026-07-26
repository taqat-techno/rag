"""S16 — service identity + registry self-healing (pure core, folds in A7).

The live defect this exists to prevent: an instance on ``:21422`` reporting
version fields that made it look like ``:21420``. §27.1's rule is that a client
verifies ``service_id``, ``profile`` and ``api_version`` and **refuses on
mismatch** — a port number alone is never trusted — and ``bound_port`` is the
ACTUAL bind, the one field that would have caught it.

This pins the identity-payload builder, the client-side verifier, and the
PID-validated registry prune (§27.2, reusing the ``_clean_stale_pid``
self-healing precedent). The registry *file format* is deferred to integration;
the prune logic is format-agnostic and tested here.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S16 -> G16)
"""

import pytest

from ragtools.service.identity import (
    API_VERSION,
    IdentityMismatch,
    RegistryEntry,
    build_identity,
    prune_stale,
    verify_identity,
)


def _identity(**over):
    base = dict(
        service_id="svc-abc",
        instance_id="inst-1",
        version="3.0.0",
        profile="dev",
        install_mode="source",
        bound_host="127.0.0.1",
        bound_port=21437,
        data_dir="/data/dev",
        config_path="/cfg/dev.toml",
        storage={"mode": "embedded", "target": "local", "engine_version": None},
        auth_mode="none",
        capabilities=["retrieval"],
        collections_ready=True,
    )
    base.update(over)
    return build_identity(**base)


# --- identity payload (§27.1) -------------------------------------------


def test_identity_has_the_required_shape():
    ident = _identity()
    for key in (
        "service", "service_id", "instance_id", "version", "api_version",
        "profile", "install_mode", "bound_host", "bound_port", "data_dir",
        "config_path", "storage", "auth_mode", "capabilities", "collections_ready",
    ):
        assert key in ident, f"missing {key}"
    assert ident["service"] == "ragtools"
    assert ident["api_version"] == API_VERSION


def test_bound_port_is_the_actual_bind_not_the_configured_one():
    # The :21422-reports-:21420 defect: identity carries the REAL bind.
    ident = _identity(bound_port=21422)
    assert ident["bound_port"] == 21422


# --- client-side verification: refuse on mismatch -----------------------


def test_verify_passes_when_identity_matches():
    actual = _identity()
    expected = {"service_id": "svc-abc", "profile": "dev", "api_version": API_VERSION}
    verify_identity(expected, actual)  # no raise


@pytest.mark.parametrize("field,bad", [
    ("service_id", "svc-other"),
    ("profile", "installed"),
    ("api_version", "999"),
])
def test_verify_refuses_on_any_identity_mismatch(field, bad):
    actual = _identity()
    expected = {"service_id": "svc-abc", "profile": "dev", "api_version": API_VERSION}
    expected[field] = bad
    with pytest.raises(IdentityMismatch):
        verify_identity(expected, actual)


def test_matching_port_does_not_excuse_a_wrong_service():
    # "A port number alone is never trusted." Same bound_port, wrong service_id.
    actual = _identity(bound_port=21420, service_id="svc-live")
    expected = {"service_id": "svc-dev", "profile": "dev",
                "api_version": API_VERSION, "bound_port": 21420}
    with pytest.raises(IdentityMismatch):
        verify_identity(expected, actual)


# --- registry prune: validated against live PIDs on read ----------------


def _entry(service_id, pid, port):
    return RegistryEntry(
        service_id=service_id, profile="dev", bound_host="127.0.0.1",
        bound_port=port, pid=pid, started_at="t", data_dir="/d",
        storage_target="local",
    )


def test_prune_drops_dead_pid_entries():
    entries = [_entry("a", 100, 21430), _entry("b", 200, 21431)]
    live = {100}  # only pid 100 is alive
    kept = prune_stale(entries, is_pid_alive=lambda p: p in live)
    assert [e.service_id for e in kept] == ["a"]


def test_prune_keeps_all_when_all_alive():
    entries = [_entry("a", 100, 21430), _entry("b", 200, 21431)]
    kept = prune_stale(entries, is_pid_alive=lambda p: True)
    assert len(kept) == 2


def test_registry_entry_dict_roundtrips():
    e = _entry("a", 100, 21430)
    assert RegistryEntry.from_dict(e.to_dict()) == e
