"""Standard streams under a GUI-subsystem build.

`ragw.exe` exists so Task Scheduler can start the service without putting a
terminal window on the desktop. The cost of a windowed build is that CPython
hands the process ``sys.stdout is None``, and the failure that causes is not the
obvious one: ``print()`` quietly does nothing on a ``None`` file, so the first
symptom is not silence, it is ``logging.StreamHandler`` raising
``AttributeError: 'NoneType' object has no attribute 'write'`` on the first
record — inside the service, at login, where nobody is watching.

These tests simulate that state directly rather than trusting a build.
"""

from __future__ import annotations

import io
import subprocess
import sys

import pytest

from ragtools._streams import ensure_std_streams


@pytest.fixture
def windowed(monkeypatch):
    """Returns a callable that puts this process into the windowed state.

    A callable, not applied in fixture setup, and the difference is not style.
    pytest's capture plugin resumes global capture between fixture setup and the
    test call, re-installing `sys.stdout` and `sys.stderr` — so a fixture that
    unbinds them is quietly undone before the test body runs. Written that way
    first, three of these tests passed against a stream that was never `None`,
    which is the same shape of vacuous pass the whole file exists to prevent.
    """
    def apply():
        for name in ("stdin", "stdout", "stderr"):
            monkeypatch.setattr(sys, name, None)
            monkeypatch.setattr(sys, f"__{name}__", None)
        assert sys.stdout is None, "the windowed state did not take effect"
    return apply


def test_a_console_build_is_left_alone():
    """The common case must be a no-op — this runs in every `rag` invocation."""
    before = (sys.stdin, sys.stdout, sys.stderr)

    assert ensure_std_streams() == []
    assert (sys.stdin, sys.stdout, sys.stderr) == before


def test_missing_streams_are_bound(windowed):
    windowed()
    assert sorted(ensure_std_streams()) == ["stderr", "stdin", "stdout"]
    assert sys.stdout is not None
    assert sys.stderr is not None
    assert sys.stdin is not None


def test_logging_survives_a_windowed_process(windowed):
    """The actual crash, reproduced.

    `logging.StreamHandler()` binds `sys.stderr` at construction. Under a
    windowed build with the streams unbound that is `None`, and the handler
    raises on its first record.
    """
    windowed()
    import logging

    ensure_std_streams()
    handler = logging.StreamHandler()
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, None)

    handler.emit(record)  # must not raise


def test_the_bound_stream_has_a_real_file_descriptor(windowed):
    """Why the null device and not `io.StringIO`.

    A `StringIO` satisfies `.write()` and fails the moment a stream is handed to
    a child process — which `spawn_detached` does. The service starts other
    processes; a stand-in without a `fileno()` moves the crash rather than
    fixing it.
    """
    windowed()
    ensure_std_streams()

    fd = sys.stdout.fileno()
    assert isinstance(fd, int) and fd >= 0
    # The operation that a StringIO would fail.
    subprocess.run([sys.executable, "-c", "pass"], stdout=sys.stdout, check=True)


def test_dunder_streams_are_restored_too(windowed):
    """`sys.__stderr__` is where logging reports its own handler failures and
    where an unhandled traceback is printed. Leaving it None turns a recoverable
    error into a second, more confusing one."""
    windowed()
    ensure_std_streams()

    assert sys.__stdout__ is not None
    assert sys.__stderr__ is not None


def test_it_is_idempotent(windowed):
    """Called from module scope in `cli.py`, which a test suite may import more
    than once."""
    windowed()
    ensure_std_streams()
    first = sys.stdout

    assert ensure_std_streams() == []
    assert sys.stdout is first


def test_a_partially_bound_process_only_gains_what_it_lacks(monkeypatch):
    """Nothing already usable is replaced — a caller that redirected stdout on
    purpose keeps its redirection."""
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "__stderr__", None)

    assert ensure_std_streams() == ["stderr"]
    assert sys.stdout is captured


def test_the_cli_binds_streams_before_rich_touches_them():
    """Ordering is the whole point, and import order is easy to "tidy" away.

    `rich.Console()` inspects its stream when constructed and a
    `logging.StreamHandler` binds one permanently, so `ensure_std_streams()` has
    to run before either import — not merely appear in the file.
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src" / "ragtools" / "cli.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    guard_line = next(
        (node.lineno for node in ast.walk(tree)
         if isinstance(node, ast.Call)
         and isinstance(node.func, ast.Name)
         and node.func.id == "ensure_std_streams"),
        None,
    )
    assert guard_line is not None, "cli.py no longer binds its standard streams"

    risky = [
        node.lineno for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and any(a.name.split(".")[0] in {"rich", "typer", "logging"}
                for a in (node.names if isinstance(node, ast.Import) else []))
        or (isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] in {"rich", "typer", "logging"})
    ]
    assert risky, "expected cli.py to import rich/typer at module scope"
    assert guard_line < min(risky), (
        f"ensure_std_streams() runs at line {guard_line}, after an import at "
        f"line {min(risky)} that binds a stream"
    )
