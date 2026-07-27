"""Standard streams for a build that has no console.

A Windows GUI-subsystem executable is not given a console, and CPython therefore
sets ``sys.stdout``, ``sys.stderr`` and ``sys.stdin`` to ``None``. That is fine
for ``print()`` — the builtin short-circuits on a ``None`` file — and fatal for
almost everything else: ``logging.StreamHandler`` binds ``sys.stderr`` at
construction and raises ``AttributeError: 'NoneType' object has no attribute
'write'`` on its first record, ``rich.Console`` interrogates the stream it is
given, and any ``subprocess`` call that inherits a stream needs a real
``fileno()``.

So the windowed entry point binds the missing streams to the null device before
anything else runs. The null device is deliberate: it is a real OS handle with a
real file descriptor, so it survives being handed to a child process, which an
``io.StringIO`` would not.

This exists because ``ragw.exe`` (see ``rag.spec``) runs the *same* script as
``rag.exe``. One binary is console-subsystem and one is not, and the difference
has to be detected at runtime rather than compiled in — which is exactly what a
missing ``sys.stdout`` is: a reliable, self-describing signal that this process
has nowhere to write.
"""

from __future__ import annotations

import os
import sys

#: ``(attribute, open mode)`` for each stream, in the order a reader expects.
_STREAMS = (("stdin", "r"), ("stdout", "w"), ("stderr", "w"))


def ensure_std_streams() -> list[str]:
    """Bind any ``None`` standard stream to the null device.

    Returns the names that were rebound — empty on a console build, which is the
    normal case and must stay a no-op. Idempotent, and never raises: a process
    that cannot open the null device still has to start.
    """
    rebound: list[str] = []
    for name, mode in _STREAMS:
        if getattr(sys, name, None) is not None:
            continue
        try:
            handle = open(os.devnull, mode, encoding="utf-8", errors="replace")
        except OSError:
            # Nothing sensible left to try. Leaving the stream as None is no
            # worse than the state we were handed.
            continue
        setattr(sys, name, handle)
        # `sys.__stderr__` is what logging falls back to when a handler fails,
        # and what a traceback is printed through. Leaving it None turns a
        # recoverable logging error into a second, more confusing one.
        if getattr(sys, f"__{name}__", None) is None:
            setattr(sys, f"__{name}__", handle)
        rebound.append(name)
    return rebound
