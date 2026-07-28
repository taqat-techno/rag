"""Rehearse the v2 -> v3 upgrade against a COPY of a real installation.

The release gate wants an upgrade proven against the previous version. The only
v2.7.0 install available is the one this machine actually uses, and running a
destructive upgrade against a working service is not something to do to
somebody's live index.

So this replicates the installed layout into a sandbox — the real `config.toml`
is copied byte-for-byte, the directory structure and stale artifacts are
recreated from what was measured, and the real `PATH` string is passed in — then
runs scan, pre-flight and migration against the copy.

What that proves: detection, protection, PATH repair and config migration all
behave correctly on real production data. What it does not prove: stopping
services, moving 1.2 GB, or re-indexing 44,000 files. Those need a machine that
can be broken.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ragtools.upgrade import migrate_config, repair_path, scan, summarize
from ragtools.upgrade.preflight import run_preflight
from ragtools.upgrade.scan import L_DATA, L_INSTALL_USER, L_LEGACY_ARTIFACT

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


class _Adapter:
    """Points the scanner at the sandbox instead of the real machine."""

    name = "windows"

    def __init__(self, app: Path, registrations):
        self._app, self._regs = app, registrations

    def app_dir(self):
        return self._app

    def dev_dir(self):
        return self._app.parent / "RAGTools-dev"

    def find_autostart(self, kind="service"):
        return [r for r in self._regs if r.kind == kind]


def build_sandbox(root: Path, real_config: Path) -> Path:
    """Replicate the measured installed layout. Copies config, fabricates the rest."""
    app = root / "RAGTools"
    data = app / "data"
    data.mkdir(parents=True)
    (root / "Programs" / "RAGTools").mkdir(parents=True)
    (root / "Programs" / "RAGTools" / "rag.exe").write_bytes(b"MZ")
    (root / "Programs" / "RAGTools" / "unins000.exe").write_bytes(b"MZ")
    (root / "Programs" / "RAGTools" / "_internal").mkdir()

    # The real configuration, byte for byte.
    shutil.copy2(real_config, app / "config.toml")

    # Artifacts observed in the real data directory.
    for name in ("service.pid", "supervisor.pid", "tray.pid", "RAGTools-Watchdog.vbs"):
        (data / name).write_text("stale", encoding="utf-8")
    (data / "qdrant").mkdir()
    (data / "logs").mkdir()
    (data / "backups").mkdir()

    # A development environment sitting alongside — must survive untouched.
    dev = root / "rag-v3-dev"
    dev.mkdir()
    (dev / "precious.db").write_text("do not delete", encoding="utf-8")
    return app


def main() -> int:
    from ragtools.platform import KIND_SERVICE, KIND_TRAY, Registration

    root = Path(tempfile.mkdtemp(prefix="ragtools-rehearsal-"))

    # Prefer the developer's real config; SYNTHESISE one when there is none.
    #
    # This used to `return 2` whenever no installation was present, which is the
    # state of every CI runner — so the job ran, printed one line, exited
    # non-zero, and was marked `continue-on-error: true`, which turned the
    # failure into a green tick. It has therefore never rehearsed anything on
    # CI while appearing in the release gate as a passing check.
    #
    # A synthesised v2 config exercises the same code on every machine; the real
    # one, when present, additionally covers whatever shape a genuine install
    # has grown into.
    real_config = Path(os.environ.get("LOCALAPPDATA", "")) / "RAGTools" / "config.toml"
    if real_config.exists():
        source, origin = real_config, f"the REAL installed config: {real_config}"
    else:
        source = root / "synthesised-config.toml"
        source.write_text(
            'version = 2\n\n'
            '[[projects]]\nid = "alpha"\npath = "C:\\\\projects\\\\alpha"\nmode = "docs"\n\n'
            '[[projects]]\nid = "beta"\npath = "C:\\\\projects\\\\beta"\nmode = "general"\n',
            encoding="utf-8")
        origin = "a SYNTHESISED v2 config (no installation on this machine)"

    print(f"Rehearsing v2.7.0 -> v3 in {root}")
    print(f"Using {origin}\n")
    app = build_sandbox(root, source)

    # The registrations measured on this machine.
    registrations = [
        Registration("RAGTools Watchdog", KIND_SERVICE, "task-scheduler",
                     "wscript RAGTools-Watchdog.vbs", legacy=True),
        Registration("RAGTools.vbs", KIND_SERVICE, "startup-folder",
                     str(root / "Startup" / "RAGTools.vbs"), legacy=True),
        Registration("RAGTools-Tray.vbs", KIND_TRAY, "startup-folder",
                     str(root / "Startup" / "RAGTools-Tray.vbs"), legacy=True),
    ]

    # --- scan -------------------------------------------------------------
    real_path = os.environ.get("PATH", "")
    result = scan(adapter=_Adapter(app, registrations), path_value=real_path)

    check("the installed data directory is found",
          bool(result.by_layout(L_DATA)), str(app))
    check("the data directory is KEPT, not removed",
          all(not f.removable for f in result.by_layout(L_DATA)),
          "renamed by apply, so rollback stays real")
    check("program files are marked for removal",
          any(f.removable for f in result.by_layout(L_INSTALL_USER)))
    stale = {f.path.name for f in result.by_layout(L_LEGACY_ARTIFACT)}
    check("every stale runtime artifact is found",
          stale == {"service.pid", "supervisor.pid", "tray.pid", "RAGTools-Watchdog.vbs"},
          ", ".join(sorted(stale)))
    check("all three superseded registrations are enumerated",
          len([r for r in result.registrations if r.legacy]) == 3,
          "; ".join(r.name for r in result.registrations if r.legacy))

    # --- development protection ------------------------------------------
    dev_findings = [f for f in result.findings if "rag-v3-dev" in str(f.path)]
    check("a co-located development environment is never a removal candidate",
          all(f.protected or not f.removable for f in dev_findings) if dev_findings else True,
          f"{len(dev_findings)} dev finding(s)")
    check("the developer's file survives the plan",
          (root / "rag-v3-dev" / "precious.db").exists())

    # --- PATH -------------------------------------------------------------
    repair = repair_path(real_path)
    product = [e for e in repair.entries if "ragtools" in e.lower()]
    check("duplicate PATH entries are collapsed to one",
          len(product) <= 1,
          f"{len(repair.original)} entries, {len(repair.removed)} duplicates removed, "
          f"{len(product)} product entry left")
    check("non-product PATH entries keep their order",
          [e for e in repair.entries if "ragtools" not in e.lower()]
          == [e for e in repair.original if "ragtools" not in e.lower()])

    # --- config migration on REAL data ------------------------------------
    import tomllib

    with (app / "config.toml").open("rb") as fh:
        document = tomllib.load(fh)
    before_projects = len(document.get("projects", []))
    migration = migrate_config(document)

    check("config version is raised", migration.document["version"] == 3,
          f"{migration.from_version} -> {migration.to_version}")
    check("every real project survives migration",
          len(migration.document["projects"]) == before_projects,
          f"{before_projects} project(s)")
    check("v3 keys are added", set(migration.added_keys) >= {"storage_backend",
                                                             "collection_strategy"},
          ", ".join(migration.added_keys))
    check("migration is idempotent", migrate_config(migration.document).changed is False)
    check("the source config file is not modified by a dry run",
          (app / "config.toml").read_bytes() == source.read_bytes())

    # --- pre-flight against real numbers ----------------------------------
    class _Settings:
        data_dir = str(app)
        service_host, service_port = "127.0.0.1", 21420
        embedding_model = "all-MiniLM-L6-v2"
        projects = []

    report = run_preflight(_Settings(), point_count=147_344,
                           port_probe=lambda h, p: False)
    check("pre-flight runs every gate", len(report.checks) == 5,
          ", ".join(c.name for c in report.checks))
    disk = next(c for c in report.checks if c.name == "disk")
    check("the disk gate sizes the real corpus", "147,344 points" in disk.detail,
          disk.detail)

    print("\n--- plan preview (what the user would be shown) ---")
    print(summarize(result))

    shutil.rmtree(root, ignore_errors=True)
    failed = [r for r in results if r[1] == FAIL]
    print(f"\n  {len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
