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
    # No exemptions. This list used to excuse "recorded install version",
    # because from a checkout that check reported whatever packaged build
    # happened to be on the developer's machine. The exemption was hiding a
    # real defect — the check never asked whether this was a packaged install
    # at all — so removing it is part of the fix, not a tightening of the test.
    checks = run_selfcheck(__version__, port=21599)

    assert failures(checks) == [], format_report(checks)


@pytest.mark.skipif(sys.platform != "win32", reason="registry is Windows-only")
def test_windows_declares_that_it_records_installed_versions():
    """The platform fact, asserted as a platform fact.

    The previous version of this test ran the check and demanded it not skip —
    which is only true on a machine where the packaged product happens to be
    installed. It passed for the author and failed on every CI runner, testing
    the developer's machine rather than the code. What is actually invariant on
    Windows is that a package database EXISTS.
    """
    from ragtools.platform import adapter

    assert adapter().records_installed_version is True


@pytest.mark.skipif(sys.platform != "win32", reason="registry is Windows-only")
def test_the_registry_is_really_read_and_never_raises():
    """Not a stub: it reaches the real key and answers with a version or None,
    on a machine with the product installed and on one without."""
    from ragtools.platform import adapter

    recorded = adapter().recorded_version()

    assert recorded is None or isinstance(recorded, str)


def test_a_packaged_install_the_system_does_not_record_is_a_failure(monkeypatch):
    """The case the old three-way collapse could not express.

    A packaged installation absent from the OS's package database means the
    installer did not register it — Add/Remove Programs, winget and upgrade
    detection all disagree with the files on disk. That is a finding, and it
    must not read the same as "this OS has no registry".
    """
    from ragtools import selfcheck

    class _Adapter:
        records_installed_version = True

        @staticmethod
        def recorded_version():
            return None

    monkeypatch.setattr(selfcheck, "_adapter", lambda: _Adapter())
    monkeypatch.setattr(selfcheck, "_is_packaged", lambda: True)
    result = selfcheck.check_recorded_version("3.0.2")

    assert not result.ok and not result.skipped
    assert "no installation" in result.detail


def test_a_platform_with_no_package_database_is_skipped(monkeypatch):
    from ragtools import selfcheck

    class _Adapter:
        records_installed_version = False

        @staticmethod
        def recorded_version():  # pragma: no cover — must not be consulted
            raise AssertionError("asked a platform that keeps no record")

    monkeypatch.setattr(selfcheck, "_adapter", lambda: _Adapter())
    monkeypatch.setattr(selfcheck, "_is_packaged", lambda: True)
    result = selfcheck.check_recorded_version("3.0.2")

    assert result.skipped and result.ok


def test_a_source_checkout_is_not_expected_in_the_package_database(monkeypatch):
    """A checkout has no reason to appear there, so its absence is not a finding.

    Every sibling check already asks this; this one did not, which is exactly
    why it reported a failure on any Windows machine that had never installed
    the packaged product.
    """
    from ragtools import selfcheck

    class _Adapter:
        records_installed_version = True

        @staticmethod
        def recorded_version():
            return None

    monkeypatch.setattr(selfcheck, "_adapter", lambda: _Adapter())
    monkeypatch.setattr(selfcheck, "_is_packaged", lambda: False)

    assert selfcheck.check_recorded_version("3.0.2").skipped
