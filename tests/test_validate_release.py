"""The release gate must not be able to lie about what it verified.

Source-suite green is explicitly not sufficient for this release, so the runner
that checks a built-and-installed artifact carries the same obligation: a row it
could not execute must never contribute to a pass.

The first version of `Matrix.validated` failed exactly that: it reported
VALIDATED with nine required rows outstanding, because it only excluded SKIP and
manual rows are not SKIP. That bug is the reason these tests exist.
"""

from __future__ import annotations

import pytest

from scripts.validate_release import (
    FAIL,
    MANUAL,
    PASS,
    Matrix,
    Result,
    Row,
    collect,
    run,
)

HEALTHY = {
    "health": {"status": "ready", "collection": "kb", "version": "3.0.0",
               "storage_reachable": True, "storage_backend": "managed"},
    "status": {"points_count": 100, "collections": [{"points": 60}, {"points": 40}],
               "storage": {"backend": "managed", "hnsw": True},
               "scale": {"level": "ok"}},
    "frameworks": {"frameworks": []},
    "autostart": {"method": "task-scheduler", "legacy": [], "problem": ""},
}


def _rows(*ids):
    from scripts.validate_release import ROWS
    return [r for r in ROWS if r.id in ids]


# --- the gate -------------------------------------------------------------


def test_outstanding_manual_rows_block_validation():
    """Nine unrun required rows must not read as VALIDATED."""
    matrix = run("windows", HEALTHY, _rows("V06", "V15"))
    assert [r.status for r in matrix.results].count(MANUAL) == 1
    assert matrix.validated is False
    assert "manual row(s) outstanding" in matrix.render()


def test_all_automated_rows_passing_with_no_manual_rows_validates():
    matrix = run("windows", HEALTHY, _rows("V06", "V07", "V09", "V11"))
    assert matrix.validated is True
    assert "VERDICT: VALIDATED" in matrix.render()


def test_a_failure_blocks_validation_and_is_named():
    broken = {**HEALTHY, "health": {**HEALTHY["health"], "status": "degraded"}}
    matrix = run("windows", broken, _rows("V06"))
    assert matrix.validated is False
    assert "row(s) failed" in matrix.render()


def test_a_check_that_raises_fails_rather_than_passing():
    """An exception in a check is not a pass. Swallowing it would turn a broken
    probe into a green row."""
    def _explode(_ctx):
        raise RuntimeError("probe crashed")

    matrix = run("windows", {}, [Row("VX", "explodes", _explode)])
    assert matrix.results[0].status == FAIL
    assert "probe crashed" in matrix.results[0].detail


def test_rows_are_filtered_by_platform():
    """`V02` (upgrade from the previous release) exists only on Windows, since
    that is the only platform with a previous release."""
    assert not [r for r in run("linux", HEALTHY, _rows("V02")).results]
    assert [r for r in run("windows", HEALTHY, _rows("V02")).results]


# --- the automated checks -------------------------------------------------


def test_storage_unreachable_fails_even_when_the_process_answers():
    """A service reporting green while the store is unreachable is the exact
    failure this row exists for."""
    ctx = {**HEALTHY, "health": {**HEALTHY["health"],
                                 "storage_reachable": False,
                                 "storage_error": "connection refused"}}
    matrix = run("windows", ctx, _rows("V07"))
    assert matrix.results[0].status == FAIL
    assert "connection refused" in matrix.results[0].detail


def test_a_surviving_legacy_registration_fails_the_upgrade_row():
    ctx = {**HEALTHY, "autostart": {"legacy": ["RAGTools Watchdog (task-scheduler)"],
                                    "problem": ""}}
    matrix = run("windows", ctx, _rows("V08"))
    assert matrix.results[0].status == FAIL
    assert "RAGTools Watchdog" in matrix.results[0].detail


def test_divergent_counts_fail_reconciliation():
    ctx = {**HEALTHY, "status": {**HEALTHY["status"],
                                 "points_count": 100,
                                 "collections": [{"points": 60}]}}
    matrix = run("windows", ctx, _rows("V09"))
    assert matrix.results[0].status == FAIL
    assert "100" in matrix.results[0].detail and "60" in matrix.results[0].detail


def test_a_scale_warning_on_a_server_engine_fails():
    """Repeating a local-mode limit on an HNSW engine trains the operator to
    ignore the warning entirely."""
    ctx = {**HEALTHY, "status": {**HEALTHY["status"], "scale": {"level": "over"}}}
    matrix = run("windows", ctx, _rows("V11"))
    assert matrix.results[0].status == FAIL


def test_an_unreachable_service_fails_rather_than_erroring(monkeypatch):
    """`collect` must turn a dead endpoint into a result, not a traceback."""
    def _fetch(_path):
        raise ConnectionError("refused")

    ctx = collect("http://127.0.0.1:1", fetch=_fetch)
    matrix = run("windows", ctx, _rows("V06", "V07"))
    assert all(r.status == FAIL for r in matrix.results)
