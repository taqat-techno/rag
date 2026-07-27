"""Verifying that the machine moved, not that files were copied.

An installer reports success when its copy step returned without error. That is
a much weaker claim than "this machine now runs the new version", and the gap
between them is where upgrade defects live: a process from the old build still
holding its own binaries, a scheduled task still naming the old directory, an
uninstall entry still reading the old version. None of those raises an error
anywhere.

`rag selfcheck` closes the gap by asking the machine afterwards. These tests
pin the two properties that make it worth running at all: it must FAIL on a
half-upgraded machine, and it must not fail merely because something could not
be inspected.
"""

from __future__ import annotations

import sys

import pytest

from ragtools import __version__
from ragtools.selfcheck import (
    Check,
    check_own_version,
    check_service_health,
    check_windowed_executable,
    failures,
    format_report,
    run_selfcheck,
)


# --- the distinction the whole module rests on ---------------------------


def test_a_skipped_check_is_not_a_passed_check():
    """"Could not inspect" and "inspected, correct" must never merge.

    If they do, a verifier on a machine where nothing is inspectable reports a
    clean bill of health — which is precisely the false success it exists to
    prevent.
    """
    skipped = Check("x", ok=True, skipped=True)
    passed = Check("y", ok=True)

    assert skipped.status == "SKIP"
    assert passed.status == "PASS"
    assert failures([skipped, passed]) == []
    assert "1 passed, 0 failed, 1 skipped" in format_report([skipped, passed])


def test_a_failed_check_is_reported_as_a_failure():
    broken = Check("z", ok=False, detail="stale")
    assert broken.status == "FAIL"
    assert failures([broken]) == [broken]


# --- the check that catches a half-upgrade -------------------------------


def test_the_version_check_fails_when_the_machine_is_on_the_old_release():
    """The core assertion. A machine still running the previous version must
    not be reported as upgraded."""
    assert check_own_version(__version__).ok
    stale = check_own_version("9.9.9")

    assert not stale.ok
    assert __version__ in stale.detail and "9.9.9" in stale.detail


def test_the_windowed_executable_check_is_skipped_when_not_required():
    """Non-Windows releases ship no `ragw.exe`, and its absence there is
    correct rather than a finding."""
    result = check_windowed_executable()

    assert result.skipped and result.ok


def test_a_source_checkout_does_not_fail_the_windowed_check(monkeypatch):
    """`ragw.exe` exists only in a packaged bundle. Running `rag selfcheck`
    from a checkout must report "not applicable", not "broken install"."""
    monkeypatch.setattr("ragtools.selfcheck._is_packaged", lambda: False)

    assert check_windowed_executable().skipped


# --- health: silence is not a verdict ------------------------------------


def test_no_service_listening_is_skipped_not_failed(monkeypatch):
    """Post-install runs before the service has necessarily started. "Not
    running yet" is not "wrong version"."""
    import httpx

    def _refuse(*a, **k):
        raise httpx.ConnectError("nothing listening")

    monkeypatch.setattr(httpx, "get", _refuse)
    result = check_service_health("3.0.2", port=21599)

    assert result.skipped and result.ok


def test_a_service_answering_with_the_old_version_fails(monkeypatch):
    """The most direct proof a machine did not move: the thing the user
    actually talks to still reports the previous release."""
    import httpx

    class _Response:
        @staticmethod
        def json():
            return {"version": "2.7.0", "status": "ready"}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response())
    result = check_service_health("3.0.2", port=21599)

    assert not result.ok and not result.skipped
    assert "2.7.0" in result.detail


# --- the whole run -------------------------------------------------------


def test_a_full_run_covers_every_documented_check():
    """The installer's verdict comes from this list; a check quietly dropped
    from it is a check that stops running in the field."""
    names = [c.name for c in run_selfcheck(__version__, port=21599)]

    assert names == [
        "installed version",
        "windowed executable",
        "recorded install version",
        "running processes",
        "autostart targets",
        "service health version",
    ]


def test_a_matching_installation_reports_no_failures_from_source():
    """Running from a checkout, everything machine-specific is skipped and the
    version matches — so the verifier must be quiet. A verifier that cries wolf
    in development is a verifier people learn to pass `|| true`."""
    checks = run_selfcheck(__version__, port=21599)
    broken = [c for c in failures(checks)
              # The uninstall registry legitimately describes whatever packaged
              # build is installed on the developer's machine, which is not this
              # source tree. Only that check may disagree here.
              if c.name != "recorded install version"]

    assert broken == [], format_report(checks)


@pytest.mark.skipif(sys.platform != "win32", reason="registry is Windows-only")
def test_the_registry_check_actually_reads_the_registry():
    """Not a stub. On Windows it must reach the real uninstall key and either
    report a version or say plainly that no entry exists."""
    from ragtools.selfcheck import check_recorded_version

    result = check_recorded_version("0.0.0-not-a-real-version")

    assert not result.skipped, "the registry check silently skipped on Windows"
    assert not result.ok
    assert "the system records" in result.detail
