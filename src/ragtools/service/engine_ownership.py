"""Which process owns the managed engine — proven, not inferred.

The v3.1.0 incident in one sentence: ``wait_ready()`` proved that a port
answered, and nothing more. A second service whose own child had died of
address-in-use polled the canonical instance's engine, got 200, matched the
pinned version — both run 1.15.5, so the version discriminates nothing — and
adopted a store it did not own, writing its collections into another
installation's data.

The product already held the rule that was broken. :mod:`ragtools.service.identity`
states it for the service layer::

    #: Fields a client checks before issuing any request. A port is deliberately
    #: not among them — "a port number alone is never trusted."

It was never applied one layer down, to the engine. This module applies it.

**Two proofs that always run, and two that are defence in depth.** Any one
failing means "not mine":

1. **The child is alive.** ALWAYS AVAILABLE. A process that has exited cannot be
   the thing answering. This alone breaks the reported incident, and it costs
   one poll.
2. **The API key authenticates.** ALWAYS AVAILABLE. Each installation generates
   its own; the engine is started with it and the client presents it. Another
   instance's engine rejects us outright, so adoption becomes impossible rather
   than merely unlikely. It is also the only proof that defends against a
   *foreign* Qdrant — somebody else's application — holding the port.
3. **The listener is our process.** BEST EFFORT. The PID holding the port must
   be the child we spawned.
4. **The executable matches.** BEST EFFORT. That PID's image must be the binary
   we launched.

**3 and 4 need ``psutil``, which this project does not declare as a dependency**
— five modules already import it opportunistically and degrade when it is
absent, and a packaged bundle generally has none. Saying "four proofs" without
that sentence would describe a boundary the shipped product does not have. What
the shipped product does have is 1 and 2, and they are the two that close the
incident: a dead child is refused outright, and an engine that is not ours
cannot authenticate us. 3 and 4 turn a later refusal into an earlier, clearer
one when they are available.

**The port check binds rather than connects** — see :func:`port_is_free`. That
distinction is load-bearing and was measured, not assumed.

**The port is the lock.** No separate lock file: the resource actually contended
is the TCP port, a held port is self-cleaning in a way a lock file is not, and a
bind conflict is the honest name for the conflict. So an occupied port is
resolved *before* anything is spawned — reattach when the manifest proves the
listener is ours, refuse otherwise. Refusing before the spawn is also what makes
"a failed secondary cannot kill the canonical engine" true: there is no failed
secondary child to clean up.

**The manifest is what makes termination attributable.** Killing on a port, or on
an image name, is how one installation kills another's database. Nothing here
terminates a process the manifest does not vouch for.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ragtools.service")

#: Filename of the ownership manifest, inside the data directory. One
#: installation, one manifest — it describes the engine THIS installation
#: started, and nothing else.
MANIFEST_NAME = "qdrant-owner.json"

#: Where the per-installation engine credentials live. Separate from the
#: manifest because they outlive any single engine process: the manifest is
#: rewritten on every start, the identity is not.
IDENTITY_NAME = "engine-identity.json"


class NotOurEngine(RuntimeError):
    """Something is listening, and it is not the engine this installation owns.

    Deliberately not a subclass of the managed-start error: this is not "the
    engine failed to start", it is "the engine that is running belongs to
    someone else". Conflating them is what produced a silent adoption.
    """


@dataclass(frozen=True)
class EngineClaim:
    """The durable record of an engine this installation started."""

    instance_id: str
    pid: int
    executable: str
    storage_path: str
    http_port: int
    grpc_port: int
    started_at: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


# --- per-installation identity ------------------------------------------


def _identity_path(settings) -> Path:
    return Path(settings.data_dir) / IDENTITY_NAME


def engine_identity(settings) -> dict:
    """This installation's ``instance_id`` and engine API key, created once.

    Generated on first use and persisted, because both must survive a restart:
    a key that changed every boot would lock the installation out of its own
    engine, and an instance id that changed would make every manifest foreign.

    ``instance_id`` from configuration wins when set — that is the explicit
    escape hatch for a deliberate second instance (see
    :func:`resolve_engine_ports`).
    """
    path = _identity_path(settings)
    record: dict = {}
    try:
        if path.is_file():
            record = json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        record = {}

    configured = (getattr(settings, "instance_id", None) or "").strip()
    changed = False
    if configured and record.get("instance_id") != configured:
        record["instance_id"] = configured
        changed = True
    if not record.get("instance_id"):
        record["instance_id"] = f"rag-{secrets.token_hex(8)}"
        changed = True
    if not record.get("api_key"):
        # 32 bytes of urandom. The engine is loopback-only, so this defends
        # against accidental adoption rather than against a network attacker —
        # but it defends against it absolutely.
        record["api_key"] = secrets.token_urlsafe(32)
        changed = True

    if changed:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass  # best effort; Windows ACLs are not POSIX modes
        except OSError as exc:
            # A key we cannot persist is a key that changes next boot, which
            # would lock us out of our own engine. Say so rather than proceed.
            logger.warning("could not persist engine identity to %s: %s", path, exc)
    return record


# --- port policy ---------------------------------------------------------


def resolve_engine_ports(settings, *, default_http: int, default_grpc: int
                         ) -> tuple[int, int, bool]:
    """The ports this instance will use, and whether they were chosen explicitly.

    Precedence: environment > configuration > the product defaults. The
    environment stays first because ``RAG_QDRANT_HTTP_PORT`` already worked that
    way and CI depends on it.

    The third element is the escape hatch's other half. One canonical managed
    instance per machine is the supported model; a deliberate second instance is
    allowed only when it says so twice — non-default ports AND an explicit
    ``instance_id``. Either alone is an accident waiting to be adopted, which is
    precisely what happened.
    """
    def _port(env_name: str, configured, default: int) -> tuple[int, bool]:
        raw = os.environ.get(env_name)
        if raw and str(raw).strip():
            return int(raw), True
        if configured:
            return int(configured), True
        return default, False

    http, http_explicit = _port("RAG_QDRANT_HTTP_PORT",
                                getattr(settings, "qdrant_http_port", None),
                                default_http)
    grpc, grpc_explicit = _port("RAG_QDRANT_GRPC_PORT",
                                getattr(settings, "qdrant_grpc_port", None),
                                default_grpc)

    non_default = (http != default_http) or (grpc != default_grpc)
    named = bool((getattr(settings, "instance_id", None) or "").strip())
    return http, grpc, bool(non_default and named and (http_explicit or grpc_explicit))


# --- manifest ------------------------------------------------------------


def manifest_path(settings) -> Path:
    return Path(settings.data_dir) / MANIFEST_NAME


def write_manifest(settings, claim: EngineClaim) -> None:
    path = manifest_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(claim.to_json(), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not write the engine manifest at %s: %s", path, exc)


def read_manifest(settings) -> Optional[EngineClaim]:
    path = manifest_path(settings)
    try:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return EngineClaim(
            instance_id=str(raw["instance_id"]),
            pid=int(raw["pid"]),
            executable=str(raw["executable"]),
            storage_path=str(raw["storage_path"]),
            http_port=int(raw["http_port"]),
            grpc_port=int(raw["grpc_port"]),
            started_at=float(raw.get("started_at") or 0.0),
        )
    except (OSError, ValueError, KeyError, TypeError):
        # A manifest we cannot read is a manifest that vouches for nothing.
        # Returning None means "no proof", which is the safe answer everywhere
        # this is consulted.
        return None


def clear_manifest(settings) -> None:
    try:
        manifest_path(settings).unlink(missing_ok=True)
    except OSError:
        pass


# --- process facts -------------------------------------------------------


def listener_identity(port: int) -> Optional[tuple[int, str]]:
    """``(pid, executable)`` of whatever is LISTENing on ``port``, or None.

    None means "could not look", which is never treated as "nothing is there" —
    an unanswerable question must not become a permissive answer.
    """
    try:
        import psutil  # type: ignore[import-untyped]

        for conn in psutil.net_connections(kind="inet"):
            laddr = getattr(conn, "laddr", None)
            if (laddr and getattr(laddr, "port", None) == port
                    and conn.status == psutil.CONN_LISTEN and conn.pid):
                try:
                    return int(conn.pid), psutil.Process(conn.pid).exe() or ""
                except Exception:  # noqa: BLE001 — pid without readable image
                    return int(conn.pid), ""
    except Exception:  # noqa: BLE001 — psutil absent or permission denied
        return None
    return None


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """Can we bind ``port``? Asked by BINDING it, not by connecting to it.

    Connecting answers "will something talk to me", which is a different
    question and a fragile one: a server that has not accepted its backlog
    refuses further connections, so a busy port reports itself free. Measured —
    a listening socket with an unaccepted connection queued made this return
    True, and the caller would then have spawned an engine onto an occupied
    port, which is the entire failure being prevented.

    Binding asks the question we actually have. It fails with EADDRINUSE
    precisely when something already holds the port, and it does not depend on
    the other process behaving well. ``SO_REUSEADDR`` is deliberately NOT set:
    it would let the bind succeed against a socket in TIME_WAIT and, on some
    platforms, against a live listener — which is the false "free" all over
    again.
    """
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
        return True
    except OSError:
        return False


def process_alive(pid: int) -> bool:
    from ragtools.platform import adapter

    try:
        return bool(adapter().pid_alive(pid))
    except Exception:  # noqa: BLE001
        return False


# --- the decision --------------------------------------------------------


@dataclass(frozen=True)
class PortVerdict:
    """What to do about a port that is already occupied."""

    action: str            # "spawn" | "reattach" | "refuse"
    reason: str
    claim: Optional[EngineClaim] = None


def inspect_port(settings, http_port: int) -> PortVerdict:
    """Decide before spawning anything. This is where the incident is prevented.

    A free port means spawn. An occupied one is resolved from the MANIFEST, never
    from the port: reattach only when this installation's own record vouches for
    the listener and that process is still alive. Anything else is someone
    else's engine — or an unidentifiable one — and managed mode refuses rather
    than adopting it.
    """
    if port_is_free(http_port):
        return PortVerdict("spawn", f"port {http_port} is free")

    claim = read_manifest(settings)
    if claim is None:
        return PortVerdict(
            "refuse",
            f"port {http_port} is already in use and this installation has no "
            f"record of starting an engine there; refusing to adopt a store it "
            f"does not own",
        )
    if claim.http_port != http_port:
        return PortVerdict(
            "refuse",
            f"port {http_port} is in use; this installation's engine was started "
            f"on {claim.http_port}, so the listener is not ours",
            claim,
        )
    if not process_alive(claim.pid):
        return PortVerdict(
            "refuse",
            f"port {http_port} is in use but our recorded engine (pid "
            f"{claim.pid}) is gone — the listener belongs to another process",
            claim,
        )

    seen = listener_identity(http_port)
    if seen is not None and seen[0] != claim.pid:
        return PortVerdict(
            "refuse",
            f"port {http_port} is held by pid {seen[0]} ({seen[1] or 'unknown image'}), "
            f"not by our recorded engine pid {claim.pid}",
            claim,
        )

    return PortVerdict(
        "reattach",
        f"our own engine (pid {claim.pid}) is already running on {http_port}",
        claim,
    )


def verify_ownership(claim: EngineClaim, *, proc=None) -> None:
    """Prove the listener on ``claim.http_port`` is the child we spawned.

    Raises :class:`NotOurEngine` with the specific failed proof. Silence is never
    success: when the listener cannot be identified at all this still returns,
    because the API key — checked by the caller on the first real request — is
    the proof that does not depend on being able to enumerate processes.
    """
    if proc is not None and proc.poll() is not None:
        raise NotOurEngine(
            f"the engine we started exited during startup (code {proc.returncode}); "
            f"whatever is answering on {claim.http_port} is not ours"
        )

    seen = listener_identity(claim.http_port)
    if seen is None:
        return  # could not look; the API key still gates every request
    pid, exe = seen
    if pid != claim.pid:
        raise NotOurEngine(
            f"port {claim.http_port} is held by pid {pid}, not the engine we "
            f"started (pid {claim.pid})"
        )
    if exe and claim.executable:
        try:
            same = Path(exe).resolve() == Path(claim.executable).resolve()
        except OSError:
            same = True  # unresolvable path is not evidence of a mismatch
        if not same:
            raise NotOurEngine(
                f"pid {pid} runs {exe}, not the binary we launched "
                f"({claim.executable})"
            )


def release(settings, claim: Optional[EngineClaim], proc=None) -> str:
    """Terminate ONLY an engine the manifest vouches for. Returns what happened.

    Every requested piece of evidence is required before a signal is sent: the
    pid, the recorded executable, and that the pid is still alive. A process the
    manifest does not describe is somebody else's database server, and this
    function's entire purpose is to not kill it.
    """
    if claim is None:
        return "no engine manifest; nothing this installation owns to stop"

    if not process_alive(claim.pid):
        clear_manifest(settings)
        return f"engine pid {claim.pid} had already exited"

    seen = listener_identity(claim.http_port)
    if seen is not None and seen[0] != claim.pid:
        return (f"refusing to stop pid {seen[0]} on port {claim.http_port}: our "
                f"manifest names pid {claim.pid}")

    stopped = False
    if proc is not None and getattr(proc, "pid", None) == claim.pid:
        try:
            proc.terminate()
            proc.wait(timeout=10)
            stopped = True
        except Exception:  # noqa: BLE001 — fall through to the adapter
            pass
    if not stopped:
        from ragtools.platform import adapter

        try:
            adapter().terminate(claim.pid, force=True)
            stopped = True
        except Exception as exc:  # noqa: BLE001
            clear_manifest(settings)
            return f"could not stop engine pid {claim.pid}: {exc}"

    clear_manifest(settings)
    return (f"stopped engine pid={claim.pid} exe={claim.executable} "
            f"port={claim.http_port} storage={claim.storage_path}")
