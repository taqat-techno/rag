"""Refuse an upgrade that cannot finish, before it stops anything.

A migration that runs out of disk half-way through has done the worst possible
thing: the old index is gone, the new one is incomplete, and the machine is on
the far side of the rollback boundary. Every check here answers "can this
finish?" *before* the first service is stopped.

The numbers are measured, not guessed. The installed collection on the machine
this was written for is 147,344 points occupying 1.2 GB, which is ~8.5 KB per
point including the state DB, the HNSW graph and Qdrant's segment overhead. The
estimate below uses that ratio with a safety multiple, because being wrong in
the optimistic direction here is what strands a machine.

Every check reports rather than throws, so the user sees the full picture in one
pass instead of fixing one blocker at a time.
"""

from __future__ import annotations

import os
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

#: Bytes of storage observed per indexed point, measured on the real corpus
#: (1.2 GB / 147,344 points). Includes vectors, payload, segments and state DB.
BYTES_PER_POINT = 8_500

#: How much headroom to demand over the estimate. A re-index writes the new
#: store while the old one still exists, so peak usage is roughly double, and
#: Qdrant's optimizer needs working room on top of that.
DISK_SAFETY_MULTIPLE = 3.0

#: Floor, so a small index still leaves room for logs, backups and the model.
DISK_FLOOR_BYTES = 2 * 1024**3

#: The streaming indexer is O(window), not O(corpus) — this is the working set
#: for the encoder plus one batch, not the corpus size.
MIN_AVAILABLE_MEMORY_BYTES = 2 * 1024**3

OK = "ok"
WARN = "warn"
BLOCK = "block"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    remedy: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == BLOCK


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.blocking]

    @property
    def ok(self) -> bool:
        return not self.blockers

    def add(self, check: Check) -> None:
        self.checks.append(check)

    def render(self) -> str:
        lines = []
        for check in self.checks:
            mark = {OK: "ok  ", WARN: "warn", BLOCK: "BLOCK"}[check.status]
            lines.append(f"  [{mark}] {check.name}: {check.detail}")
            if check.remedy and check.status != OK:
                lines.append(f"           -> {check.remedy}")
        return "\n".join(lines)


def estimate_required_bytes(point_count: int) -> int:
    """Disk the migration needs, including the old store it must not delete yet."""
    return max(int(point_count * BYTES_PER_POINT * DISK_SAFETY_MULTIPLE), DISK_FLOOR_BYTES)


def check_disk(target: Path, point_count: int, *, usage=None) -> Check:
    """Enough room for the new store while the old one still exists.

    The old data directory is renamed rather than deleted, precisely so a failed
    migration is recoverable — which means both copies coexist at peak.
    """
    required = estimate_required_bytes(point_count)
    try:
        free = (usage or shutil.disk_usage)(str(target)).free
    except OSError as exc:
        return Check("disk", WARN, f"could not measure free space ({exc})",
                     "check available space manually before continuing")
    gb = 1024**3
    detail = f"{free / gb:.1f} GB free, {required / gb:.1f} GB required " \
             f"for {point_count:,} points"
    if free < required:
        return Check("disk", BLOCK, detail,
                     f"free at least {(required - free) / gb:.1f} GB, or move the "
                     "data directory to a larger volume")
    if free < required * 1.5:
        return Check("disk", WARN, detail, "the margin is thin; consider freeing more")
    return Check("disk", OK, detail)


def check_memory(*, available: Optional[int] = None) -> Check:
    """Enough RAM for the encoder and one index window.

    The indexer streams in bounded windows — peak RSS on the 38k-file corpus is
    ~1.2 GB flat, not proportional to the corpus — so this is a modest floor
    rather than a function of index size.
    """
    if available is None:
        try:
            import psutil

            available = psutil.virtual_memory().available
        except Exception:  # noqa: BLE001 — psutil is optional
            return Check("memory", WARN, "could not measure available memory",
                         "ensure ~2 GB is free before migrating")
    gb = 1024**3
    detail = f"{available / gb:.1f} GB available"
    if available < MIN_AVAILABLE_MEMORY_BYTES:
        return Check("memory", BLOCK, detail,
                     "close other applications, or migrate one project at a time")
    return Check("memory", OK, detail)


def check_port(host: str, port: int, *, probe=None) -> Check:
    """The service port is free, or already ours.

    A port conflict discovered after the old service is stopped leaves the
    machine with neither running.
    """
    def _default_probe(h, p):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex((h, p)) == 0

    try:
        in_use = (probe or _default_probe)(host, port)
    except Exception as exc:  # noqa: BLE001
        return Check("port", WARN, f"could not probe {host}:{port} ({exc})")
    if in_use:
        # Expected during an upgrade: our own old service still holds it. The
        # stop sequence releases it; a foreign listener does not.
        return Check("port", WARN, f"{host}:{port} is in use",
                     "the upgrade stops the previous service first; if something "
                     "else owns this port, choose another with --port")
    return Check("port", OK, f"{host}:{port} is free")


def check_projects(projects: Iterable) -> Check:
    """Every registered project still resolves to a directory.

    A project whose folder was moved or deleted is reported and skipped — never
    silently dropped, because "my project vanished after the upgrade" is
    indistinguishable from data loss to the person it happens to.
    """
    missing = []
    total = 0
    for project in projects:
        total += 1
        try:
            if not Path(project.path).is_dir():
                missing.append(project.id)
        except OSError:
            missing.append(project.id)
    if not total:
        return Check("projects", WARN, "no projects registered",
                     "nothing to migrate; add projects after upgrading")
    if missing:
        return Check(
            "projects", WARN,
            f"{total - len(missing)}/{total} resolve; missing: {', '.join(missing)}",
            "these are skipped and reported, not removed from config — "
            "re-point or remove them after the upgrade",
        )
    return Check("projects", OK, f"{total} project(s) resolve")


def check_model(settings, *, expected: Optional[str] = None) -> Check:
    """The embedding model matches what the new collections will be built with.

    A model change invalidates every vector; discovering that after re-indexing
    44,000 files is an expensive way to learn it.
    """
    configured = getattr(settings, "embedding_model", "")
    target = expected or configured
    if configured != target:
        return Check("model", BLOCK,
                     f"configured {configured!r} but target is {target!r}",
                     "align RAG_EMBEDDING_MODEL with the release default before migrating")
    return Check("model", OK, configured)


def run_preflight(
    settings,
    *,
    point_count: int = 0,
    data_dir: Optional[Path] = None,
    usage=None,
    available_memory: Optional[int] = None,
    port_probe=None,
) -> PreflightReport:
    """Every gate, in one pass, reporting rather than raising.

    Fixing one blocker at a time across five separate failed attempts is a
    miserable way to upgrade; the user gets the whole list at once.
    """
    report = PreflightReport()
    target = Path(data_dir or getattr(settings, "data_dir", ".") or ".")
    report.add(check_disk(target, point_count, usage=usage))
    report.add(check_memory(available=available_memory))
    report.add(check_port(getattr(settings, "service_host", "127.0.0.1"),
                          int(getattr(settings, "service_port", 21420)),
                          probe=port_probe))
    report.add(check_projects(getattr(settings, "projects", []) or []))
    report.add(check_model(settings))
    return report
