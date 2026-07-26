"""The platform seam: one contract, three implementations, zero machine mutation.

Every adapter takes injectable roots and an injectable command runner, so these
tests exercise the real logic — unit rendering, plist structure, schtasks
argument construction, legacy enumeration — against temp directories on any
host. Nothing here registers a scheduled task, writes to a Startup folder,
enables a systemd unit or loads a LaunchAgent.

The invariant worth the most is :func:`assert_single_registration`. "Exactly one
autostart per concern" is the property the product could never state about
itself, which is why the development machine accumulated four registrations for
two concerns: a `RAGTools Service` task, a `RAGTools Watchdog` task, and two
Startup-folder scripts, none of which any code could enumerate.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from ragtools.platform import (
    KIND_SERVICE,
    KIND_TRAY,
    AutostartSpec,
    DuplicateRegistration,
    PlatformUnsupported,
    Registration,
    assert_single_registration,
    current_platform,
    resolve_adapter,
)
from ragtools.platform.base import CommandResult
from ragtools.platform.darwin import DarwinAdapter
from ragtools.platform.linux import LinuxAdapter
from ragtools.platform.windows import WindowsAdapter, render_task_xml


class FakeRunner:
    """Records argv and replies from a table. No process is ever started."""

    def __init__(self, replies: dict | None = None, default: CommandResult | None = None):
        self.calls: list[list[str]] = []
        self._replies = replies or {}
        self._default = default or CommandResult(1, "", "not found")

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        for needle, reply in self._replies.items():
            if needle in " ".join(argv):
                return reply
        return self._default

    def saw(self, needle: str) -> bool:
        return any(needle in " ".join(c) for c in self.calls)


OK = CommandResult(0, "", "")


# --- resolution ----------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("win32", "windows"), ("cygwin", "windows"),
    ("linux", "linux"), ("linux2", "linux"),
    ("darwin", "darwin"),
])
def test_platform_names_normalise(raw, expected):
    assert current_platform(raw) == expected


def test_an_unknown_platform_is_refused_not_guessed():
    """A POSIX-shaped guess about how a machine starts services fails at
    reboot, on someone else's computer. Refuse at the seam instead."""
    with pytest.raises(PlatformUnsupported, match="Refusing to guess"):
        resolve_adapter("sunos5")


@pytest.mark.parametrize("name,cls", [
    ("win32", WindowsAdapter), ("linux", LinuxAdapter), ("darwin", DarwinAdapter),
])
def test_each_supported_platform_resolves_to_its_adapter(name, cls, tmp_path):
    assert isinstance(resolve_adapter(name, home=tmp_path, runner=FakeRunner()), cls)


def test_every_adapter_satisfies_the_same_contract(tmp_path):
    """A contract only one implementation satisfies is not a contract."""
    required = [
        "app_dir", "dev_dir", "spawn_detached", "pid_alive", "terminate",
        "supports_autostart", "install_autostart", "remove_autostart",
        "find_autostart", "has_desktop_session", "open_url", "open_path", "copy_text",
    ]
    for name in ("win32", "linux", "darwin"):
        impl = resolve_adapter(name, home=tmp_path, runner=FakeRunner())
        missing = [m for m in required if not callable(getattr(impl, m, None))]
        assert not missing, f"{name} adapter missing {missing}"


def test_data_roots_differ_per_platform_and_never_collide_with_dev(tmp_path):
    """A dev run sharing the installed data root is how a live index gets
    corrupted by a test."""
    for name in ("win32", "linux", "darwin"):
        impl = resolve_adapter(name, home=tmp_path, runner=FakeRunner())
        assert impl.app_dir() != impl.dev_dir()


# --- the single-registration invariant -----------------------------------


def test_exactly_one_registration_is_accepted():
    reg = Registration("svc", KIND_SERVICE, "task-scheduler", "rag.exe")
    assert assert_single_registration([reg], KIND_SERVICE) is reg


def test_no_registration_is_reported_not_silently_accepted():
    with pytest.raises(DuplicateRegistration, match="no service autostart"):
        assert_single_registration([], KIND_SERVICE)


def test_two_current_registrations_are_refused():
    regs = [Registration("a", KIND_SERVICE, "task-scheduler", "x"),
            Registration("b", KIND_SERVICE, "systemd-user", "y")]
    with pytest.raises(DuplicateRegistration, match="expected exactly one"):
        assert_single_registration(regs, KIND_SERVICE)


def test_a_surviving_legacy_registration_names_the_fix():
    """Legacy and duplicate-current are different failures needing different
    actions: one is upgrade work not done, the other is a bug here."""
    regs = [
        Registration("RAGTools\\Service", KIND_SERVICE, "task-scheduler", "rag.exe"),
        Registration("RAGTools.vbs", KIND_SERVICE, "startup-folder", "x", legacy=True),
    ]
    with pytest.raises(DuplicateRegistration, match="rag upgrade apply"):
        assert_single_registration(regs, KIND_SERVICE)


# --- Windows -------------------------------------------------------------


def _win(tmp_path, runner, **kwargs):
    startup = tmp_path / "Startup"
    startup.mkdir(parents=True, exist_ok=True)
    kwargs.setdefault("user", "TESTHOST\\tester")
    return WindowsAdapter(runner, home=tmp_path, local_app_data=tmp_path / "Local",
                          startup_dir=startup, **kwargs), startup


def _rendered(spec, user="TESTHOST\\tester", task_path=r"\RAGTools\Service"):
    return render_task_xml(spec, user=user, task_path=task_path)


def test_windows_registers_one_at_logon_task_without_a_vbs_shim(tmp_path, allow_platform_writes):
    """The `.vbs` shims existed to hide a console window — which is what
    CREATE_NO_WINDOW is for. Shipping an interpreted script to work around a
    process-creation flag is how the terminal-flash defect shipped."""
    runner = FakeRunner({"schtasks /create": OK})
    adapter, _ = _win(tmp_path, runner)

    reg = adapter.install_autostart(AutostartSpec(
        name="svc", kind=KIND_SERVICE, argv=[r"C:\P\rag.exe", "service", "run"]))

    assert reg.mechanism == "task-scheduler"
    assert runner.saw("/xml"), "registration goes through a task definition"
    assert not runner.saw(".vbs")
    assert not runner.saw("wscript")


def test_windows_names_the_user_in_the_logon_trigger():
    """The defect this closes, stated as a test.

    `schtasks /sc onlogon` emits a LogonTrigger with no <UserId> — "at logon of
    ANY user" — which the scheduler accepts only from an administrator. Measured
    on a standard account: /sc onlogon is refused with *Access is denied* while
    /sc once succeeds in the same namespace. Without this element the product
    cannot start itself for the very user it is installed for.
    """
    xml = _rendered(AutostartSpec("svc", KIND_SERVICE, ["rag.exe"]))

    trigger = xml.split("<Triggers>")[1].split("</Triggers>")[0]
    assert "<UserId>TESTHOST\\tester</UserId>" in trigger
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml, "must never elevate"


def test_windows_task_survives_the_conditions_a_service_actually_meets():
    """Windows' own defaults are wrong for a background service, and `/sc
    onlogon` set none of them: it would refuse to start on battery, be stopped
    on switching to battery, and be killed at the default 72-hour limit."""
    xml = _rendered(AutostartSpec("svc", KIND_SERVICE, ["rag.exe"]))

    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml, "PT0S = no limit"
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml


def test_windows_restart_policy_replaces_the_watchdog_process():
    """The 462-line watchdog restarted a service the scheduler restarts
    natively — but only if something asks it to, and nothing ever did."""
    xml = _rendered(AutostartSpec("svc", KIND_SERVICE, ["rag.exe"]))

    restart = xml.split("<RestartOnFailure>")[1].split("</RestartOnFailure>")[0]
    assert "<Interval>PT1M</Interval>" in restart
    assert "<Count>3</Count>" in restart


def test_windows_task_xml_is_written_as_utf16(tmp_path, allow_platform_writes):
    """Task Scheduler rejects a UTF-8 definition as "The task XML is malformed",
    which reads like a schema error and is not one."""
    seen: dict = {}

    def runner(argv):
        argv = list(argv)
        if "/xml" in argv:
            seen["raw"] = Path(argv[argv.index("/xml") + 1]).read_bytes()
        return OK

    adapter, _ = _win(tmp_path, runner)
    adapter.install_autostart(AutostartSpec("svc", KIND_SERVICE, ["rag.exe"]))

    assert seen["raw"][:2] in (b"\xff\xfe", b"\xfe\xff"), "UTF-16 BOM expected"
    assert "<LogonTrigger>" in seen["raw"].decode("utf-16")


def test_windows_task_definition_is_not_left_in_the_temp_directory(tmp_path, allow_platform_writes):
    """The definition names the account the task runs as. It is a temp file with
    a lifetime, not a document to leave lying around."""
    captured: dict = {}

    def runner(argv):
        argv = list(argv)
        if "/xml" in argv:
            captured["path"] = Path(argv[argv.index("/xml") + 1])
        return CommandResult(1, "", "Access denied")

    adapter, _ = _win(tmp_path, runner)
    with pytest.raises(RuntimeError):
        adapter.install_autostart(AutostartSpec("svc", KIND_SERVICE, ["rag.exe"]))

    assert not captured["path"].exists(), "removed even when registration fails"


def test_windows_xml_escapes_arguments():
    """A path or argument containing `&` produces a malformed document, and the
    scheduler's only reply is that the XML is malformed."""
    xml = _rendered(AutostartSpec(
        "svc", KIND_SERVICE, [r"C:\Program Files\R&D\rag.exe", "--flag", "a<b"],
        description="R&D build", working_dir=Path(r"C:\R&D")))

    assert "R&amp;D" in xml
    assert "a&lt;b" in xml
    assert "&D\\rag" not in xml, "a raw ampersand would break the document"


def test_windows_removal_prunes_the_empty_task_folder(tmp_path, allow_platform_writes):
    """Task Scheduler keeps the folder after its last task is deleted, so an
    uninstall that removes both registrations still leaves a `RAGTools` node in
    the scheduler tree."""
    runner = FakeRunner({"/tn \\RAGTools\\Service": OK, "schtasks /delete": OK})
    adapter, _ = _win(tmp_path, runner)

    adapter.remove_autostart(r"\RAGTools\Service")

    assert runner.saw("DeleteFolder('RAGTools', 0)")


def test_windows_removing_a_startup_file_does_not_touch_the_task_folder(tmp_path, allow_platform_writes):
    """Pruning is scoped to the mechanism that owns the folder."""
    runner = FakeRunner()
    adapter, startup = _win(tmp_path, runner)
    (startup / "RAGTools.vbs").write_text("legacy", encoding="utf-8")

    adapter.remove_autostart("RAGTools.vbs")

    assert not runner.saw("DeleteFolder")


def test_windows_enumerates_every_superseded_registration(tmp_path):
    """This is the whole reason upgrades kept adding mechanisms: nothing could
    see the ones already there."""
    runner = FakeRunner({
        "/tn \\RAGTools\\Service": OK,      # current
        "/tn RAGTools Watchdog": OK,        # legacy task
    })
    adapter, startup = _win(tmp_path, runner)
    (startup / "RAGTools.vbs").write_text("legacy", encoding="utf-8")

    found = adapter.find_autostart(KIND_SERVICE)
    names = {r.name for r in found}
    assert "\\RAGTools\\Service" in names
    assert "RAGTools Watchdog" in names
    assert "RAGTools.vbs" in names
    assert {r.name for r in found if r.legacy} == {"RAGTools Watchdog", "RAGTools.vbs"}


def test_windows_tray_legacy_is_separate_from_service_legacy(tmp_path):
    """Service and tray are different concerns with different lifetimes — a
    headless box has the first and not the second."""
    adapter, startup = _win(tmp_path, FakeRunner())
    (startup / "RAGTools-Tray.vbs").write_text("legacy", encoding="utf-8")

    assert {r.name for r in adapter.find_autostart(KIND_TRAY)} == {"RAGTools-Tray.vbs"}
    assert adapter.find_autostart(KIND_SERVICE) == []


def test_windows_removing_a_startup_file_is_idempotent(tmp_path, allow_platform_writes):
    """An interrupted upgrade re-runs; removing what is already gone must be
    success, not an error that strands the machine mid-upgrade."""
    adapter, startup = _win(tmp_path, FakeRunner())
    vbs = startup / "RAGTools.vbs"
    vbs.write_text("legacy", encoding="utf-8")

    assert [r.name for r in adapter.remove_autostart("RAGTools.vbs")] == ["RAGTools.vbs"]
    assert not vbs.exists()
    assert adapter.remove_autostart("RAGTools.vbs") == []      # second run: no-op


def test_windows_delay_is_encoded_as_the_task_schema_expects():
    """ISO-8601, because that is what the schema takes — `schtasks`' own
    `mmmm:ss` form does not appear in a task definition."""
    assert "<Delay>PT90S</Delay>" in _rendered(
        AutostartSpec("svc", KIND_SERVICE, ["rag.exe"], delay_seconds=90))
    assert "<Delay>PT2M</Delay>" in _rendered(
        AutostartSpec("svc", KIND_SERVICE, ["rag.exe"], delay_seconds=120))
    assert "<Delay>" not in _rendered(
        AutostartSpec("svc", KIND_SERVICE, ["rag.exe"])), "no delay, no element"


def test_windows_install_failure_raises_rather_than_reporting_success(tmp_path, allow_platform_writes):
    adapter, _ = _win(tmp_path, FakeRunner(default=CommandResult(1, "", "Access denied")))
    with pytest.raises(RuntimeError, match="Access denied"):
        adapter.install_autostart(AutostartSpec("svc", KIND_SERVICE, ["rag.exe"]))


# --- Linux ---------------------------------------------------------------


def _linux(tmp_path, runner):
    return LinuxAdapter(runner, home=tmp_path,
                        xdg_data_home=tmp_path / "data",
                        xdg_config_home=tmp_path / "config")


def test_linux_unit_restarts_on_failure_without_a_bespoke_watchdog(tmp_path):
    """Restart-on-failure is an init-system capability. The 462-line Windows
    watchdog reimplemented it with a polling task; no platform needs that."""
    adapter = _linux(tmp_path, FakeRunner({"systemctl": OK}))
    unit = adapter.render_unit(AutostartSpec(
        name="svc", kind=KIND_SERVICE, argv=["/usr/bin/rag", "service", "run"],
        description="RAG Tools"))

    assert "Restart=on-failure" in unit
    assert "RestartSec=5" in unit
    assert "StartLimitBurst=5" in unit, "a crash loop must eventually surface as failed"
    assert "ExecStart=/usr/bin/rag service run" in unit
    assert "WantedBy=default.target" in unit


def test_linux_start_limits_live_in_the_unit_section(tmp_path):
    """`StartLimitIntervalSec` in `[Service]` is silently IGNORED by systemd.

    Found by running `systemd-analyze verify` on real systemd: "Unknown key
    name 'StartLimitIntervalSec' in section 'Service', ignoring". The unit
    parsed, looked correct, and had no crash-loop protection at all — it would
    have restarted a failing service forever.
    """
    unit = _linux(tmp_path, FakeRunner()).render_unit(
        AutostartSpec("svc", KIND_SERVICE, ["/usr/bin/rag"]))

    unit_section = unit.split("[Service]")[0]
    service_section = unit.split("[Service]")[1]
    assert "StartLimitIntervalSec=300" in unit_section
    assert "StartLimitBurst=5" in unit_section
    assert "StartLimit" not in service_section


@pytest.mark.parametrize("state,alive", [
    ("R", True), ("S", True), ("D", True),   # running / sleeping / uninterruptible
    ("Z", False),                             # exited, not yet reaped
])
def test_linux_reads_process_state_from_proc(tmp_path, monkeypatch, state, alive):
    """An exited-but-unreaped process still answers `os.kill(pid, 0)`, so the
    obvious check reports a dead service as running and the stale PID file is
    never cleaned — the same class the Windows adapter guards with
    GetExitCodeProcess.

    Found on real Linux: `terminate` succeeded and `pid_alive` kept saying yes.
    """
    from ragtools.platform import linux as linux_mod

    # `comm` may contain spaces and parentheses, so the state is the field
    # after the LAST ')' — a naive split() on the whole line gets this wrong.
    line = f"4242 (rag service run) {state} 1 4242 0 0"

    real_read = linux_mod.Path.read_text

    def _read(self, *args, **kwargs):
        if str(self).replace("\\", "/").endswith("/proc/4242/stat"):
            return line
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(linux_mod.Path, "read_text", _read)
    assert _linux(tmp_path, FakeRunner()).pid_alive(4242) is alive


def test_linux_falls_back_when_proc_is_unavailable(tmp_path, monkeypatch):
    """A container without /proc must still get an answer rather than a crash."""
    from ragtools.platform import linux as linux_mod

    def _no_proc(self, *args, **kwargs):
        raise OSError("no /proc")

    monkeypatch.setattr(linux_mod.Path, "read_text", _no_proc)
    monkeypatch.setattr(linux_mod.os, "kill", lambda pid, sig: None)
    assert _linux(tmp_path, FakeRunner()).pid_alive(4242) is True


def test_linux_installs_a_user_unit_not_a_system_unit(tmp_path, allow_platform_writes):
    """A system unit runs as another account and cannot see $HOME — where this
    product's data and the user's projects both live."""
    runner = FakeRunner({"systemctl": OK})
    adapter = _linux(tmp_path, runner)
    reg = adapter.install_autostart(AutostartSpec("svc", KIND_SERVICE, ["/usr/bin/rag"]))

    assert reg.path == tmp_path / "config" / "systemd" / "user" / "ragtools.service"
    assert reg.path.exists()
    assert runner.saw("--user enable")
    assert not runner.saw("systemctl enable ragtools")   # i.e. never system-scope


def test_linux_reloads_before_enabling(tmp_path, allow_platform_writes):
    """Enable before daemon-reload enables the PREVIOUS unit content, so an
    upgrade keeps running the old command until the next reload."""
    runner = FakeRunner({"systemctl": OK})
    _linux(tmp_path, runner).install_autostart(
        AutostartSpec("svc", KIND_SERVICE, ["/usr/bin/rag"]))

    order = [" ".join(c) for c in runner.calls]
    assert any("daemon-reload" in c for c in order)
    assert next(i for i, c in enumerate(order) if "daemon-reload" in c) < \
           next(i for i, c in enumerate(order) if "enable" in c)


def test_linux_reports_lingering_because_headless_service_death_is_silent(tmp_path):
    """Without linger the user manager — and the service — stop at logout. On
    an SSH-only box that reads as "indexing randomly broke"."""
    on = _linux(tmp_path, FakeRunner({"show-user": CommandResult(0, "Linger=yes", "")}))
    off = _linux(tmp_path, FakeRunner({"show-user": CommandResult(0, "Linger=no", "")}))
    assert on.linger_enabled("ahmed") is True
    assert off.linger_enabled("ahmed") is False


def test_linux_desktop_session_detected_for_x11_and_wayland(tmp_path, monkeypatch):
    adapter = _linux(tmp_path, FakeRunner())
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert adapter.has_desktop_session() is False, "headless must not claim a tray"
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert adapter.has_desktop_session() is True


def test_linux_unit_carries_environment_and_working_dir(tmp_path):
    adapter = _linux(tmp_path, FakeRunner({"systemctl": OK}))
    unit = adapter.render_unit(AutostartSpec(
        "svc", KIND_SERVICE, ["/usr/bin/rag"],
        environment={"RAG_PROFILE": "installed"}, working_dir=Path("/opt/ragtools")))
    assert 'Environment="RAG_PROFILE=installed"' in unit
    assert "WorkingDirectory=/opt/ragtools" in unit


# --- macOS ---------------------------------------------------------------


def test_darwin_agent_restarts_on_crash_but_respects_a_clean_exit(tmp_path):
    """KeepAlive=true would fight `rag service stop` forever."""
    adapter = DarwinAdapter(FakeRunner({"launchctl": OK}), home=tmp_path)
    plist = adapter.render_plist(AutostartSpec("svc", KIND_SERVICE, ["/usr/local/bin/rag"]))

    assert plist["Label"] == "com.ragtools.service"
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ProgramArguments"] == ["/usr/local/bin/rag"]


def test_darwin_writes_a_launch_agent_and_boots_out_the_old_definition(tmp_path, allow_platform_writes):
    """launchd keeps the previously loaded definition until bootout, so an
    upgrade without it keeps running the old binary until next login."""
    runner = FakeRunner({"launchctl": OK})
    adapter = DarwinAdapter(runner, home=tmp_path)
    reg = adapter.install_autostart(AutostartSpec("svc", KIND_SERVICE, ["/usr/local/bin/rag"]))

    assert reg.path == tmp_path / "Library" / "LaunchAgents" / "com.ragtools.service.plist"
    assert reg.path.exists()
    assert runner.saw("bootout")
    order = [" ".join(c) for c in runner.calls]
    assert next(i for i, c in enumerate(order) if "bootout" in c) < \
           next(i for i, c in enumerate(order) if "bootstrap" in c)


def test_darwin_plist_is_valid_and_round_trips(tmp_path, allow_platform_writes):
    runner = FakeRunner({"launchctl": OK})
    adapter = DarwinAdapter(runner, home=tmp_path)
    adapter.install_autostart(AutostartSpec("svc", KIND_SERVICE, ["/usr/local/bin/rag", "run"]))

    path = tmp_path / "Library" / "LaunchAgents" / "com.ragtools.service.plist"
    with path.open("rb") as handle:
        data = plistlib.load(handle)
    assert data["ProgramArguments"] == ["/usr/local/bin/rag", "run"]


def test_darwin_falls_back_to_legacy_load_on_older_macos(tmp_path, allow_platform_writes):
    """`bootstrap` is unavailable before 10.11-era launchctl; failing there
    would make the product uninstallable on those machines."""
    runner = FakeRunner({"launchctl load": OK})     # bootstrap fails, load works
    adapter = DarwinAdapter(runner, home=tmp_path)
    adapter.install_autostart(AutostartSpec("svc", KIND_SERVICE, ["/usr/local/bin/rag"]))
    assert runner.saw("load -w")


def test_darwin_service_and_tray_use_distinct_labels(tmp_path):
    adapter = DarwinAdapter(FakeRunner({"launchctl": OK}), home=tmp_path)
    svc = adapter.render_plist(AutostartSpec("s", KIND_SERVICE, ["rag"]))
    tray = adapter.render_plist(AutostartSpec("t", KIND_TRAY, ["rag", "tray"]))
    assert svc["Label"] != tray["Label"]


# --- the structural rule -------------------------------------------------


def test_no_platform_branch_survives_outside_this_package():
    """The seam is only real if it cannot leak back. Thirteen modules branched
    on sys.platform before this package existed; three had no non-Windows path
    at all, which is a Windows product rather than a portable one.
    """
    import re

    root = Path(__file__).resolve().parents[1] / "src" / "ragtools"
    pattern = re.compile(r"sys\.platform|platform\.system\(\)")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("platform/"):
            continue                       # the seam itself
        if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
            offenders.append(rel)

    assert not offenders, (
        "platform dispatch outside ragtools.platform: " + ", ".join(sorted(offenders))
    )


# --- macOS: the bug class real systemd caught, prevented without a Mac -----


def test_darwin_plist_only_uses_keys_launchd_understands(tmp_path):
    """launchd IGNORES an unrecognised key silently — the same way systemd
    ignored `StartLimitIntervalSec` in the wrong section, discarding the
    crash-loop protection without a word. No Mac is available to run `plutil`
    here, so validating the key set is what stops that class shipping."""
    from ragtools.platform.darwin import LAUNCHD_KEYS, validate_plist

    adapter = DarwinAdapter(FakeRunner({"launchctl": OK}), home=tmp_path)
    document = adapter.render_plist(AutostartSpec(
        "svc", KIND_SERVICE, ["/usr/local/bin/rag", "service", "run"],
        environment={"RAG_PROFILE": "installed"}, working_dir=Path("/opt/ragtools"),
        delay_seconds=10))

    assert set(document) <= LAUNCHD_KEYS, sorted(set(document) - LAUNCHD_KEYS)
    validate_plist(document)


def test_darwin_refuses_an_unknown_key():
    from ragtools.platform.darwin import InvalidAgent, validate_plist

    with pytest.raises(InvalidAgent, match="does not recognise"):
        validate_plist({"Label": "x", "ProgramArguments": ["y"], "RestartSec": 5})


def test_darwin_refuses_a_misspelt_keepalive_condition():
    """`SuccessfulExist` loads fine and silently never restarts on crash."""
    from ragtools.platform.darwin import InvalidAgent, validate_plist

    with pytest.raises(InvalidAgent, match="KeepAlive does not accept"):
        validate_plist({"Label": "x", "ProgramArguments": ["y"],
                        "KeepAlive": {"SuccessfulExist": False}})


def test_darwin_refuses_a_plist_with_nothing_to_run():
    from ragtools.platform.darwin import InvalidAgent, validate_plist

    with pytest.raises(InvalidAgent, match="ProgramArguments or Program"):
        validate_plist({"Label": "com.ragtools.service"})


def test_darwin_refuses_a_label_less_plist():
    from ragtools.platform.darwin import InvalidAgent, validate_plist

    with pytest.raises(InvalidAgent, match="Label is required"):
        validate_plist({"ProgramArguments": ["/usr/local/bin/rag"]})


def test_windows_ignores_spec_name_for_the_registration_target(tmp_path, allow_platform_writes):
    r"""`spec.name` is a DISPLAY name; the target comes from `kind`.

    Found by a verification probe that passed a sandbox name, was silently given
    the real `\RAGTools\Service` path, and would have overwritten the user's
    actual registration had the account been allowed to create tasks. Isolation
    has to come from adapter configuration, not from a field the adapter drops.
    """
    runner = FakeRunner({"schtasks /create": OK})
    adapter, _ = _win(tmp_path, runner)

    adapter.install_autostart(AutostartSpec(
        name="something-else-entirely", kind=KIND_SERVICE, argv=["rag.exe"]))

    assert runner.saw(r"\RAGTools\Service")
    assert not runner.saw("something-else-entirely")


def test_windows_task_prefix_is_injectable_for_isolation(tmp_path, allow_platform_writes):
    """The only mechanism that genuinely prevents a probe touching production."""
    runner = FakeRunner({"schtasks /create": OK})
    startup = tmp_path / "Startup"
    startup.mkdir(parents=True, exist_ok=True)
    adapter = WindowsAdapter(runner, home=tmp_path, local_app_data=tmp_path / "L",
                             startup_dir=startup, task_prefix=r"\RAGToolsVerify")

    adapter.install_autostart(AutostartSpec("probe", KIND_SERVICE, ["cmd.exe"]))

    assert runner.saw(r"\RAGToolsVerify\Service")
    assert not runner.saw(r"\RAGTools\Service"), "a probe reached the real registration"
