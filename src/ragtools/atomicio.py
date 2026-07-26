"""Atomic file writes (RAG v3, Stage S1 / A4).

Write fully-formed bytes to a temp file on the same filesystem, fsync, then
``os.replace`` (atomic) onto the target. A failure — full disk, permission
error, process kill mid-write — can never leave a partial or empty target;
the original file is untouched until the atomic replace succeeds. Reused by
config, state, and registry writers across S5/S16.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S1/A4 -> G1)
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Union

_PathLike = Union[str, os.PathLike]


def atomic_write_bytes(
    path: _PathLike, data: bytes, *, backup: bool = False
) -> None:
    """Atomically write ``data`` to ``path``.

    Creates parent directories as needed. With ``backup=True`` an existing
    target is copied to ``<name>.bak`` before the replace. The bytes must be
    fully serialized by the caller: serialization errors then never touch the
    target file at all.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if backup and target.exists():
        shutil.copyfile(target, target.with_name(target.name + ".bak"))

    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)  # atomic on a single filesystem
    except BaseException:
        # Interruption anywhere above: drop the temp, leave the target as-is.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
