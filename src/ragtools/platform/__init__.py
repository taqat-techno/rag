"""The one place the product asks which operating system it is on.

Everything else imports :func:`adapter` and stops caring. The rule this package
exists to enforce is mechanical and checkable: **no ``sys.platform`` branch may
live outside ``ragtools.platform``** — there is a test that greps for it, so the
seam cannot quietly leak back the way it did before (thirteen modules, three of
them Windows-only with no other path).

An unknown platform raises :class:`~ragtools.platform.base.PlatformUnsupported`
rather than falling through to a POSIX-shaped guess. The managed-Qdrant asset
matrix already refuses unknown platform/arch pairs instead of inventing an asset
name; guessing how a machine starts services fails later and more expensively —
at reboot, on someone else's computer.

Note on the name: this package is ``ragtools.platform``, and modules inside it
still ``import platform`` to reach the standard library. Python 3's absolute
imports keep those distinct.
"""

from __future__ import annotations

import sys
from typing import Optional

from ragtools.platform.base import (
    KIND_SCHEDULE,
    KIND_SERVICE,
    KIND_TRAY,
    AutostartSpec,
    CommandResult,
    CommandRunner,
    PlatformAdapter,
    PlatformUnsupported,
    Registration,
    default_runner,
)

__all__ = [
    "adapter", "resolve_adapter", "reset_adapter", "current_platform",
    "host_system", "host_machine",
    "AutostartSpec", "Registration", "PlatformAdapter", "PlatformUnsupported",
    "CommandResult", "CommandRunner", "default_runner",
    "KIND_SERVICE", "KIND_TRAY", "KIND_SCHEDULE",
    "DuplicateRegistration", "assert_single_registration",
]

_cached: Optional[PlatformAdapter] = None


def current_platform(platform_name: Optional[str] = None) -> str:
    """Normalise ``sys.platform`` to ``windows`` | ``linux`` | ``darwin``."""
    raw = (platform_name or sys.platform).lower()
    # cygwin and msys are Windows hosts with a POSIX-looking sys.platform;
    # dispatching them to the Linux adapter would look for systemd.
    if raw.startswith(("win", "cygwin", "msys")):
        return "windows"
    if raw == "darwin":
        return "darwin"
    if raw.startswith("linux"):
        return "linux"
    return raw


def host_system() -> str:
    """Host OS as the Qdrant asset matrix names it (``Windows``/``Linux``/``Darwin``).

    Distinct from :func:`current_platform`, which answers "which adapter". This
    answers "which build artifact", and the asset matrix is a pure
    (system, machine) -> asset table that must stay callable for a TARGET
    platform other than the host — that is what lets a Linux package be
    validated from any build machine.
    """
    import platform as _stdlib

    return _stdlib.system()


def host_machine() -> str:
    """Host CPU architecture (``AMD64``, ``x86_64``, ``arm64``, ``aarch64``)."""
    import platform as _stdlib

    return _stdlib.machine()


def resolve_adapter(platform_name: Optional[str] = None, **kwargs) -> PlatformAdapter:
    """Build a fresh adapter. ``kwargs`` are passed through — tests inject
    temp roots and a fake runner so the suite never touches a real scheduler,
    Startup folder, unit directory or LaunchAgents directory."""
    name = current_platform(platform_name)
    if name == "windows":
        from ragtools.platform.windows import WindowsAdapter

        return WindowsAdapter(**kwargs)
    if name == "linux":
        from ragtools.platform.linux import LinuxAdapter

        return LinuxAdapter(**kwargs)
    if name == "darwin":
        from ragtools.platform.darwin import DarwinAdapter

        return DarwinAdapter(**kwargs)
    raise PlatformUnsupported(
        f"no platform adapter for {name!r}. Supported: windows, linux, darwin. "
        "Refusing to guess how this system starts services."
    )


def adapter() -> PlatformAdapter:
    """The process-wide adapter for the host platform."""
    global _cached
    if _cached is None:
        _cached = resolve_adapter()
    return _cached


def reset_adapter() -> None:
    """Drop the cached adapter (tests)."""
    global _cached
    _cached = None


class DuplicateRegistration(RuntimeError):
    """More than one autostart registration exists for one concern.

    The failure this names is not hypothetical: the development machine carries
    a ``RAGTools Service`` task, a ``RAGTools Watchdog`` task, a
    ``RAGTools.vbs`` and a ``RAGTools-Tray.vbs`` simultaneously, because every
    upgrade added a mechanism without being able to see the previous ones.
    """


def assert_single_registration(
    registrations: list[Registration], kind: str
) -> Registration:
    """Return the one live registration for ``kind``, or explain what is wrong.

    Legacy entries are reported separately from current ones, because the two
    demand different actions: a duplicate *current* registration is a bug in
    this code, while a surviving *legacy* registration is upgrade work that has
    not been done.
    """
    current = [r for r in registrations if not r.legacy]
    legacy = [r for r in registrations if r.legacy]
    if legacy:
        raise DuplicateRegistration(
            f"{len(legacy)} superseded {kind} registration(s) still present: "
            + ", ".join(r.describe() for r in legacy)
            + ". Run `rag upgrade apply` to remove them."
        )
    if not current:
        raise DuplicateRegistration(f"no {kind} autostart registration found")
    if len(current) > 1:
        raise DuplicateRegistration(
            f"{len(current)} {kind} registrations found, expected exactly one: "
            + ", ".join(r.describe() for r in current)
        )
    return current[0]
