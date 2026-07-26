"""Service instance identity + registry self-healing (RAG v3, Stage S16).

Not to be confused with :mod:`ragtools.identity` (project/framework identity).
This module is about *which running service is this* — the ``/identity``
endpoint payload, the client-side verifier that refuses on mismatch, and the
PID-validated instance registry.

The defect this prevents is on record: an instance bound to ``:21422`` whose
version fields made it look like ``:21420``, so a client talked to the wrong
knowledge base. §27.1's rule — verify ``service_id`` / ``profile`` /
``api_version`` and refuse on mismatch; never trust a port number alone; and
``bound_port`` is the ACTUAL bind — is enforced here.

The registry is written on start, removed on clean shutdown, and **validated
against live PIDs on read** (§27.2), reusing the ``_clean_stale_pid``
self-healing precedent. The prune logic is format-agnostic and lives here; the
on-disk file format is chosen at integration time.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S16 -> G16)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Iterable

SERVICE_NAME = "ragtools"

#: Bumped when the identity/verification contract changes incompatibly. A client
#: refuses a service whose ``api_version`` it does not recognise (§27.1).
API_VERSION = "1"

#: Fields a client checks before issuing any request. A port is deliberately not
#: among them — "a port number alone is never trusted."
_VERIFIED_FIELDS = ("service_id", "profile", "api_version")


class IdentityMismatch(RuntimeError):
    """A service's identity does not match what the client expected. Refuse."""


def build_identity(
    *,
    service_id: str,
    instance_id: str,
    version: str,
    profile: str,
    install_mode: str,
    bound_host: str,
    bound_port: int,
    data_dir: str,
    config_path: str,
    storage: dict,
    auth_mode: str,
    capabilities: Iterable[str],
    collections_ready: bool,
) -> dict:
    """Build the ``GET /identity`` payload (§27.1).

    ``bound_host`` / ``bound_port`` are the ACTUAL bind reported by the running
    server, never the configured value — that distinction is the whole point.
    """
    return {
        "service": SERVICE_NAME,
        "service_id": service_id,
        "instance_id": instance_id,
        "version": version,
        "api_version": API_VERSION,
        "profile": profile,
        "install_mode": install_mode,
        "bound_host": bound_host,
        "bound_port": bound_port,
        "data_dir": data_dir,
        "config_path": config_path,
        "storage": dict(storage),
        "auth_mode": auth_mode,
        "capabilities": list(capabilities),
        "collections_ready": collections_ready,
    }


def verify_identity(expected: dict, actual: dict) -> None:
    """Refuse (raise :class:`IdentityMismatch`) unless the identity matches.

    Only :data:`_VERIFIED_FIELDS` are compared, and every one must be present in
    ``actual`` and equal to ``expected``. A matching ``bound_port`` never
    substitutes for a matching ``service_id`` — the defect that motivated this.
    """
    for field in _VERIFIED_FIELDS:
        if field not in expected:
            continue  # client did not pin this field
        want = expected[field]
        got = actual.get(field)
        if got != want:
            raise IdentityMismatch(
                f"identity mismatch on {field}: expected {want!r}, got {got!r}"
            )


@dataclass
class RegistryEntry:
    """One running instance in the per-profile registry (§27.2)."""

    service_id: str
    profile: str
    bound_host: str
    bound_port: int
    pid: int
    started_at: str
    data_dir: str
    storage_target: str
    qdrant_http_port: int | None = None
    qdrant_grpc_port: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RegistryEntry":
        fields = {
            "service_id", "profile", "bound_host", "bound_port", "pid",
            "started_at", "data_dir", "storage_target", "qdrant_http_port",
            "qdrant_grpc_port",
        }
        return cls(**{k: v for k, v in data.items() if k in fields})


def prune_stale(
    entries: Iterable[RegistryEntry],
    *,
    is_pid_alive: Callable[[int], bool],
) -> list[RegistryEntry]:
    """Drop entries whose PID is no longer alive (self-healing on read).

    ``is_pid_alive`` is injected so this is pure and testable without touching
    real processes; the service wires it to an ``os``-level liveness probe.
    """
    return [e for e in entries if is_pid_alive(e.pid)]
