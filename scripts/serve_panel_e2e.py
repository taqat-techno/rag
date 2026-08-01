"""Boot a REAL admin panel, in isolation, and run the browser suite against it.

WP-R10. ``tests/test_panel_e2e.py`` has been in the tree since S15 and has never
executed in CI: it is gated on ``RAG_E2E_PANEL_URL``, no workflow set it, and
Playwright was not even a declared dependency. Eight tests, zero runs, and a
green suite line that said "19 skipped" as if that were an aside.

This script closes that. One step does the whole thing — build the fixture,
boot the service, index it, prove the preconditions, run pytest, tear down —
because splitting it across workflow steps means managing a background process
across step boundaries, which behaves differently on all three platforms and is
the kind of arrangement that ends up quietly starting nothing.

THREE CONSTRAINTS IT HONOURS, EACH ONE PAID FOR SOMEWHERE ELSE
-------------------------------------------------------------
1. **It writes nothing into the repository.** ``tests/conftest.py``'s
   ``_no_repository_pollution`` guard exists because a test once left a real
   ``data/relayout.db`` in the working copy and ``rag selfcheck`` then reported a
   pending re-index that did not exist. The service booted here has its own
   ``RAG_DATA_DIR`` and its own ``RAG_CONFIG_PATH``, both under a temporary root.

2. **The pytest child does NOT inherit that isolation.** ``RAG_DATA_DIR`` in the
   environment marks ``data_dir`` as explicitly set, which defeats
   ``_anchor_data_dir`` and broke 29 unrelated tests the last time someone tried
   it globally (see the guard's own docstring). The child gets exactly one new
   variable: ``RAG_E2E_PANEL_URL``.

3. **The child is never spawned onto an inherited handle.** Same rule the
   managed engine follows: explicit ``stdout=``/``stderr=`` to a file, so what
   the service says about its own death survives.

The panel is deliberately booted on the **per-project layout with two populated
projects**, because ``test_per_project_search_is_isolated_in_the_browser`` skips
itself otherwise — and a job that green-lights while its isolation test excused
itself is the defect this whole work package is about. The preconditions are
asserted HERE, before pytest starts, so a mis-built fixture fails with a sentence
about the fixture rather than as a confusing skip three layers away.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Deliberately not 21420 (the installed service) or 21421 (the dev default).
#: A test harness that can collide with a real install is a test harness that
#: will eventually index into one.
DEFAULT_PORT = 21455

#: Two projects, each with enough prose that the encoder produces distinct
#: vectors and a search for "configuration" returns something. The isolation
#: test asserts a scoped query never returns the OTHER project's paths, so the
#: two corpora must be plausibly similar — not two unrelated words.
FIXTURE: dict[str, dict[str, str]] = {
    "alpha": {
        "README.md": (
            "# Alpha service\n\n"
            "## Configuration\n\n"
            "The alpha service reads its configuration from a TOML file and\n"
            "resolves every path relative to the data directory. Settings are\n"
            "validated at load rather than at first use.\n\n"
            "## Deployment\n\n"
            "Alpha is deployed as a single process. It holds an exclusive lock\n"
            "on its storage directory for the whole of its life.\n"),
        "configuration.md": (
            "# Alpha configuration reference\n\n"
            "## Storage\n\n"
            "Alpha stores vectors locally. The storage engine is selected by a\n"
            "configuration key and an unknown value is refused rather than\n"
            "silently downgraded.\n\n"
            "## Logging\n\n"
            "Rotating file handler, ten megabytes, three backups.\n"),
    },
    "beta": {
        "README.md": (
            "# Beta service\n\n"
            "## Configuration\n\n"
            "The beta service takes its configuration from the environment and\n"
            "falls back to a committed default document. Unknown keys are an\n"
            "error, not a warning.\n\n"
            "## Operations\n\n"
            "Beta runs as two processes and coordinates through a lock file.\n"),
        "configuration.md": (
            "# Beta configuration reference\n\n"
            "## Networking\n\n"
            "Beta binds loopback only. The bound port is reported by the\n"
            "identity endpoint so the configured value is never mistaken for\n"
            "the actual one.\n\n"
            "## Retention\n\n"
            "Beta keeps its configuration history for thirty days.\n"),
    },
}


def log(message: str) -> None:
    print(f"[panel-e2e] {message}", flush=True)


def build_fixture(root: Path, port: int) -> Path:
    """Write the isolated content tree and the config that describes it."""
    data = root / "data"
    content = root / "content"
    data.mkdir(parents=True, exist_ok=True)

    for project, files in FIXTURE.items():
        folder = content / project
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (folder / name).write_text(text, encoding="utf-8")

    def toml_path(p: Path) -> str:
        # TOML basic strings take backslash escapes; a Windows path written raw
        # produces an invalid document (or, worse, a valid one meaning
        # something else). Forward slashes are accepted by pathlib everywhere.
        return str(p.resolve()).replace("\\", "/")

    projects = "\n".join(
        "[[projects]]\n"
        f'id = "{name}"\n'
        f'name = "{name}"\n'
        f'path = "{toml_path(content / name)}"\n'
        "enabled = true\n"
        'mode = "docs"\n'
        for name in FIXTURE
    )

    config = root / "ragtools.toml"
    config.write_text(
        # `version`, `storage_backend` and `collection_strategy` are ALL stated
        # explicitly and on purpose. `migrate_config` adopts a recommended
        # default only for a key that is ABSENT, and adopting one opens a
        # relayout plan — which would leave /health reporting `migrating` and
        # the panel showing a migration banner the browser suite knows nothing
        # about. An explicit value is read as a decision and left alone.
        "version = 3\n"
        'storage_backend = "embedded"\n'
        'collection_strategy = "per_project"\n'
        'service_host = "127.0.0.1"\n'
        f"service_port = {port}\n"
        f'data_dir = "{toml_path(data)}"\n'
        f'qdrant_path = "{toml_path(data / "qdrant")}"\n'
        f'state_db = "{toml_path(data / "index_state.db")}"\n'
        'log_level = "INFO"\n'
        "\n" + projects,
        encoding="utf-8",
    )
    log(f"fixture at {root}")
    log(f"config: {config}")
    return config


def service_env(root: Path, config: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["RAG_CONFIG_PATH"] = str(config)
    env["RAG_DATA_DIR"] = str((root / "data").resolve())
    # `_default_env_file()` returns a bare ".env" in source mode, resolved
    # against the CWD. Point it at nothing so a stray file in a checkout cannot
    # reconfigure the panel under test.
    env["RAG_ENV_FILE"] = str((root / "no-such.env").resolve())
    # An ambient value for any of these overrides the file we just wrote, which
    # would silently test a different storage arrangement than the one declared.
    for leaky in ("RAG_STORAGE_BACKEND", "RAG_COLLECTION_STRATEGY",
                  "RAG_QDRANT_BINARY", "RAG_STORAGE_URL", "RAG_SERVICE_PORT",
                  "RAG_COLLECTION_NAME", "RAG_INSTANCE_ID"):
        env.pop(leaky, None)
    return env


def get_json(url: str, timeout: float = 15.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:   # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict, timeout: float):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def wait_for_health(base: str, deadline: float, proc: subprocess.Popen) -> dict:
    """Block until /health answers `ready`, or say precisely why it never did."""
    last = "no response yet"
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(
                f"the service exited with code {proc.returncode} before it "
                f"became ready. Its output is in the log dumped below.")
        try:
            payload = get_json(f"{base}/health", timeout=5.0)
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
        except Exception as exc:                       # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        else:
            status = payload.get("status")
            if status == "ready":
                return payload
            last = f"status={status!r}"
        time.sleep(2.0)
    raise SystemExit(f"the panel never became ready ({last})")


def assert_preconditions(base: str) -> None:
    """Prove the panel is in the shape the browser suite requires.

    Checked here rather than left to pytest because a precondition that fails
    inside a test reports as a SKIP, and a skip is the exact outcome this work
    package exists to make impossible.
    """
    status = get_json(f"{base}/api/status", timeout=30.0)
    strategy = status.get("collection_strategy")
    if strategy != "per_project":
        raise SystemExit(
            f"the panel is on the {strategy!r} layout; the isolation test needs "
            f"'per_project'. The config written by this script asks for it, so "
            f"something overrode it — check for an ambient RAG_* variable.")

    populated = [c for c in status.get("collections", [])
                 if c.get("kind") == "project" and (c.get("points") or 0) > 0]
    if len(populated) < 2:
        raise SystemExit(
            f"only {len(populated)} project collection(s) hold points; the "
            f"isolation test needs two. Collections reported: "
            f"{json.dumps(status.get('collections', []), indent=2)}")

    log(f"preconditions OK: layout={strategy}, populated projects="
        f"{[c.get('project') for c in populated]}, "
        f"points={status.get('points_count')}")


def dump(path: Path, label: str, tail: int = 120) -> None:
    """Print the tail of a log, without ever becoming the failure itself.

    Both halves of that are earned. The service log is read with
    ``errors="replace"``, which produces U+FFFD, and printing U+FFFD to a
    Windows console under cp1252 raises ``UnicodeEncodeError`` — so the routine
    that exists to EXPLAIN a failure crashed while explaining one, and took the
    teardown down with it because it ran inside a ``finally``.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"

    def emit(text: str) -> None:
        print(text.encode(encoding, errors="replace").decode(encoding,
                                                             errors="replace"),
              flush=True)

    if not path.is_file():
        log(f"{label}: (no file at {path})")
        return
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    log(f"--- {label} (last {min(tail, len(lines))} of {len(lines)} lines) ---")
    for line in lines[-tail:]:
        emit(f"    {line}")


def run_pytest(base: str, junit: Path | None, extra: list[str]) -> int:
    env = dict(os.environ)
    env["RAG_E2E_PANEL_URL"] = base
    # See the module docstring, constraint 2: the pytest process must NOT
    # inherit the service's isolation, only the URL.
    for inherited in ("RAG_DATA_DIR", "RAG_CONFIG_PATH", "RAG_ENV_FILE"):
        env.pop(inherited, None)

    cmd = [sys.executable, "-m", "pytest", "tests/test_panel_e2e.py", "-v",
           "-rs", "--color=no"]
    if junit is not None:
        cmd += [f"--junitxml={junit}"]
    cmd += extra
    log(f"running: {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=str(ROOT), env=env).returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--root", type=Path, default=None,
                        help="isolated working root (default: a temp dir)")
    parser.add_argument("--junit", type=Path, default=None,
                        help="where pytest writes its JUnit report")
    parser.add_argument("--boot-timeout", type=float, default=420.0,
                        help="seconds to wait for /health (the first run on a "
                             "cold runner downloads the embedding model)")
    parser.add_argument("--index-timeout", type=float, default=900.0)
    parser.add_argument("--keep", action="store_true",
                        help="do not delete the temporary root on exit")
    parser.add_argument("pytest_args", nargs="*",
                        help="extra arguments forwarded to pytest")
    args = parser.parse_args(argv)

    owns_root = args.root is None
    root = (args.root or Path(tempfile.mkdtemp(prefix="ragtools-panel-e2e-"))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_relative_to(ROOT):
        raise SystemExit(
            f"--root {root} is inside the repository. The panel writes a data "
            f"directory, and tests/conftest.py fails the suite for exactly that.")

    config = build_fixture(root, args.port)
    env = service_env(root, config)
    base = f"http://127.0.0.1:{args.port}"
    stdio = root / "service-stdio.log"
    service_log = root / "data" / "logs" / "service.log"

    log(f"booting: {sys.executable} -m ragtools.service.run "
        f"--host 127.0.0.1 --port {args.port}")
    handle = open(stdio, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ragtools.service.run",
         "--host", "127.0.0.1", "--port", str(args.port)],
        cwd=str(root),          # never the repo: nothing may land in the checkout
        env=env,
        stdout=handle,          # explicit, never inherited — see constraint 3
        stderr=subprocess.STDOUT,
    )

    code = 1
    try:
        health = wait_for_health(base, time.time() + args.boot_timeout, proc)
        log(f"ready: version={health.get('version')} "
            f"backend={health.get('storage_backend')} "
            f"layout={health.get('collection_strategy')}")

        log("indexing both fixture projects (synchronous)")
        result = post_json(f"{base}/api/index?wait=true",
                           {"project": None, "full": True},
                           timeout=args.index_timeout)
        log(f"index stats: {json.dumps(result.get('stats', result))[:400]}")

        assert_preconditions(base)
        code = run_pytest(base, args.junit, list(args.pytest_args))
        log(f"pytest exited {code}")
        if code == 5:
            # EXIT_NOTESTSCOLLECTED. The panel booted, indexed and passed its
            # preconditions, and then pytest collected NOTHING — which is what
            # a module-level `importorskip` on a missing Playwright looks like.
            # Named here because "5" reads like a crash and is not one.
            log("pytest collected no tests at all. The usual cause is that "
                "Playwright is not installed: install the `e2e` extra and run "
                "`python -m playwright install chromium`.")
    except SystemExit as exc:
        log(f"FAILED: {exc}")
        code = 1
    finally:
        # Teardown FIRST-CLASS: nothing here may be skipped because a
        # diagnostic raised. `dump` once threw UnicodeEncodeError on a Windows
        # console and the service was left running as a result.
        if code != 0:
            try:
                dump(stdio, "service stdout/stderr")
                dump(service_log, "service.log")
            except Exception as exc:                    # noqa: BLE001
                log(f"(could not print the service logs: {exc})")
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=30)
        handle.close()
        if owns_root and not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
