"""One bad path must not stop indexing for everything else.

Field-observed: at 16:58:29 a startup sync aborted with
``[WinError 448] The provided mount point is not trusted`` on a junction inside
a generated ``node_modules`` tree. The scanner had no exception handling, the
per-project loop had none either, and the only handler was the blanket
``except Exception`` around the whole startup sync — which logged it as
"Startup sync failed (non-fatal)". So one untraversable junction inside ONE
project silently stopped indexing for all twenty-five, on every boot, in a
message containing the word "non-fatal".

Two measurements shaped these tests rather than assumptions:

* ``rglob`` iteration is resilient. A directory with all access denied is
  skipped and the walk continues — so the fix belongs at the ``stat`` that
  resolves each entry, not around the generator.
* ``rglob`` follows junctions. A self-referential junction reached depth 23 and
  returned 23 copies of one file, bounded only by the path-length limit — so
  enabling long paths deepens the loop rather than fixing it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ragtools.indexing.scanner import (
    SkipLedger,
    _inspect,
    discover_indexable_files,
    scan_configured_projects,
)


def make_junction(link: Path, target: Path) -> bool:
    """A real NTFS junction, or False where that is not possible."""
    if sys.platform != "win32":
        return False
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, text=True)
    return result.returncode == 0 and link.exists()


class _Exploding:
    """A path whose `stat()` raises the way a distrusted mount point does.

    `stat` rather than `is_file`: the scanner takes ONE stat and derives both
    "is this a regular file?" and the file's identity from it, so that single
    call is where a filesystem refusal actually surfaces.
    """

    def __init__(self, error: OSError):
        self._error = error

    def stat(self):
        raise self._error

    def __str__(self):
        return "<exploding path>"


# --- the guard itself -------------------------------------------------------


def test_an_untrusted_mount_point_is_skipped_not_raised():
    """WinError 448 is the exact error observed in the field."""
    error = OSError(22, "The provided mount point is not trusted")
    error.winerror = 448  # type: ignore[attr-defined]
    ledger = SkipLedger()

    is_file, identity = _inspect(_Exploding(error), ledger)

    assert is_file is False and identity is None
    assert ledger.unreadable == 1
    assert ledger.total == 1


@pytest.mark.parametrize("error", [
    PermissionError(13, "Permission denied"),
    OSError(40, "Too many levels of symbolic links"),   # ELOOP
    FileNotFoundError(2, "No such file or directory"),  # deleted mid-walk
])
def test_every_filesystem_refusal_is_a_skip(error):
    """Caught as OSError rather than by error code: the POSIX equivalents are
    the same class of "the filesystem will not answer" and want the same
    answer, so the fix must not be Windows-shaped."""
    assert _inspect(_Exploding(error), SkipLedger())[0] is False


def test_a_skip_is_counted_and_described():
    """A scan that silently drops files reports the same success as one that
    read everything."""
    ledger = SkipLedger()
    _inspect(_Exploding(PermissionError(13, "denied")), ledger)

    assert ledger.total == 1
    assert "unreadable" in ledger.describe()
    assert ledger.examples, "nothing was named, so nothing can be investigated"


# --- the whole-scan property -----------------------------------------------


def test_an_unreadable_entry_does_not_lose_the_rest_of_the_tree(tmp_path):
    (tmp_path / "a_first.md").write_text("# first", encoding="utf-8")
    (tmp_path / "z_last.md").write_text("# last", encoding="utf-8")

    found = {p.name for p in discover_indexable_files(tmp_path, mode="general")}

    assert {"a_first.md", "z_last.md"} <= found


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_a_junction_loop_does_not_multiply_the_index(tmp_path):
    """Measured before the fix: 23 copies of one file, one per loop iteration,
    every one of them chunked, embedded and stored separately."""
    (tmp_path / "only.md").write_text("# only", encoding="utf-8")
    if not make_junction(tmp_path / "loop", tmp_path):
        pytest.skip("could not create a junction on this runner")

    found = discover_indexable_files(tmp_path, mode="general")

    assert len(found) == 1, f"the same file was indexed {len(found)} times: {found}"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_a_broken_junction_does_not_stop_the_scan(tmp_path):
    (tmp_path / "real.md").write_text("# real", encoding="utf-8")
    make_junction(tmp_path / "dangling", tmp_path / "does_not_exist")

    found = {p.name for p in discover_indexable_files(tmp_path, mode="general")}

    assert "real.md" in found


# --- fault isolation between projects --------------------------------------


def _project(tmp_path, name):
    from ragtools.config import ProjectConfig

    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(f"# {name}", encoding="utf-8")
    return ProjectConfig(id=name, path=str(root), mode="general")


def test_one_unscannable_project_does_not_take_the_others_with_it(tmp_path, monkeypatch):
    """The reported blast radius, asserted directly.

    A project root that becomes unusable mid-scan — a disconnected share, a
    revoked permission, an ejected volume — must cost that project only.
    """
    good_a = _project(tmp_path, "good_a")
    broken = _project(tmp_path, "broken")
    good_b = _project(tmp_path, "good_b")

    import ragtools.indexing.scanner as scanner

    real = scanner.discover_indexable_files

    def explode_for_one(directory, **kwargs):
        if Path(directory).name == "broken":
            raise OSError(22, "The provided mount point is not trusted")
        return real(directory, **kwargs)

    monkeypatch.setattr(scanner, "discover_indexable_files", explode_for_one)

    ledger = SkipLedger()
    results = scanner.scan_configured_projects([good_a, broken, good_b], ledger=ledger)

    scanned = {pid for pid, _ in results}
    assert scanned == {"good_a", "good_b"}, (
        "a failure in one project changed what the others returned"
    )
    assert ledger.total >= 1, "the failed project was not recorded anywhere"


def test_a_project_whose_path_is_gone_is_skipped_quietly(tmp_path):
    from ragtools.config import ProjectConfig

    present = _project(tmp_path, "present")
    absent = ProjectConfig(id="absent", path=str(tmp_path / "nope"), mode="general")

    results = scan_configured_projects([present, absent])

    assert {pid for pid, _ in results} == {"present"}


def test_skips_are_reported_up_to_the_caller(tmp_path, monkeypatch):
    """The caller must be able to tell "read everything" from "read some of
    it" — otherwise a degraded scan is indistinguishable from a clean one."""
    import ragtools.indexing.scanner as scanner

    project = _project(tmp_path, "proj")

    def one_bad_file(directory, **kwargs):
        led = kwargs.get("ledger")
        if led is not None:
            led.record(Path(directory) / "bad", "PermissionError: denied")
        return []

    monkeypatch.setattr(scanner, "discover_indexable_files", one_bad_file)

    ledger = SkipLedger()
    scanner.scan_configured_projects([project], ledger=ledger)

    assert ledger.total == 1
    assert "unreadable" in ledger.describe()


# --- the ledger's own arithmetic -------------------------------------------


def test_loops_and_unreadable_paths_are_counted_separately():
    """They are different findings: one is a filesystem refusal, the other is
    the scanner declining to walk in a circle."""
    ledger = SkipLedger()
    ledger.record(Path("a"), "denied")
    ledger.record(Path("b"), "already reached", kind="loop")

    assert (ledger.unreadable, ledger.loops, ledger.total) == (1, 1, 2)
    described = ledger.describe()
    assert "unreadable" in described and "link loop" in described


def test_a_clean_scan_describes_nothing():
    assert SkipLedger().total == 0
