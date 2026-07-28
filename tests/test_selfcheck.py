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
        # Installation: is this machine running this release at all?
        "installed version",
        "windowed executable",
        "recorded install version",
        "running processes",
        "autostart targets",
        "service health version",
        # Product: an installation can be byte-perfect and still BE the previous
        # product. v3.0.0 and v3.0.1 both shipped a migration nothing invoked,
        # so every upgraded machine kept a v2 configuration and ran on v2
        # defaults — and nothing reported it, which is how it survived two
        # releases.
        "config schema",
        "migration state",
        "storage contract",
        "index identity",
        "reindex state",
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


# --- the product checks: an installation can be the PREVIOUS product -------


def test_a_v2_configuration_is_reported_even_on_a_perfect_installation(tmp_path, monkeypatch):
    """The defect that survived two releases, stated as a check.

    v3.0.0 and v3.0.1 both shipped a migration nothing invoked, so every
    upgraded machine had byte-perfect files and a v2 configuration — running on
    v2 code defaults with the v3 architecture unreachable. Every installation
    check passed. Nothing looked at the configuration.
    """
    import ragtools.config as cfg
    from ragtools.selfcheck import check_config_schema

    config = tmp_path / "config.toml"
    config.write_text("version = 2\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "_find_config_path", lambda: config)
    monkeypatch.setenv("RAG_CONFIG_PATH", str(config))

    result = check_config_schema()

    assert not result.ok and not result.skipped
    assert "rag upgrade" in result.detail, "the finding does not name the remedy"


def test_no_configuration_file_is_not_a_stale_configuration(tmp_path, monkeypatch):
    """"Never written one" and "wrote an old one" are different states.

    Collapsing them reports a stale config on every machine that has never
    written one — a source checkout, a fresh container, a CI runner — which is
    the same three-state mistake `recorded_version` made, in a new place.
    """
    import ragtools.config as cfg
    from ragtools.selfcheck import check_config_schema

    monkeypatch.setattr(cfg, "_find_config_path", lambda: None)

    assert check_config_schema().skipped


def test_a_current_configuration_passes(tmp_path, monkeypatch):
    import ragtools.config as cfg
    from ragtools.config import CONFIG_VERSION
    from ragtools.selfcheck import check_config_schema

    config = tmp_path / "config.toml"
    config.write_text(f"version = {CONFIG_VERSION}\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "_find_config_path", lambda: config)
    monkeypatch.setenv("RAG_CONFIG_PATH", str(config))

    result = check_config_schema()

    assert result.ok and not result.skipped


def test_an_unsupported_storage_configuration_is_a_finding(tmp_path, monkeypatch):
    """`Settings()` refuses it at load, so the check must report that refusal
    rather than crash on it."""
    from ragtools.selfcheck import check_storage_contract

    config = tmp_path / "config.toml"
    config.write_text('version = 3\nstorage_backend = "nonsense"\n', encoding="utf-8")
    monkeypatch.setenv("RAG_CONFIG_PATH", str(config))

    result = check_storage_contract()

    assert not result.ok and not result.skipped
    assert "refused" in result.detail


def test_a_stalled_migration_is_a_failure_not_a_status_line(tmp_path, monkeypatch):
    """Gate 5. An unmigrated legacy config reaching steady state must FAIL.

    The machine is then running a v3 configuration over an index that was never
    rebuilt for it: some projects present, some absent, and every query against
    a missing one answering "no matches" in the ordinary, reassuring shape. Left
    as an informational note this reads as progress and gets ignored.
    """
    from ragtools.config import Settings
    from ragtools.selfcheck import check_reindex_state
    from ragtools.upgrade import relayout
    from ragtools.upgrade.relayout import Inventory, Unit

    monkeypatch.setenv("RAG_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("RAG_QDRANT_PATH", str(tmp_path / "qdrant"))
    settings = Settings()

    relayout.begin(settings,
                   Inventory(units=[Unit(relayout.KIND_PROJECT, "alpha", 500),
                                    Unit(relayout.KIND_PROJECT, "beta", 300)]),
                   from_backend="embedded", to_backend="managed",
                   from_strategy="shared", to_strategy="per_project")

    result = check_reindex_state()

    assert not result.ok and not result.skipped, result
    assert "rag upgrade --resume" in result.detail, (
        "the failure does not name the supported retry path"
    )


def test_a_failed_unit_is_named_in_the_finding(tmp_path, monkeypatch):
    """"Something failed" is not actionable; the project and the error are."""
    from ragtools.config import Settings
    from ragtools.selfcheck import check_reindex_state
    from ragtools.upgrade import relayout
    from ragtools.upgrade.relayout import STATUS_FAILED, Inventory, Unit

    monkeypatch.setenv("RAG_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("RAG_QDRANT_PATH", str(tmp_path / "qdrant"))
    settings = Settings()

    plan = relayout.begin(
        settings, Inventory(units=[Unit(relayout.KIND_PROJECT, "beta", 300)]),
        from_backend="embedded", to_backend="managed",
        from_strategy="shared", to_strategy="per_project")
    relayout.mark(settings, plan, Unit(relayout.KIND_PROJECT, "beta"),
                  STATUS_FAILED, error="permission denied")

    result = check_reindex_state()

    assert not result.ok
    assert "beta" in result.detail and "permission denied" in result.detail


def test_a_completed_migration_passes(tmp_path, monkeypatch):
    from ragtools.config import Settings
    from ragtools.selfcheck import check_reindex_state
    from ragtools.upgrade import relayout
    from ragtools.upgrade.relayout import STATUS_DONE, Inventory, Unit

    monkeypatch.setenv("RAG_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("RAG_QDRANT_PATH", str(tmp_path / "qdrant"))
    settings = Settings()

    plan = relayout.begin(
        settings, Inventory(units=[Unit(relayout.KIND_PROJECT, "alpha", 1)]),
        from_backend="embedded", to_backend="managed",
        from_strategy="shared", to_strategy="per_project")
    relayout.mark(settings, plan, Unit(relayout.KIND_PROJECT, "alpha"),
                  STATUS_DONE, points_after=1)
    relayout.finalize(settings, plan)

    assert check_reindex_state().ok
