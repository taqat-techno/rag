"""Replacing a previous installation cleanly.

Split by intent, because the difference matters when something goes wrong:

* :mod:`ragtools.upgrade.scan` — **sees**. Pure inspection, produces a plan a
  human reads before a single process is stopped.
* :mod:`ragtools.upgrade.migrate` — **transforms**. Config v2 → v3 and PATH
  repair, each returning exactly what it would write so a dry run and a real
  run cannot disagree.
* :mod:`ragtools.upgrade.preflight` — **refuses**. Every "can this finish?"
  gate, run before a single service is stopped.
* :mod:`ragtools.upgrade.reconcile` — **proves**. The five properties that must
  hold before an upgrade may be called successful.
* :mod:`ragtools.upgrade.state` — **remembers**. Where it got to, and which side
  of the rollback boundary the machine is on.

Nothing in this package removes anything on import, and no function mutates the
machine unless its name says so.
"""

from ragtools.upgrade.migrate import (
    CONFIG_VERSION,
    MigrationResult,
    PathRepair,
    migrate_config,
    repair_path,
)
from ragtools.upgrade.preflight import PreflightReport, run_preflight
from ragtools.upgrade.reconcile import ReconcileReport, reconcile
from ragtools.upgrade.state import STEPS, UpgradeState
from ragtools.upgrade.scan import (
    Finding,
    ScanResult,
    is_development_path,
    scan,
    summarize,
)

__all__ = [
    "scan", "summarize", "is_development_path", "Finding", "ScanResult",
    "migrate_config", "repair_path", "MigrationResult", "PathRepair",
    "CONFIG_VERSION",
    "run_preflight", "PreflightReport",
    "reconcile", "ReconcileReport",
    "UpgradeState", "STEPS",
]
