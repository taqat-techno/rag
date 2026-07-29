"""`rag selfcheck` said "no autostart registered" while two tasks were registered.

Measured on the affected machine, against the exact command `find_autostart`
issued (`schtasks /query /tn "\\RAGTools\\Service"`, no format flags)::

    Folder: \\RAGTools
    TaskName                                 Next Run Time          Status
    ======================================== ====================== ===============
    Service                                  N/A                    Ready

Zero lines contain `.exe`, and the TaskName column carries the LEAF name with no
backslash — so `_task_target`'s predicate (`"\\" in line and ".exe" in line`)
failed on both halves and returned `""`. `check_autostart_targets` then skipped
that registration WITHOUT counting it, found nothing, and reported "no autostart
registered" as a SKIP.

The consequence is worse than the wrong message. A task pointing at the PREVIOUS
install directory produced the same empty string and the same reassuring skip —
so the one check whose job is to catch "this machine reverts to the old build at
the next logon" could not fail. It had never verified a target on any real
Windows machine.

Two things were wrong and both are fixed here: the query never asked for the
command, and the parser could not have read it. The fixtures below are REAL
`schtasks` output captured from an installed machine, because the previous unit
tests fed the adapter `CommandResult(0, "", "")` — an empty stdout, which is
exactly the input that produces the bug.
"""

from __future__ import annotations

from ragtools.platform.base import CommandResult, KIND_SERVICE, KIND_TRAY
from ragtools.platform.windows import WindowsAdapter, _task_target

#: Real output of `schtasks /query /tn "\RAGTools\Service" /fo LIST /v`.
VERBOSE = """
Folder: \\RAGTools
HostName:                             LAKOSHA-HOME
TaskName:                             \\RAGTools\\Service
Next Run Time:                        N/A
Status:                               Ready
Logon Mode:                           Interactive only
Last Run Time:                        11/30/1999 12:00:00 AM
Last Result:                          267011
Author:                               N/A
Task To Run:                          C:\\Users\\ahmed\\AppData\\Local\\Programs\\RAGTools\\ragw.exe service run --host 127.0.0.1 --port 21420 --profile installed
Start In:                             N/A
Comment:                              RAG Tools - local knowledge-base service
Scheduled Task State:                 Enabled
"""

#: Real output of the SAME task with no format flags — what v3.1.0 parsed.
SUMMARY = """
Folder: \\RAGTools
TaskName                                 Next Run Time          Status
======================================== ====================== ===============
Service                                  N/A                    Ready
"""


class Runner:
    """Replies from a table and records what was actually asked."""

    def __init__(self, replies):
        self.calls = []
        self._replies = replies

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        joined = " ".join(argv)
        for needle, reply in self._replies.items():
            if needle in joined:
                return reply
        return CommandResult(1, "", "not found")


# --- the parser -------------------------------------------------------------


def test_the_registered_command_is_read_from_verbose_output():
    target = _task_target(VERBOSE)

    assert target.startswith(
        "C:\\Users\\ahmed\\AppData\\Local\\Programs\\RAGTools\\ragw.exe")
    assert "service run" in target


def test_the_summary_format_yields_nothing_and_that_is_the_point():
    """Documents WHY the query had to change: the command is simply not there."""
    assert _task_target(SUMMARY) == ""


def test_a_drive_letter_colon_does_not_truncate_the_path():
    """`Task To Run:` values contain `C:\\...` — a second colon. Splitting on the
    wrong one silently returns a path fragment that matches no install dir."""
    target = _task_target(VERBOSE)

    assert ":" in target[:3], f"the drive letter was lost: {target!r}"


def test_a_localised_label_still_yields_a_target():
    """Windows translates the label. Recognise the value by shape, not by name."""
    german = (
        "Aufgabenname:                         \\RAGTools\\Service\n"
        "Auszuf\u00fchrende Aufgabe:               "
        "C:\\Program Files\\RAGTools\\ragw.exe service run\n"
    )

    assert "ragw.exe" in _task_target(german)


def test_the_taskname_row_is_never_mistaken_for_a_command():
    noise = "TaskName:                             \\RAGTools\\Service\n"

    assert _task_target(noise) == ""


def test_empty_output_is_empty():
    assert _task_target("") == ""


# --- the query --------------------------------------------------------------


def test_the_query_asks_for_the_verbose_list_format():
    """The defect's other half. Without these flags there is nothing to parse."""
    runner = Runner({"/tn \\RAGTools\\Service": CommandResult(0, VERBOSE, "")})
    adapter = WindowsAdapter(runner, task_prefix="\\RAGTools")

    adapter.find_autostart(KIND_SERVICE)

    query = next(c for c in runner.calls if "/query" in c)
    assert "/fo" in query and "LIST" in query and "/v" in query, query


def test_a_registered_task_reports_its_real_target():
    runner = Runner({"/tn \\RAGTools\\Service": CommandResult(0, VERBOSE, "")})
    adapter = WindowsAdapter(runner, task_prefix="\\RAGTools")

    found = adapter.find_autostart(KIND_SERVICE)

    assert len(found) == 1
    assert "ragw.exe" in found[0].target


# --- the check that could neither pass nor fail -----------------------------


def _selfcheck_with(monkeypatch, registrations, install_dir):
    import ragtools.selfcheck as sc

    class Impl:
        def find_autostart(self, kind):
            return registrations.get(kind, [])

    monkeypatch.setattr(sc, "_is_packaged", lambda: True)
    monkeypatch.setattr(sc, "_adapter", lambda: Impl())
    monkeypatch.setattr(sc, "_install_dir", lambda: install_dir)
    return sc.check_autostart_targets()


class Reg:
    def __init__(self, name, target, legacy=False):
        self.name, self.target, self.legacy = name, target, legacy


def test_a_correct_registration_now_passes(monkeypatch, tmp_path):
    install = tmp_path / "Programs" / "RAGTools"
    install.mkdir(parents=True)
    result = _selfcheck_with(monkeypatch, {
        KIND_SERVICE: [Reg("\\RAGTools\\Service", str(install / "ragw.exe"))],
        KIND_TRAY: [Reg("\\RAGTools\\Tray", str(install / "ragw.exe"))],
    }, install)

    assert result.ok and not result.skipped, result
    assert "2 registration(s)" in result.detail


def test_a_stale_target_now_FAILS(monkeypatch, tmp_path):
    """The whole reason the check exists.

    Before the fix this produced `seen=0` and a SKIP reading "no autostart
    registered", so a machine that silently reverts to the previous build at
    every logon reported a clean bill of health.
    """
    install = tmp_path / "new"
    install.mkdir()
    result = _selfcheck_with(monkeypatch, {
        KIND_SERVICE: [Reg("\\RAGTools\\Service",
                           str(tmp_path / "old" / "ragw.exe"))],
    }, install)

    assert not result.ok and not result.skipped, result
    assert "old" in result.detail


def test_an_unreadable_target_is_a_finding_not_a_silent_skip(monkeypatch, tmp_path):
    """"Registered but unverifiable" must never read as "nothing registered"."""
    install = tmp_path / "new"
    install.mkdir()
    result = _selfcheck_with(monkeypatch, {
        KIND_SERVICE: [Reg("\\RAGTools\\Service", "")],
    }, install)

    assert not result.ok and not result.skipped, result
    assert "could not be read" in result.detail


def test_genuinely_nothing_registered_is_still_a_skip(monkeypatch, tmp_path):
    """The one case the old message was right about must keep working."""
    install = tmp_path / "new"
    install.mkdir()

    assert _selfcheck_with(monkeypatch, {}, install).skipped


def test_a_surviving_legacy_registration_still_fails(monkeypatch, tmp_path):
    install = tmp_path / "new"
    install.mkdir()
    result = _selfcheck_with(monkeypatch, {
        KIND_SERVICE: [Reg("RAGTools Watchdog", "whatever", legacy=True)],
    }, install)

    assert not result.ok
    assert "legacy" in result.detail


# --- the test-double gap that let this ship ---------------------------------


def test_the_adapter_suite_no_longer_relies_on_empty_stdout():
    """`OK = CommandResult(0, "", "")` fed the parser the exact input that
    produces the bug, so no unit test could have caught it. Any fixture used to
    exercise target discovery has to contain a target."""
    assert "Task To Run" in VERBOSE
    assert _task_target(VERBOSE) != ""
