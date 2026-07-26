"""Where an upgrade got to, so an interrupted one can be resumed or repaired.

The question a half-finished upgrade has to answer is *"can I go back?"*, and it
must never require reading code. That is what :data:`BOUNDARY_STEP` encodes:

* **before** the first write to the new store — full rollback. Old binaries and
  old data are intact.
* **after** it — forward-only. Recovery is resume/repair, and the previous data
  directory is retained (renamed, never deleted) until an explicit
  ``rag upgrade commit``.

State is written after every step with an atomic replace, because a crash
*during* the bookkeeping is exactly when the file matters most. A torn state
file would leave the machine in a state nothing can classify.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

STEP_SCANNED = "scanned"
STEP_PREFLIGHT = "preflight"
STEP_STOPPED = "stopped"
STEP_BACKED_UP = "backed-up"
STEP_BINARIES = "binaries-installed"
STEP_CONFIG = "config-migrated"
STEP_STORE_STARTED = "store-started"
STEP_INDEXING = "indexing"
STEP_RECONCILED = "reconciled"
STEP_COMMITTED = "committed"

#: The ordered pipeline. Position in this list is what makes "resume from where
#: it stopped" a lookup rather than a guess.
STEPS = [
    STEP_SCANNED, STEP_PREFLIGHT, STEP_STOPPED, STEP_BACKED_UP,
    STEP_BINARIES, STEP_CONFIG, STEP_STORE_STARTED, STEP_INDEXING,
    STEP_RECONCILED, STEP_COMMITTED,
]

#: The first step that writes to the NEW store. Reaching it crosses the
#: rollback boundary.
BOUNDARY_STEP = STEP_INDEXING


@dataclass
class UpgradeState:
    """Durable record of an in-progress upgrade."""

    from_version: str = ""
    to_version: str = ""
    completed: list[str] = field(default_factory=list)
    #: Projects that reconciled, and ones that did not — by name, always.
    reconciled: list[str] = field(default_factory=list)
    failed: dict = field(default_factory=dict)
    #: Where the previous data directory was moved to. The rollback path.
    backup_path: str = ""
    started_at: str = ""
    updated_at: str = ""

    # --- position -------------------------------------------------------

    @property
    def last_step(self) -> str:
        return self.completed[-1] if self.completed else ""

    @property
    def next_step(self) -> Optional[str]:
        """The step to run now, or None when the upgrade is finished."""
        for step in STEPS:
            if step not in self.completed:
                return step
        return None

    @property
    def past_boundary(self) -> bool:
        """True once anything has been written to the new store."""
        return BOUNDARY_STEP in self.completed

    @property
    def can_rollback(self) -> bool:
        return not self.past_boundary

    def explain(self) -> str:
        """The answer to "what happened, and can I go back?" in one string."""
        if not self.completed:
            return "not started"
        if self.next_step is None:
            return f"complete ({self.from_version} -> {self.to_version})"
        position = f"stopped after {self.last_step}; next step is {self.next_step}"
        if self.can_rollback:
            return (f"{position}. Rollback is available — nothing has been written "
                    f"to the new store yet: `rag upgrade rollback`.")
        return (f"{position}. Past the data boundary, so this is forward-only: "
                f"`rag upgrade resume`. The previous data directory is retained "
                f"at {self.backup_path or '(unrecorded)'} until `rag upgrade commit`.")

    # --- transitions ----------------------------------------------------

    def complete(self, step: str, *, clock=None) -> "UpgradeState":
        """Record a finished step. Idempotent — resuming re-runs the last step
        safely rather than requiring exactly-once semantics from every action."""
        if step not in STEPS:
            raise ValueError(f"unknown upgrade step {step!r}")
        if step not in self.completed:
            self.completed.append(step)
            self.completed.sort(key=STEPS.index)
        self.updated_at = _now(clock)
        return self

    def record_project(self, project_id: str, ok: bool, reason: str = "",
                       recovery: str = "") -> None:
        """Every project ends up in exactly one of two lists, by name.

        A project that is neither reconciled nor reported as failed is the
        outcome this refuses to allow: it looks like success and is not.
        """
        self.reconciled = [p for p in self.reconciled if p != project_id]
        self.failed.pop(project_id, None)
        if ok:
            self.reconciled.append(project_id)
        else:
            self.failed[project_id] = {"reason": reason, "recovery": recovery}

    @property
    def complete_and_reconciled(self) -> bool:
        """Success is every project accounted for AND none failed."""
        return self.next_step is None and not self.failed

    # --- persistence ----------------------------------------------------

    def save(self, path: Path) -> None:
        """Atomic write. A crash mid-bookkeeping is exactly when this matters."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self), indent=2, sort_keys=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(payload, encoding="utf-8")
        temp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "UpgradeState":
        """Read state, or a fresh one. A corrupt file is treated as absent:
        refusing to start because the bookkeeping is unreadable would strand the
        machine on the strength of the least important file involved."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def _now(clock=None) -> str:
    from datetime import datetime, timezone

    return (clock or (lambda: datetime.now(timezone.utc)))().isoformat(timespec="seconds")
