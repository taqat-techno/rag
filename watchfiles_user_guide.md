# watchfiles User Guide

> A comprehensive, practical guide for Python developers, platform engineers, and application builders.
> Covers watchfiles v1.1.x (October 2025). Written for a technical audience.

---

## Table of Contents

- [1. Introduction](#1-introduction)
- [2. Fundamentals](#2-fundamentals)
- [3. Architecture](#3-architecture)
- [4. Key Features](#4-key-features)
- [5. Installation and Setup](#5-installation-and-setup)
- [6. Quick Start](#6-quick-start)
- [7. Core API Reference](#7-core-api-reference)
- [8. Filtering](#8-filtering)
- [9. CLI Usage](#9-cli-usage)
- [10. Integration Patterns](#10-integration-patterns)
- [11. Platform Considerations](#11-platform-considerations)
- [12. Advanced Usage](#12-advanced-usage)
- [13. Performance Tuning](#13-performance-tuning)
- [14. Troubleshooting](#14-troubleshooting)
- [15. Common Pitfalls](#15-common-pitfalls)
- [16. Comparison and Tradeoffs](#16-comparison-and-tradeoffs)
- [17. Best Practices Checklist](#17-best-practices-checklist)
- [18. Quick Start Recap](#18-quick-start-recap)
- [19. References](#19-references)
- [Appendix A: Feature Summary](#appendix-a-feature-summary)
- [Appendix B: Recommended Learning Path](#appendix-b-recommended-learning-path)
- [Appendix C: Top 10 Implementation Tips](#appendix-c-top-10-implementation-tips)

---

## 1. Introduction

### What watchfiles Is

`watchfiles` is a high-performance Python library for detecting file system changes and triggering code reload. Built on a Rust core using the `notify` crate via PyO3 bindings, it leverages native OS file-watching APIs (inotify, FSEvents, ReadDirectoryChangesW) instead of polling, resulting in near-zero CPU usage when idle.

| Attribute | Value |
|-----------|-------|
| **Current Version** | 1.1.1 (October 14, 2025) |
| **License** | MIT |
| **Maintainer** | Samuel Colvin (author of Pydantic) |
| **GitHub Stars** | ~2,500 |
| **Dependents** | 173,000+ projects |
| **Python Support** | 3.9 – 3.14 (including free-threading 3.14t) |
| **Core Language** | Python + Rust (notify crate via PyO3) |
| **Repository** | https://github.com/samuelcolvin/watchfiles |
| **Documentation** | https://watchfiles.helpmanual.io/ |

### Who It Is For

- **Web developers** using Uvicorn, FastAPI, or Django who need fast auto-reload
- **Backend engineers** building file-triggered workflows (config reload, asset processing)
- **DevOps/tooling engineers** creating custom build watchers and test runners
- **Async Python developers** who need non-blocking file monitoring

### Problems It Solves

1. **Efficient file watching** — Native OS events instead of CPU-heavy polling
2. **Auto-reload** — Restart processes on code changes with built-in debouncing
3. **Cross-platform** — Single API for Linux, macOS, and Windows
4. **Async-native** — First-class asyncio/trio support via anyio
5. **Sensible defaults** — Built-in filtering of `.git`, `__pycache__`, `node_modules`, etc.

### History

The library was originally named `watchgod`. It was completely rewritten and renamed to `watchfiles` in 2022 (v0.10) to avoid confusion with the similarly-named `watchdog` library. The rewrite replaced the polling-based core with Rust's `notify` crate for native OS event detection.

---

## 2. Fundamentals

### How File Watching Works

Operating systems provide kernel-level mechanisms for monitoring file system changes:

| OS | Mechanism | How It Works |
|----|-----------|-------------|
| **Linux** | inotify | Kernel notifies userspace when files in watched directories change |
| **macOS** | FSEvents | Apple framework delivers events for file system modifications |
| **Windows** | ReadDirectoryChangesW | Win32 API reports directory content changes |

These mechanisms are **event-driven** — the OS pushes notifications to the application, rather than the application polling for changes. This is fundamentally more efficient than scanning file modification times.

### Key Terminology

| Term | Definition |
|------|-----------|
| **Change** | An enum: `added` (1), `modified` (2), or `deleted` (3) |
| **FileChange** | A tuple of `(Change, str)` — the change type and the absolute file path |
| **Debounce** | Grouping rapid changes into a single batch to avoid excessive triggers |
| **Step** | The Rust-level polling interval within the event loop (not file polling) |
| **Filter** | A callable that decides which changes to include or exclude |
| **Polling mode** | Fallback mode using file stat scanning instead of OS events |

### Core Concepts

1. **Watch** → yield batches of file changes as a generator
2. **Filter** → decide which file changes matter
3. **Debounce** → group rapid changes to prevent excessive restarts
4. **React** → run code, restart processes, or trigger builds on changes

---

## 3. Architecture

### Internal Design

```
Python API (watch, awatch, run_process, arun_process)
    ↓
PyO3 Bindings (RustNotify class)
    ↓
Rust `notify` crate (v8.0.0)
    ↓
OS-Specific Backend
  ├── Linux: inotify
  ├── macOS: FSEvents
  ├── Windows: ReadDirectoryChangesW
  └── Fallback: Polling
```

### Key Architectural Decisions

**1. Rust core via PyO3:** All file system integration, event collection, and debouncing happen in Rust. Python only handles filtering and presentation. This keeps CPU usage near-zero when idle.

**2. Threading model:** The Rust code creates a new OS thread to watch for file changes. The GIL is released during `step_ms` sleep intervals, so other Python threads can run freely.

**3. Async via anyio:** Async methods (`awatch`, `arun_process`) use `anyio.to_thread.run_sync` to delegate blocking Rust calls to a thread. This makes them compatible with both asyncio and trio.

**4. Rust-level debouncing:** Change debouncing occurs in Rust, not Python. Once a change is detected, the watcher accumulates changes for `debounce_ms` milliseconds, then returns the batch to Python.

### RustNotify Class

The low-level Rust/Python bridge:

```python
class RustNotify:
    def __init__(
        self,
        watch_paths: list[str],
        debug: bool,
        force_polling: bool,
        poll_delay_ms: int,
        recursive: bool,
        ignore_permission_denied: bool,
    ) -> None: ...

    def watch(
        self,
        debounce_ms: int,
        step_ms: int,
        timeout_ms: int,
        stop_event: AbstractEvent | None,
    ) -> set[tuple[int, str]] | Literal['signal', 'stop', 'timeout']: ...

    def close(self) -> None: ...
```

Return values from `watch()`:
- `set[tuple[int, str]]` — detected changes (event type int + path string)
- `'signal'` — a system signal was received
- `'stop'` — the stop event was triggered
- `'timeout'` — the timeout elapsed with no changes

> **Note:** Most users never interact with `RustNotify` directly. The `watch()` and `awatch()` functions wrap it with filtering, type conversion, and lifecycle management.

---

## 4. Key Features

### Synchronous and Async Watching

```python
# Sync — blocking generator
from watchfiles import watch
for changes in watch('./src'):
    print(changes)

# Async — async generator
from watchfiles import awatch
async for changes in awatch('./src'):
    print(changes)
```

### Filtering

Built-in filters exclude common noise (`.git`, `__pycache__`, `node_modules`, etc.). Custom filters via callables or subclasses.

### Debouncing

Groups rapid file changes into batches (default 1600ms). Prevents excessive restarts when an IDE saves multiple files at once.

### Process Restart

Built-in `run_process()` and `arun_process()` run a function or command in a subprocess and restart it on file changes.

### Recursive Watching

Watches subdirectories by default. Configurable via `recursive=False`.

### Polling Fallback

Automatic fallback to polling when native OS events are unavailable (network filesystems, Docker bind mounts, WSL2). Configurable via `force_polling=True` or `WATCHFILES_FORCE_POLLING=1`.

### CLI Tool

Built-in command-line interface for running commands on file changes without writing Python code.

### Multiple Paths

Watch multiple directories simultaneously:

```python
for changes in watch('./src', './tests', './config'):
    print(changes)
```

---

## 5. Installation and Setup

### Installation

```bash
# From PyPI (recommended)
pip install watchfiles

# With ONNX support for specific backends (optional extras don't exist — just pip install)
pip install watchfiles

# From conda-forge
conda install -c conda-forge watchfiles

# From source (requires Rust stable compiler)
pip install watchfiles --no-binary watchfiles
```

Pre-built binary wheels are available for:
- Linux: x86_64, aarch64, i686, ppc64le, s390x
- macOS: x86_64, arm64 (Apple Silicon)
- Windows: x86_64, i686

### Dependencies

| Package | Purpose |
|---------|---------|
| `anyio` (>= 3.0.0) | Async compatibility (asyncio + trio) |

The Rust component is compiled into the wheel — no Rust toolchain needed for pip install.

### Verification

```python
import watchfiles
print(watchfiles.__version__)  # 1.1.1

from watchfiles import watch, Change
print(Change.added, Change.modified, Change.deleted)
# Change.added Change.modified Change.deleted
```

```bash
# CLI verification
watchfiles --version
```

---

## 6. Quick Start

### Watch for File Changes

```python
from watchfiles import watch

for changes in watch('./src'):
    for change_type, path in changes:
        print(f"{change_type.name}: {path}")
# Output: modified: /abs/path/to/src/main.py
```

### Watch Asynchronously

```python
import asyncio
from watchfiles import awatch

async def main():
    async for changes in awatch('./src'):
        for change_type, path in changes:
            print(f"{change_type.name}: {path}")

asyncio.run(main())
```

### Auto-Restart a Process

```python
from watchfiles import run_process

def my_server():
    print("Server starting...")
    # your server code here

run_process('./src', target=my_server)
# Automatically restarts my_server() when files in ./src change
```

### CLI — Run Tests on Changes

```bash
watchfiles 'pytest --lf' src tests
```

### Stop Watching Programmatically

```python
import threading
from watchfiles import watch

stop = threading.Event()

# In another thread: stop.set()

for changes in watch('./src', stop_event=stop):
    print(changes)
```

---

## 7. Core API Reference

### Change Enum

```python
from watchfiles import Change

class Change(IntEnum):
    added = 1      # New file or directory created
    modified = 2   # File content or metadata changed
    deleted = 3    # File or directory removed
```

### `watch()` — Synchronous Generator

```python
def watch(
    *paths: Path | str,                                    # One or more paths to monitor
    watch_filter: Callable[[Change, str], bool] | None = DefaultFilter(),
    debounce: int = 1600,                                  # ms to batch changes
    step: int = 50,                                        # ms between Rust loop iterations
    stop_event: threading.Event | None = None,             # External stop signal
    rust_timeout: int = 5000,                              # ms timeout for Rust watcher
    yield_on_timeout: bool = False,                        # Yield empty set on timeout
    debug: bool | None = None,                             # Log raw events to stderr
    raise_interrupt: bool = True,                          # Re-raise KeyboardInterrupt
    force_polling: bool | None = None,                     # Force polling mode
    poll_delay_ms: int = 300,                              # Delay between polls
    recursive: bool = True,                                # Watch subdirectories
    ignore_permission_denied: bool | None = None,          # Skip permission errors
) -> Generator[set[tuple[Change, str]], None, None]:
```

Each iteration yields a `set` of `(Change, str)` tuples — the change type and the absolute file path.

### `awatch()` — Async Generator

Same signature as `watch()` but:
- `stop_event` accepts `anyio.Event` (not `threading.Event`)
- `raise_interrupt` defaults to `None`
- Returns an `AsyncGenerator`

```python
async for changes in awatch('./src'):
    process(changes)
```

### `run_process()` — Synchronous Process Runner

```python
def run_process(
    *paths: Path | str,
    target: str | Callable,              # Command string or Python function
    args: tuple = (),                    # Args for function targets
    kwargs: dict | None = None,          # Kwargs for function targets
    target_type: str = 'auto',           # 'auto', 'function', or 'command'
    callback: Callable | None = None,    # Called on each reload with changes
    watch_filter: ... = DefaultFilter(),
    grace_period: float = 0,             # Seconds before monitoring starts
    debounce: int = 1600,
    step: int = 50,
    sigint_timeout: int = 5,             # Seconds before SIGKILL after SIGINT
    sigkill_timeout: int = 1,            # Seconds before exception after SIGKILL
    recursive: bool = True,
    ignore_permission_denied: bool = False,
) -> int:                                # Returns number of reloads
```

**target_type detection:**
- `'function'` — calls `target` via `multiprocessing.Process`
- `'command'` — runs `target` via `subprocess.Popen`
- `'auto'` — auto-detects based on type and content

### `arun_process()` — Async Process Runner

Same parameters as `run_process()`. The `callback` can be a coroutine function.

> **Warning:** `arun_process` cannot suppress `KeyboardInterrupt` within the async function.

### Parameters Reference

| Parameter | Default | Description |
|-----------|---------|-------------|
| `debounce` | 1600 ms | Time to batch changes before yielding. Lower = faster response, more frequent triggers |
| `step` | 50 ms | Rust event loop interval. GIL released during sleep |
| `rust_timeout` | 5000 ms | Timeout for Rust watcher per iteration. 0 = wait indefinitely |
| `force_polling` | None | Force stat-based polling. Auto-set on WSL2. Also: `WATCHFILES_FORCE_POLLING` env var |
| `poll_delay_ms` | 300 ms | Delay between polls in polling mode. Also: `WATCHFILES_POLL_DELAY_MS` env var |
| `recursive` | True | Watch subdirectories |
| `grace_period` | 0 | Seconds to wait after process starts before monitoring (run_process only) |
| `sigint_timeout` | 5 | Seconds to wait after SIGINT before SIGKILL (run_process only) |

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `WATCHFILES_FORCE_POLLING` | Force polling mode when set to any truthy value |
| `WATCHFILES_POLL_DELAY_MS` | Override `poll_delay_ms` (added in v1.0.0) |

---

## 8. Filtering

### DefaultFilter

The default filter excludes common development noise:

**Ignored directories:**
```python
{
    '__pycache__', '.git', '.hg', '.svn', '.tox', '.venv',
    'site-packages', '.idea', 'node_modules', '.mypy_cache',
    '.pytest_cache', '.hypothesis'
}
```

**Ignored file patterns:** Compiled bytecode (`.pyc`, `.pyo`, `.pyd`), editor temp files (`.swp`, `.swx`, `~`), system files (`.DS_Store`).

### PythonFilter

Watches only Python files (`.py`, `.pyx`, `.pyd`):

```python
from watchfiles import watch, PythonFilter

for changes in watch('./src', watch_filter=PythonFilter()):
    print(changes)

# With extra extensions
for changes in watch('./src', watch_filter=PythonFilter(extra_extensions=('.toml', '.cfg'))):
    print(changes)
```

### Custom Filter — Class-Based

Extend `DefaultFilter` to inherit its ignore rules and add your own:

```python
from watchfiles import watch, Change
from watchfiles.filters import DefaultFilter

class WebFilter(DefaultFilter):
    allowed_extensions = ('.html', '.css', '.js', '.ts', '.tsx')

    def __call__(self, change: Change, path: str) -> bool:
        return super().__call__(change, path) and path.endswith(self.allowed_extensions)

for changes in watch('./frontend', watch_filter=WebFilter()):
    print(changes)
```

### Custom Filter — Function-Based

A simple callable also works:

```python
# Only watch Python files
def python_only(change: Change, path: str) -> bool:
    return path.endswith('.py')

for changes in watch('./src', watch_filter=python_only):
    print(changes)

# Only watch for new files
def only_added(change: Change, path: str) -> bool:
    return change == Change.added

for changes in watch('.', watch_filter=only_added):
    print(changes)

# Lambda filter
for changes in watch('.', watch_filter=lambda c, p: p.endswith(('.yaml', '.json'))):
    print(changes)
```

### Disable All Filtering

Pass `None` to see every change, including `.git`, `__pycache__`, etc.:

```python
for changes in watch('.', watch_filter=None):
    print(changes)
```

### BaseFilter

The foundation class for all filters. Provides three ignore mechanisms:

- `ignore_dirs`: Set of directory names to exclude
- `ignore_entity_patterns`: Tuple of regex patterns for files/dirs to exclude
- `ignore_paths`: Tuple of absolute paths to exclude

---

## 9. CLI Usage

### Basic Syntax

```bash
watchfiles [options] target [paths...]
```

The CLI can also be invoked as `python -m watchfiles`.

### Arguments

- **`target`** (required): A shell command string (e.g., `'pytest --lf'`) or a dotted Python function path (e.g., `myapp.main`)
- **`paths`** (optional): Filesystem paths to watch. Defaults to current directory.

### Options

| Option | Description |
|--------|-------------|
| `--filter [FILTER]` | `"default"`, `"python"`, `"all"`, or dotted path to custom class |
| `--ignore-paths` | Comma-separated directories to exclude |
| `--target-type` | `"command"`, `"function"`, or `"auto"` (default) |
| `--args` | Arguments passed to sys.argv when calling a target function |
| `--non-recursive` | Don't watch subdirectories |
| `--verbosity` | `"warning"`, `"info"`, or `"debug"` |
| `--verbose` | Shortcut for `--verbosity debug` |
| `--sigint-timeout` | Seconds before SIGKILL after SIGINT |
| `--sigkill-timeout` | Seconds before timeout exception after SIGKILL |
| `--grace-period` | Seconds before monitoring starts after process launch |
| `--ignore-permission-denied` | Suppress permission errors |
| `--version, -V` | Display version |

### Examples

```bash
# Re-run pytest on any file change
watchfiles 'pytest --lf' src tests

# Watch only Python files
watchfiles --filter python 'pytest -x' .

# Run a Python function on changes
watchfiles myapp.main ./src

# Non-recursive, verbose
watchfiles --non-recursive --verbose 'make build' ./src

# Custom ignore paths
watchfiles --ignore-paths dist,build 'npm run build' ./src

# Watch all files (no filter)
watchfiles --filter all 'echo "changed!"' ./data
```

---

## 10. Integration Patterns

### Uvicorn / FastAPI

Uvicorn uses watchfiles as its default reloader when installed:

```bash
# Install with watchfiles support
pip install uvicorn[standard]

# Run with auto-reload
uvicorn myapp:app --reload
```

If watchfiles is not installed, Uvicorn falls back to polling-based file modification time checking. With watchfiles installed, additional options become available:

```bash
uvicorn myapp:app --reload --reload-include '*.html' --reload-exclude 'test_*'
```

FastAPI's `fastapi dev` command uses Uvicorn with reload enabled by default.

### Django

The `django-watchfiles` package replaces Django's default polling `StatReloader`:

```bash
pip install django-watchfiles
```

```python
# settings.py
INSTALLED_APPS = [
    'django_watchfiles',
    # ... other apps
]
```

**Performance difference** (benchmark: 385K lines of code, 206 packages, M1 MacBook):

| Metric | Django StatReloader | django-watchfiles |
|--------|--------------------|--------------------|
| CPU usage (idle) | ~10% every other second | ~0% |
| Detection latency | 1+ second | ~50ms |

### asyncio Applications

```python
import asyncio
from watchfiles import awatch, Change

async def watch_config():
    async for changes in awatch('./config'):
        for change_type, path in changes:
            if change_type == Change.modified and path.endswith('.yaml'):
                print(f"Reloading config: {path}")
                await reload_config(path)

async def main():
    await asyncio.gather(
        watch_config(),
        run_application(),
    )

asyncio.run(main())
```

### Custom Build Pipelines

```python
from watchfiles import run_process

# Run npm build on frontend changes
run_process(
    './frontend/src',
    target='npm run build',
    watch_filter=lambda c, p: p.endswith(('.ts', '.tsx', '.css', '.html')),
    debounce=2000,  # Wait 2s for IDE to finish saving
)
```

### Test Runner

```python
from watchfiles import run_process

def run_tests():
    import subprocess
    subprocess.run(['pytest', '--lf', '-x'], check=False)

reload_count = run_process(
    './src', './tests',
    target=run_tests,
    watch_filter=lambda c, p: p.endswith('.py'),
)
print(f"Tests re-run {reload_count} times")
```

### With a Callback

```python
from watchfiles import run_process

def on_reload(changes):
    files = [path for _, path in changes]
    print(f"Restarting due to changes in: {files}")

run_process(
    './src',
    target=my_server,
    callback=on_reload,
)
```

---

## 11. Platform Considerations

### Linux — inotify

| Aspect | Details |
|--------|---------|
| **Mechanism** | Kernel inotify API |
| **Watch limit** | Default `fs.inotify.max_user_watches` is 8192 on many distros |
| **Instance limit** | Default `fs.inotify.max_user_instances` is 128 |
| **Fix** | `sudo sysctl -w fs.inotify.max_user_watches=524288` |
| **Persist** | Add `fs.inotify.max_user_watches=524288` to `/etc/sysctl.conf` |

Each watched directory consumes one inotify watch. Large monorepos with many directories can exceed the default limit.

### macOS — FSEvents

- Uses Apple's FSEvents framework
- No practical watch limits
- Reliable with no known major issues
- File rename tracking works correctly (fixed in v0.14)

### Windows — ReadDirectoryChangesW

- Uses Win32 API `ReadDirectoryChangesW`
- Command splitting behavior differs from Unix (fixed in v0.18.1)
- Python 3.14 Windows builds added in v1.1.1

### WSL2

Since v0.18.0, watchfiles **automatically forces polling mode** on WSL because inotify does not reliably propagate events across the WSL/Windows filesystem boundary. No manual configuration needed, but expect slightly higher CPU usage and latency compared to native Linux.

### Docker / Container Bind Mounts

Docker bind mounts (host → container) do **not** propagate inotify events. You must use polling mode:

```bash
# Environment variable (recommended for Docker)
WATCHFILES_FORCE_POLLING=1

# Or in docker-compose.yml
environment:
  - WATCHFILES_FORCE_POLLING=true
```

```python
# Or in code
for changes in watch('./src', force_polling=True):
    print(changes)
```

### Network Filesystems (NFS, SMB, CIFS)

Native file system events are not propagated over network filesystems. Use `force_polling=True`.

### Platform Summary

| Platform | Backend | Polling Needed? | Notes |
|----------|---------|-----------------|-------|
| Linux (native) | inotify | No | May need to increase watch limit |
| macOS | FSEvents | No | Reliable |
| Windows | ReadDirectoryChangesW | No | Minor quirks fixed in recent versions |
| WSL2 | Polling (auto) | Auto-forced | Since v0.18.0 |
| Docker bind mounts | Polling (manual) | Yes | Set `WATCHFILES_FORCE_POLLING=1` |
| NFS/SMB/CIFS | Polling (manual) | Yes | Set `WATCHFILES_FORCE_POLLING=1` |

---

## 12. Advanced Usage

### Debounce Tuning

The default 1600ms debounce groups rapid changes. Adjust for your use case:

```python
# Low-latency monitoring (react quickly, risk frequent triggers)
for changes in watch('./src', debounce=200):
    print(changes)

# High-latency batch processing (group changes, fewer triggers)
for changes in watch('./data', debounce=5000):
    process_batch(changes)
```

### Timeout and Yield-on-Timeout

```python
# Yield empty set every 5 seconds even if no changes
for changes in watch('./src', rust_timeout=5000, yield_on_timeout=True):
    if changes:
        process_changes(changes)
    else:
        print("Heartbeat — no changes")
```

### Stop Event

```python
import threading
from watchfiles import watch

stop = threading.Event()

def watcher():
    for changes in watch('./src', stop_event=stop):
        print(changes)
    print("Watcher stopped")

thread = threading.Thread(target=watcher, daemon=True)
thread.start()

# Later...
stop.set()  # Cleanly stops the watcher
thread.join()
```

Async version:

```python
import asyncio
from watchfiles import awatch

async def main():
    stop = asyncio.Event()

    async def watcher():
        async for changes in awatch('./src', stop_event=stop):
            print(changes)

    task = asyncio.create_task(watcher())

    await asyncio.sleep(30)
    stop.set()
    await task
```

### Grace Period

Avoid restarting immediately after the process starts (useful when the process modifies files during startup):

```python
from watchfiles import run_process

run_process(
    './src',
    target=my_server,
    grace_period=3.0,  # Ignore changes for 3 seconds after start
)
```

### Debug Mode

Log raw file system events to stderr:

```python
for changes in watch('./src', debug=True):
    print(changes)
```

Or via CLI:

```bash
watchfiles --verbose 'pytest' src
```

### Non-Recursive Watching

Watch only the specified directory, not subdirectories:

```python
for changes in watch('./config', recursive=False):
    print(changes)
```

### Direct RustNotify Usage

For advanced scenarios requiring lower-level control:

```python
from watchfiles._rust_notify import RustNotify

watcher = RustNotify(
    watch_paths=['/path/to/watch'],
    debug=False,
    force_polling=False,
    poll_delay_ms=300,
    recursive=True,
    ignore_permission_denied=False,
)

try:
    while True:
        result = watcher.watch(
            debounce_ms=1600,
            step_ms=50,
            timeout_ms=5000,
            stop_event=None,
        )
        if isinstance(result, set):
            for event_type, path in result:
                print(f"Event {event_type}: {path}")
        elif result == 'timeout':
            continue
        elif result in ('signal', 'stop'):
            break
finally:
    watcher.close()
```

> **Warning:** You must call `watcher.close()` to terminate the watching thread and prevent resource leaks.

---

## 13. Performance Tuning

### CPU Efficiency

| Scenario | CPU Usage |
|----------|-----------|
| watchfiles (native events, idle) | ~0% |
| watchfiles (polling mode, idle) | Low (depends on `poll_delay_ms`) |
| Python polling alternatives (idle) | 5-10% periodic spikes |
| Django StatReloader | ~10% every other second |

### Detection Latency

Total time from file change to Python receiving the notification:

```
Native events: step (50ms) + debounce (1600ms) = ~1650ms maximum
Polling mode:  poll_delay (300ms) + debounce (1600ms) = ~1900ms maximum
```

To reduce latency:
- Lower `debounce` (e.g., 200-500ms) — faster response, more frequent triggers
- Lower `step` (e.g., 20ms) — marginal improvement, slightly more CPU
- Lower `poll_delay_ms` (e.g., 100ms) — only affects polling mode, more CPU

### Memory

- The Rust watcher thread has minimal memory overhead
- Memory scales with watched directories (not files)
- No Python objects created for intermediate events — they accumulate in Rust

### Tuning Guide

| Goal | debounce | step | Notes |
|------|----------|------|-------|
| Fastest response | 100-300 ms | 20 ms | May cause rapid restarts on multi-file saves |
| Balanced (default) | 1600 ms | 50 ms | Good for code reload workflows |
| Batch processing | 3000-10000 ms | 100 ms | Groups many changes into single batches |
| Minimal CPU (polling) | 1600 ms | 50 ms | Increase `poll_delay_ms` to 500-1000 |

---

## 14. Troubleshooting

### "inotify watch limit reached" (Linux)

**Symptom**: `OSError: inotify watch limit reached`

**Fix**:
```bash
# Temporary (resets on reboot)
sudo sysctl -w fs.inotify.max_user_watches=524288

# Permanent
echo 'fs.inotify.max_user_watches=524288' | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

### File changes not detected in Docker

**Symptom**: Watching doesn't detect file changes in Docker containers with bind mounts.

**Fix**: Enable polling mode:
```bash
# docker-compose.yml
environment:
  - WATCHFILES_FORCE_POLLING=true
```

### Changes not detected on network filesystem

**Symptom**: No events on NFS, SMB, or CIFS mounts.

**Fix**: `force_polling=True` or `WATCHFILES_FORCE_POLLING=1`.

### Debounce feels too slow

**Symptom**: 1.6 seconds between file save and process restart.

**Fix**: Lower `debounce`:
```python
for changes in watch('./src', debounce=300):
    ...
```

**Tradeoff**: Too-low debounce may trigger multiple restarts when your IDE saves multiple files.

### Too many restarts

**Symptom**: Process restarts multiple times for a single "save all" operation.

**Fix**: Increase `debounce` (e.g., 2000-3000ms) to batch all saves from the IDE.

### Permission errors

**Symptom**: `PermissionError` when watching directories with restricted subdirectories.

**Fix**:
```python
for changes in watch('./root', ignore_permission_denied=True):
    ...
```

### WSL2 — slow detection

**Symptom**: File changes detected with extra latency on WSL2.

**Cause**: watchfiles auto-forces polling on WSL2 since v0.18.0 because inotify is unreliable.

**Mitigation**: Lower `poll_delay_ms`:
```bash
WATCHFILES_POLL_DELAY_MS=100
```

---

## 15. Common Pitfalls

### Not Filtering Appropriately

| Pitfall | Consequence | Fix |
|---------|-------------|-----|
| Using `watch_filter=None` in dev | `.git` changes trigger restarts on every commit | Use `DefaultFilter()` (default) |
| Not filtering build output | Build artifacts trigger re-build loops | Add build dirs to filter exclusions |
| Watching `node_modules` | Thousands of irrelevant change events | DefaultFilter already excludes it |

### Ignoring Platform Differences

- **Docker**: Bind mounts don't propagate inotify events → set `WATCHFILES_FORCE_POLLING=1`
- **WSL2**: Auto-polled since v0.18.0 → no fix needed, but expect higher latency
- **Linux**: inotify limits → increase `fs.inotify.max_user_watches` for large projects

### Debounce Misconfiguration

- **Too low** (< 200ms): Multiple restarts from a single "save all" in your IDE
- **Too high** (> 5000ms): Feels unresponsive for interactive development
- **Not tuned for polling**: In polling mode, the effective delay is `poll_delay_ms` + `debounce`

### Not Using stop_event

If you start a watcher without a stop mechanism, it runs indefinitely:

```python
# Bad — no way to stop
for changes in watch('./src'):
    ...

# Good — can be stopped externally
stop = threading.Event()
for changes in watch('./src', stop_event=stop):
    ...
```

### Forgetting to Close RustNotify

If using the low-level `RustNotify` class directly, always call `.close()` to terminate the watching thread:

```python
watcher = RustNotify(['/path'], False, False, 300, True, False)
try:
    # ... use watcher ...
finally:
    watcher.close()  # Essential — prevents resource leak
```

### Confusing watchfiles with watchdog

These are different libraries:
- **watchfiles** — Rust-based, generator/async API, by Samuel Colvin
- **watchdog** — Python/C, observer pattern, by Yesudeep Mangalapilly

They are not interchangeable. Don't install one expecting the other's API.

---

## 16. Comparison and Tradeoffs

### watchfiles vs. watchdog

| Aspect | watchfiles | watchdog |
|--------|------------|----------|
| **Core** | Rust (notify crate via PyO3) | Python + C extensions |
| **API** | Generator / async generator | Observer pattern (event handlers) |
| **Async** | Native (awatch, arun_process) | No native async |
| **Debounce** | Built-in (Rust level) | Manual implementation needed |
| **Process restart** | Built-in (run_process) | Via `watchmedo` CLI |
| **CLI** | Built-in | Separate (`watchmedo`) |
| **Filtering** | Filter classes/callables | Event handler patterns |
| **CPU (idle)** | Near-zero | Low (native events) |
| **Maturity** | 2022 rewrite | Since 2010 |
| **Ecosystem** | Uvicorn/FastAPI default | Broader community |
| **Framework compat** | anyio (asyncio + trio) | Threading only |

**Choose watchfiles when:** You use Uvicorn/FastAPI, need async support, want built-in debouncing and process restart, or prefer a generator-based API.

**Choose watchdog when:** You need the observer pattern, have existing watchdog integrations, need the broader ecosystem, or prefer a more mature library.

### watchfiles vs. inotify / pyinotify

| Aspect | watchfiles | pyinotify |
|--------|------------|-----------|
| **Cross-platform** | Yes | Linux only |
| **API level** | High-level Python | Low-level inotify wrapper |
| **Maintenance** | Active | Largely unmaintained |
| **Polling fallback** | Automatic | No |

### watchfiles vs. manual polling

| Aspect | watchfiles | Manual `os.stat()` polling |
|--------|------------|---------------------------|
| **CPU (idle)** | ~0% | 5-10% periodic spikes |
| **Detection latency** | 50ms + debounce | Polling interval |
| **Complexity** | One function call | Custom loop, stat tracking, diffing |
| **Reliability** | OS-level events | May miss rapid changes between polls |

### When watchfiles May Not Be the Best Choice

| Scenario | Alternative |
|----------|-------------|
| Need observer pattern API | watchdog |
| Linux-only, need lowest overhead | Direct inotify via C |
| Shell scripting, not Python | fswatch, inotifywait |
| Need to watch millions of files | Consider event-driven architecture or inotify directly |
| Embedded/constrained environments | No Rust dependency — use pure Python |

---

## 17. Best Practices Checklist

- [ ] **Use the default filter** — don't pass `watch_filter=None` unless you have a reason
- [ ] **Handle Docker properly** — set `WATCHFILES_FORCE_POLLING=1` for bind mount containers
- [ ] **Increase inotify limits on Linux** — `fs.inotify.max_user_watches=524288` for large projects
- [ ] **Tune debounce for your workflow** — 1600ms is good for code reload; lower for real-time, higher for batch
- [ ] **Use `stop_event`** for controllable lifecycle in threaded/async applications
- [ ] **Use `PythonFilter`** when you only care about `.py` file changes
- [ ] **Use `run_process()` for auto-restart** — don't reinvent process management
- [ ] **Set `grace_period`** if your process modifies files during startup
- [ ] **Test in your deployment environment** — polling behavior differs from native events
- [ ] **Close `RustNotify`** if using the low-level API directly
- [ ] **Use the CLI** for quick one-off workflows (test runners, build tools)
- [ ] **Install `uvicorn[standard]`** to get watchfiles as the Uvicorn reloader
- [ ] **Use `django-watchfiles`** in Django projects for near-zero CPU reload
- [ ] **Don't confuse with watchdog** — they are different libraries with different APIs

---

## 18. Quick Start Recap

```bash
pip install watchfiles
```

```python
from watchfiles import watch, awatch, run_process, Change

# --- Sync watching ---
for changes in watch('./src'):
    for change_type, path in changes:
        print(f"{change_type.name}: {path}")

# --- Async watching ---
import asyncio

async def main():
    async for changes in awatch('./src'):
        print(changes)

asyncio.run(main())

# --- Auto-restart process on changes ---
def my_app():
    print("App running...")
    import time; time.sleep(999)

run_process('./src', target=my_app)

# --- CLI ---
# watchfiles 'pytest --lf' src tests
```

---

## 19. References

### Official

- [watchfiles Documentation](https://watchfiles.helpmanual.io/) — Official docs
- [watchfiles on PyPI](https://pypi.org/project/watchfiles/) — Package page
- [watchfiles GitHub](https://github.com/samuelcolvin/watchfiles) — Source code
- [watchfiles Releases](https://github.com/samuelcolvin/watchfiles/releases) — Changelog
- [Migration from watchgod](https://watchfiles.helpmanual.io/migrating/) — Migration guide
- [Rust Backend API](https://watchfiles.helpmanual.io/api/rust_backend/) — RustNotify reference
- [Filters API](https://watchfiles.helpmanual.io/api/filters/) — Filter classes
- [CLI Documentation](https://watchfiles.helpmanual.io/cli/) — CLI reference

### Integrations

- [django-watchfiles](https://github.com/adamchainz/django-watchfiles) — Django integration
- [Uvicorn Settings](https://uvicorn.dev/settings/) — Uvicorn reload configuration

### Related

- [Rust notify crate](https://docs.rs/notify/latest/notify/) — Underlying Rust library
- [PyO3](https://pyo3.rs/) — Python/Rust bindings framework

---

## Appendix A: Feature Summary

| Category | Features |
|----------|----------|
| **Watching** | Sync (`watch`), async (`awatch`), multi-path, recursive, non-recursive |
| **Process Management** | `run_process`, `arun_process`, auto-restart, grace period, signal handling |
| **Filtering** | `DefaultFilter`, `PythonFilter`, custom class/callable, regex patterns |
| **Debouncing** | Rust-level, configurable (default 1600ms) |
| **Backends** | inotify (Linux), FSEvents (macOS), ReadDirectoryChangesW (Windows), polling fallback |
| **Async** | anyio-based (asyncio + trio compatible) |
| **CLI** | Built-in `watchfiles` command for commands and Python functions |
| **Configuration** | Parameters + environment variables (`WATCHFILES_FORCE_POLLING`, `WATCHFILES_POLL_DELAY_MS`) |
| **Change Types** | `added`, `modified`, `deleted` |
| **Integrations** | Uvicorn (default reloader), Django (django-watchfiles), FastAPI |

## Appendix B: Recommended Learning Path

1. **Hour 1**: Install, run `watch('./src')` in a script, edit a file, see the output.
2. **Hour 2**: Try the CLI — `watchfiles 'pytest' src tests`. Edit a test, watch it re-run.
3. **Day 1**: Use `run_process()` to auto-restart a server on code changes. Try different `debounce` values.
4. **Day 2**: Write a custom filter. Try `PythonFilter`. Understand `DefaultFilter` exclusions.
5. **Day 3**: Use `awatch()` in an async application. Combine with `asyncio.gather()` for concurrent tasks.
6. **Week 1**: Set up `uvicorn[standard]` and `django-watchfiles`. Compare CPU usage with default reloaders.
7. **Week 2**: Handle Docker and WSL2 scenarios. Configure polling fallback. Tune for your CI/CD pipeline.
8. **Ongoing**: Use `stop_event` for lifecycle management. Monitor inotify limits on Linux. Update watchfiles with new releases.

## Appendix C: Top 10 Implementation Tips

1. **Start with defaults** — `DefaultFilter` and 1600ms debounce are correct for 90% of development workflows. Only tune when you have a specific reason.
2. **Use `WATCHFILES_FORCE_POLLING=1` in Docker** — The #1 cause of "watching doesn't work" in containerized development.
3. **Lower `debounce` for interactive development** — 300-500ms feels responsive. 1600ms is safe but noticeable.
4. **Use `PythonFilter` for Python projects** — It excludes everything except `.py/.pyx/.pyd`, reducing noise dramatically.
5. **Always use `stop_event`** in production code — Gives you clean shutdown control instead of relying on process termination.
6. **Set `grace_period`** when your process generates files at startup — Prevents restart loops.
7. **Install `uvicorn[standard]`**, not just `uvicorn` — This pulls in watchfiles for the fast reloader.
8. **Increase inotify limits proactively on Linux** — Don't wait for the error. Add `fs.inotify.max_user_watches=524288` to `/etc/sysctl.conf` on development machines.
9. **Use the CLI for quick tasks** — `watchfiles 'pytest --lf' src` is faster than writing a script for one-off test watching.
10. **Don't confuse with watchdog** — If you search for "python watch files" you'll find both. `watchfiles` = generator API + Rust core. `watchdog` = observer pattern + C extensions. Choose based on your API preference.

---

*Guide version: 1.0 — March 2026*
*Covers watchfiles v1.1.x*
*Sources: Official documentation (watchfiles.helpmanual.io), GitHub repository, PyPI, and cited references.*
