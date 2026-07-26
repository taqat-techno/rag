"""Development-environment isolation (RAG v3, Stage S0).

A dev or CI runtime must never mount live data or place a vector store on a
synced / FUSE / network path — Qdrant's own filesystem check (v1.15.0+)
documents *silent data loss* on FUSE, and this workspace syncs via Syncthing.
These guards enforce that in code, not by convention, and are pure +
dependency-injected so they can be unit-tested without any real service,
port, directory, or platform assumption.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S0 -> G0)
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Union

# --- Runtime profiles ----------------------------------------------------

PROFILE_INSTALLED = "installed"
PROFILE_DEV = "dev"
PROFILE_CI = "ci"
PROFILE_GATEWAY = "gateway"

ALL_PROFILES = frozenset(
    {PROFILE_INSTALLED, PROFILE_DEV, PROFILE_CI, PROFILE_GATEWAY}
)
#: Profiles whose data persists and whose target IS a live directory.
PERSISTENT_PROFILES = frozenset({PROFILE_INSTALLED, PROFILE_GATEWAY})
#: Profiles that must be isolated and disposable.
EPHEMERAL_PROFILES = frozenset({PROFILE_DEV, PROFILE_CI})

#: Loopback ports owned by live services on the author's machine; a dev/ci
#: runtime must never bind these. 21420 = installed owner service,
#: 21422 = Roy / restricted-bot runtime.
RESERVED_LIVE_PORTS = frozenset({21420, 21422})


class IsolationError(RuntimeError):
    """A dev/ci runtime was about to touch live data or an unsafe location."""


_PathLike = Union[str, os.PathLike]


def _norm(p: _PathLike) -> Path:
    """Absolute, user-expanded, symlink-resolved, ``..``-collapsed path.

    Tolerates non-existent paths (the dev data dir usually does not exist
    yet) so the guard can run before anything is created.
    """
    return Path(os.path.expanduser(os.fspath(p))).resolve()


#: Marker directory Syncthing places at the root of every synced folder.
_SYNC_MARKERS = (".stfolder",)


def default_synced_detector(
    path: _PathLike, *, markers: Iterable[str] = _SYNC_MARKERS
) -> bool:
    """True if ``path`` or any ancestor is the root of a synced folder.

    The reliable, cross-platform signal for Syncthing is a ``.stfolder``
    marker at the synced-folder root; everything beneath it is synced. We
    therefore check ``path`` itself and every ancestor up to the filesystem
    root (a finite walk). Deeper FUSE/network detection can extend this later;
    Syncthing is the concrete risk in this workspace.
    """
    p = _norm(path)
    for ancestor in (p, *p.parents):
        for marker in markers:
            try:
                if (ancestor / marker).exists():
                    return True
            except OSError:
                # Unreadable ancestor (permissions, disconnected drive):
                # treat as "no marker here" and keep walking.
                continue
    return False


def assert_dev_data_dir_isolated(
    candidate: _PathLike,
    *,
    live_dirs: Iterable[_PathLike] = (),
    is_synced_path: Callable[[Path], bool] | None = None,
) -> None:
    """Raise :class:`IsolationError` if ``candidate`` is unsafe for dev/ci.

    Unsafe means any of:

    * it equals, is nested within, or would *contain* a known live data dir
      (mounting or swallowing live data), or
    * it lies on a synced / FUSE / network path (``is_synced_path`` true),
      where Qdrant documents silent corruption.

    All comparisons are made on normalized absolute paths. ``live_dirs`` and
    ``is_synced_path`` are injected so this stays pure and testable.
    """
    cand = _norm(candidate)
    for live in live_dirs:
        lv = _norm(live)
        if cand == lv or cand.is_relative_to(lv) or lv.is_relative_to(cand):
            raise IsolationError(
                f"refusing dev/ci data dir {cand}: collides with live data dir "
                f"{lv}. A dev/ci runtime must never mount or contain live data."
            )
    if is_synced_path is not None and is_synced_path(cand):
        raise IsolationError(
            f"refusing dev/ci data dir {cand}: it lies on a synced / FUSE / "
            "network path, where Qdrant's filesystem check documents silent "
            "data loss. Use a local, non-synced location."
        )


def assert_dev_port_allowed(port: int) -> None:
    """Raise :class:`IsolationError` if ``port`` is a reserved live-service port.

    ``0`` (OS-assigned ephemeral) and any non-reserved port are allowed.
    """
    if int(port) in RESERVED_LIVE_PORTS:
        raise IsolationError(
            f"refusing port {port}: reserved for a live service "
            f"{sorted(RESERVED_LIVE_PORTS)}. A dev/ci runtime must use an "
            "ephemeral (0) or explicitly non-reserved port."
        )


# --- Runtime profile + isolated dev data dir -----------------------------


def resolve_profile(
    env: Mapping[str, str], *, packaged: bool = False
) -> str:
    """Resolve the runtime profile from ``env`` (``RAG_PROFILE``).

    Explicit ``RAG_PROFILE`` wins (case-insensitive, trimmed) and must be one
    of :data:`ALL_PROFILES`. With none set, a packaged build defaults to
    ``installed`` and a source checkout to ``dev`` — a source checkout never
    silently assumes the installed profile.
    """
    raw = (env.get("RAG_PROFILE") or "").strip().lower()
    if raw:
        if raw not in ALL_PROFILES:
            raise ValueError(
                f"unknown RAG_PROFILE {raw!r}; expected one of "
                f"{sorted(ALL_PROFILES)}"
            )
        return raw
    return PROFILE_INSTALLED if packaged else PROFILE_DEV


def _default_dev_base() -> Path:
    """A local, non-synced root for dev data, distinct from the installed dir.

    Deliberately ``RAGTools-dev`` (not ``RAGTools``) so a dev runtime can
    never resolve onto the installed application's data directory.
    """
    # The dev root must never coincide with the installed root — that is what
    # lets a development run corrupt a live index. The adapter guarantees the
    # two differ on every platform (asserted in test_platform_adapters).
    from ragtools.platform import adapter

    return adapter().dev_dir()


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str) -> str:
    return _SLUG_RE.sub("-", name).strip("-") or "wt"


def dev_data_dir(
    *,
    worktree_root: _PathLike,
    base: Optional[_PathLike] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """Compute an isolated dev data dir keyed by ``worktree_root``.

    Parallel worktrees get distinct, deterministic directories so they never
    collide. An explicit ``RAG_DATA_DIR`` override wins (the caller then owns
    isolation, but the guard still runs). This does not create anything.
    """
    env = env if env is not None else os.environ
    explicit = env.get("RAG_DATA_DIR")
    if explicit:
        return _norm(explicit)
    base_dir = _norm(base) if base is not None else _norm(_default_dev_base())
    root = _norm(worktree_root)
    key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    return base_dir / f"{_slug(root.name)}-{key}"


def resolve_and_verify_dev_data_dir(
    *,
    worktree_root: _PathLike,
    base: Optional[_PathLike] = None,
    live_dirs: Iterable[_PathLike] = (),
    env: Optional[Mapping[str, str]] = None,
    is_synced_path: Callable[[Path], bool] = default_synced_detector,
) -> Path:
    """Compute the dev data dir and refuse it if unsafe. The S0 entry point.

    Returns the verified, isolated dev data dir, or raises
    :class:`IsolationError` if it would collide with live data or sit on a
    synced/FUSE path.
    """
    d = dev_data_dir(worktree_root=worktree_root, base=base, env=env)
    assert_dev_data_dir_isolated(
        d, live_dirs=live_dirs, is_synced_path=is_synced_path
    )
    return d


def profile_may_autostart(profile: str) -> bool:
    """Only persistent profiles register OS autostart. Dev/ci never do."""
    return profile in PERSISTENT_PROFILES


@dataclass
class DevEnvironment:
    """A bootstrapped, disposable dev/ci runtime environment.

    ``teardown`` is guaranteed and safety-guarded: it will only ever remove a
    directory that lives inside its ``base``, so a mis-constructed environment
    can never delete live data.
    """

    profile: str
    worktree_root: Path
    data_dir: Path
    port: int
    base: Optional[Path] = None

    def teardown(self) -> None:
        d = _norm(self.data_dir)
        if self.base is not None:
            b = _norm(self.base)
            if d != b and not d.is_relative_to(b):
                raise IsolationError(
                    f"refusing teardown of {d}: it is not within the dev base "
                    f"{b}. teardown never deletes outside the dev base."
                )
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


def bootstrap_dev_environment(
    *,
    worktree_root: _PathLike,
    base: Optional[_PathLike] = None,
    env: Optional[Mapping[str, str]] = None,
    port: int = 0,
    live_dirs: Iterable[_PathLike] = (),
    is_synced_path: Callable[[Path], bool] = default_synced_detector,
    create: bool = True,
) -> DevEnvironment:
    """Resolve, verify, and (optionally) create an isolated dev/ci runtime.

    Refuses — before creating anything — a persistent profile, a reserved
    live port, a synced/FUSE base, or a data dir that collides with live data.
    The returned :class:`DevEnvironment` tears down cleanly and never autostarts.
    """
    env = env if env is not None else os.environ
    profile = resolve_profile(env)
    if profile not in EPHEMERAL_PROFILES:
        raise IsolationError(
            f"bootstrap_dev_environment is only for ephemeral profiles "
            f"{sorted(EPHEMERAL_PROFILES)}; got {profile!r} (persistent). "
            "Refusing to bootstrap a disposable environment for it."
        )
    assert_dev_port_allowed(port)
    data_dir = resolve_and_verify_dev_data_dir(
        worktree_root=worktree_root,
        base=base,
        live_dirs=live_dirs,
        env=env,
        is_synced_path=is_synced_path,
    )
    resolved_base = _norm(base) if base is not None else _norm(_default_dev_base())
    if create:
        data_dir.mkdir(parents=True, exist_ok=True)
    return DevEnvironment(
        profile=profile,
        worktree_root=_norm(worktree_root),
        data_dir=data_dir,
        port=int(port),
        base=resolved_base,
    )
