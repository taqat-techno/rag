# Research Report: Python `watchfiles` Library

## Metadata
- **Date**: 2026-03-31
- **Research ID**: RES-2026-0331-WATCHFILES
- **Domain**: Technical (Python Libraries / File System Monitoring)
- **Status**: Complete
- **Confidence**: High
- **Library Version at Time of Research**: v1.1.1

## Executive Summary

`watchfiles` is a high-performance Python library for file system change detection and code reloading, created by Samuel Colvin (author of Pydantic). It is built on a Rust core using the `notify` crate via PyO3 bindings, providing native OS-level file watching (inotify on Linux, FSEvents on macOS, ReadDirectoryChangesW on Windows) with an automatic polling fallback. The library serves as the default reloader for Uvicorn/FastAPI and is used by over 173,000 projects. It was renamed from `watchgod` in 2022 to avoid confusion with the similarly-named `watchdog` library.

## Research Question

Comprehensive technical analysis of the `watchfiles` library covering architecture, API surface, configuration, platform behavior, integration patterns, performance characteristics, comparisons with alternatives, and version history.

---

## 1. What watchfiles Is

### Overview

`watchfiles` provides "simple, modern and high performance file watching and code reload in Python" [1]. The underlying file system notifications are delegated to the Rust-based `notify` crate, making it significantly faster and more CPU-efficient than pure-Python polling alternatives.

### Key Facts

| Attribute | Value |
|-----------|-------|
| **Current Version** | v1.1.1 (October 14, 2025) |
| **License** | MIT |
| **Maintainer** | Samuel Colvin (s@muelcolvin.com) |
| **GitHub Stars** | ~2,500 |
| **GitHub Forks** | ~133 |
| **Dependents** | 173,000+ projects |
| **Python Support** | 3.9 through 3.14 (including free-threading 3.14t) |
| **Language Composition** | Python 86.1%, Rust 12.5%, Makefile 1.4% |
| **Repository** | https://github.com/samuelcolvin/watchfiles |
| **Documentation** | https://watchfiles.helpmanual.io/ |
| **PyPI** | https://pypi.org/project/watchfiles/ |

### Relationship to `watchgod` (Predecessor)

The library was previously named `watchgod`. It was "significantly rewritten and renamed from `watchgod` to `watchfiles`" to avoid confusion with the similarly named `watchdog` package [3]. The core architectural change was switching from file scanning/polling to native OS file system notifications via the Rust `notify` library. The original `watchgod` PyPI package remains available but may receive deprecation warnings in the future [3].

### Installation

```bash
# From PyPI (recommended)
pip install watchfiles

# From conda-forge
mamba install -c conda-forge watchfiles

# From source (requires Rust stable compiler)
pip install watchfiles --no-binary watchfiles
```

Pre-built binary wheels are available for Linux (x86_64, aarch64, i686, ppc64le, s390x), macOS (x86_64, arm64), and Windows (x86_64, i686) [1][2].

---

## 2. Architecture

### Internal Design

The architecture follows a layered model:

```
Python API Layer (watch, awatch, run_process, arun_process)
        |
        v
  PyO3 Bindings (RustNotify class)
        |
        v
  Rust notify crate (v8.0.0 as of v1.1.0)
        |
        v
  OS-Specific Backend
    - Linux: inotify
    - macOS: FSEvents
    - Windows: ReadDirectoryChangesW
    - Fallback: Polling
```

### Key Architectural Decisions

1. **Rust Core via PyO3**: All file system integration, event collection, and debouncing happen in Rust. Python only handles filtering and presentation [4].

2. **Threading Model**: The Rust code creates a new thread to watch for file changes. Synchronous Python methods (like `watch()`) require no Python-side threading logic. The GIL is released during `step_ms` sleep on each iteration to avoid blocking other Python threads [4].

3. **Async Support**: Asynchronous methods (`awatch`, `arun_process`) use `anyio.to_thread.run_sync` to delegate blocking Rust calls to a thread, making them compatible with both `asyncio` and `trio` [1].

4. **Debouncing in Rust**: Change debouncing (grouping rapid changes into batches) occurs at the Rust level, not in Python. Once a change is detected, the watcher groups changes and returns within `debounce_ms` milliseconds [4].

### RustNotify Class (Rust Backend)

The `RustNotify` class is the direct Python interface to the Rust `notify` crate [4]:

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

    # Supports context manager protocol
    def __enter__(self) -> 'RustNotify': ...
    def __exit__(self, *args: Any) -> None: ...
```

**Return values from `watch()`**:
- A `set` of `(event_type: int, path: str)` tuples when changes are detected
- `'signal'` when a system signal is received
- `'stop'` when the stop_event is triggered
- `'timeout'` when timeout_ms elapses with no changes

The watching thread is created during `__init__`, not on `__enter__`. The `close()` method must be called to terminate the thread and prevent resource leaks [4].

**Exception**: `WatchfilesRustInternalError(RuntimeError)` is raised for unknown internal errors [5].

---

## 3. Core API

### Change Enum

```python
class Change(IntEnum):
    added = 1     # New file or directory created
    modified = 2  # File or directory content altered
    deleted = 3   # File or directory removed
```

### FileChange Type

```python
FileChange = Tuple[Change, str]  # (change_type, absolute_path)
```

### `watch()` -- Synchronous Blocking Generator

```python
def watch(
    *paths: Union[Path, str],
    watch_filter: Optional[Callable[[Change, str], bool]] = DefaultFilter(),
    debounce: int = 1600,
    step: int = 50,
    stop_event: Optional[threading.Event] = None,
    rust_timeout: int = 5000,
    yield_on_timeout: bool = False,
    debug: Optional[bool] = None,
    raise_interrupt: bool = True,
    force_polling: Optional[bool] = None,
    poll_delay_ms: int = 300,
    recursive: bool = True,
    ignore_permission_denied: Optional[bool] = None,
) -> Generator[Set[FileChange], None, None]:
```

**Usage**:
```python
from watchfiles import watch

for changes in watch('./src', './tests', raise_interrupt=False):
    print(changes)
    # {(Change.modified, '/abs/path/to/file.py'), ...}
```

### `awatch()` -- Async Generator

```python
async def awatch(
    *paths: Union[Path, str],
    watch_filter: Optional[Callable[[Change, str], bool]] = DefaultFilter(),
    debounce: int = 1600,
    step: int = 50,
    stop_event: Optional[anyio.Event] = None,
    rust_timeout: Optional[int] = None,
    yield_on_timeout: bool = False,
    debug: Optional[bool] = None,
    raise_interrupt: Optional[bool] = None,
    force_polling: Optional[bool] = None,
    poll_delay_ms: int = 300,
    recursive: bool = True,
    ignore_permission_denied: Optional[bool] = None,
) -> AsyncGenerator[Set[FileChange], None]:
```

**Usage**:
```python
import asyncio
from watchfiles import awatch

async def main():
    async for changes in awatch('./src'):
        print(changes)

asyncio.run(main())
```

### `run_process()` -- Synchronous Process Runner

```python
def run_process(
    *paths: Union[Path, str],
    target: Union[str, Callable],
    args: tuple = (),
    kwargs: Optional[dict] = None,
    target_type: str = 'auto',
    callback: Optional[Callable] = None,
    watch_filter: Optional[Callable] = DefaultFilter(),
    grace_period: float = 0,
    debounce: int = 1600,
    step: int = 50,
    debug: Optional[bool] = None,
    sigint_timeout: int = 5,
    sigkill_timeout: int = 1,
    recursive: bool = True,
    ignore_permission_denied: bool = False,
) -> int:
```

Runs a function (via `multiprocessing.Process`) or command (via `subprocess.Popen`) and restarts it when watched files change. Returns the number of times the process was reloaded [6].

### `arun_process()` -- Async Process Runner

Same parameters as `run_process()` but runs all operations in a separate thread. The `callback` parameter can accept a coroutine function. Cannot suppress `KeyboardInterrupt` within the async function [6].

### `detect_target_type()`

Utility function that determines whether a target is a Python function or a shell command using regex pattern matching and file extension analysis [6].

---

## 4. Parameters and Configuration (Detailed)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `*paths` | `Path \| str` | (required) | One or more filesystem paths to monitor |
| `watch_filter` | `Callable[[Change, str], bool] \| None` | `DefaultFilter()` | Filter function; return `True` to include change, `False` to exclude. Pass `None` to disable filtering |
| `debounce` | `int` | `1600` | Milliseconds to wait after first change before yielding batch. Groups rapid changes together |
| `step` | `int` | `50` | Polling interval in ms for the Rust watcher loop. GIL is released during sleep |
| `stop_event` | `threading.Event \| asyncio.Event` | `None` | External signal to stop watching |
| `rust_timeout` | `int` | `5000` | Timeout in ms for the Rust watcher. 0 = wait indefinitely |
| `yield_on_timeout` | `bool` | `False` | If `True`, yields empty set when timeout expires with no changes |
| `debug` | `bool \| None` | `None` | When `True`, outputs raw events to stderr |
| `raise_interrupt` | `bool` | `True` (`watch`) / `None` (`awatch`) | Whether to re-raise `KeyboardInterrupt` or handle silently |
| `force_polling` | `bool \| None` | `None` | Force polling mode instead of native OS events. Also settable via `WATCHFILES_FORCE_POLLING` env var |
| `poll_delay_ms` | `int` | `300` | Delay between polls when using polling mode. Also settable via env var (since v1.0.0) |
| `recursive` | `bool` | `True` | Watch subdirectories recursively |
| `ignore_permission_denied` | `bool \| None` | `None` | Suppress permission errors when accessing files |

### Additional `run_process` / `arun_process` Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `target` | `str \| Callable` | (required) | Function or command to run |
| `args` | `tuple` | `()` | Positional arguments for function targets |
| `kwargs` | `dict \| None` | `None` | Keyword arguments for function targets |
| `target_type` | `str` | `'auto'` | `'auto'`, `'function'`, or `'command'` |
| `callback` | `Callable \| None` | `None` | Called on each reload with the set of changes |
| `grace_period` | `float` | `0` | Seconds to wait after process starts before monitoring begins |
| `sigint_timeout` | `int` | `5` | Seconds to wait after SIGINT before sending SIGKILL |
| `sigkill_timeout` | `int` | `1` | Seconds to wait after SIGKILL before raising timeout exception |

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `WATCHFILES_FORCE_POLLING` | Force polling mode when set (any truthy value) |
| `WATCHFILES_POLL_DELAY_MS` | Override `poll_delay_ms` (added in v1.0.0) |

---

## 5. Filtering System

### BaseFilter

The foundation class for all filters. Provides three ignore mechanisms [7]:

- **`ignore_dirs`**: Set of directory names to ignore (e.g., `{'.git', 'node_modules'}`)
- **`ignore_entity_patterns`**: Tuple of regex patterns for files/directories to ignore (compiled to regexes)
- **`ignore_paths`**: Tuple of absolute paths to ignore

The `__call__(self, change: Change, path: str) -> bool` method returns `True` to include a change, `False` to exclude it.

### DefaultFilter

Extends `BaseFilter` with sensible defaults for development workflows [7]:

**Default `ignore_dirs`**:
```python
{
    '__pycache__', '.git', '.hg', '.svn', '.tox', '.venv',
    'site-packages', '.idea', 'node_modules', '.mypy_cache',
    '.pytest_cache', '.hypothesis'
}
```

**Default `ignore_entity_patterns`**: Matches compiled bytecode (`.pyc`, `.pyo`, `.pyd`), backup files (`~`), editor temp files (`.swp`, `.swx`), and system files (`.DS_Store`).

### PythonFilter

Extends `DefaultFilter` to only watch Python files [7]:

- Watches extensions: `('.py', '.pyx', '.pyd')`
- Accepts `extra_extensions` parameter to add more file types
- Accepts `ignore_paths` for custom exclusions

### Custom Filters

**Class-based** (extend `DefaultFilter`):
```python
class WebFilter(DefaultFilter):
    allowed_extensions = '.html', '.css', '.js'

    def __call__(self, change: Change, path: str) -> bool:
        return super().__call__(change, path) and path.endswith(self.allowed_extensions)
```

**Function-based** (simple callable):
```python
def only_added(change: Change, path: str) -> bool:
    return change == Change.added
```

**Disabling all filtering**:
```python
for changes in watch('.', watch_filter=None):
    print(changes)
```

---

## 6. CLI Usage

The CLI can be invoked as `watchfiles` or `python -m watchfiles` [8]:

```
watchfiles [options] target [paths]
```

### Core Arguments

- **`target`**: Command string or dotted Python function path (e.g., `'pytest --lf'` or `myapp.main`)
- **`paths`**: Filesystem paths to monitor (defaults to current directory)

### Options

| Option | Description |
|--------|-------------|
| `--filter [FILTER]` | `"default"`, `"python"`, `"all"`, or dotted path to custom filter class |
| `--ignore-paths` | Comma-separated directories to exclude |
| `--target-type` | `"command"`, `"function"`, or `"auto"` (default) |
| `--args` | Arguments passed to sys.argv when calling target function |
| `--non-recursive` | Disable watching subdirectories |
| `--verbosity` | Log level: `"warning"`, `"info"`, or `"debug"` |
| `--verbose` | Shortcut for `--verbosity debug` |
| `--sigint-timeout` | Seconds before SIGKILL after SIGINT |
| `--sigkill-timeout` | Seconds before timeout exception after SIGKILL |
| `--grace-period` | Seconds to wait after process starts before watching |
| `--ignore-permission-denied` | Suppress permission errors |
| `--version, -V` | Display version number |

### Examples

```bash
# Run pytest on file changes
watchfiles 'pytest --lf'

# Run a Python function
watchfiles myapp.main

# Watch specific dirs with Python filter
watchfiles --filter python 'pytest --lf' src tests

# Non-recursive watching
watchfiles --non-recursive myapp.main ./src
```

---

## 7. Integration Patterns

### With Uvicorn / FastAPI

Uvicorn uses `watchfiles` as its default file reloader when installed. When running with `--reload`, Uvicorn checks for `watchfiles` at runtime [9]:

```bash
# Install with watchfiles support
pip install uvicorn[standard]

# Run with auto-reload
uvicorn myapp:app --reload
```

If `watchfiles` is not installed, Uvicorn falls back to polling-based file modification time checking. With `watchfiles` installed, additional options become available:
- `--reload-include`: Glob patterns for files to watch
- `--reload-exclude`: Glob patterns for files to exclude

FastAPI's `fastapi dev` command uses Uvicorn with auto-reload enabled by default [9].

### With Django

The `django-watchfiles` package by Adam Johnson replaces Django's default polling-based `StatReloader` with a `WatchfilesReloader` [10]:

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

**Performance benchmark** (385,000 lines of code, 206 installed packages, M1 MacBook) [10]:
- Default Django `StatReloader`: ~10% CPU every other second
- `django-watchfiles`: ~0% CPU usage
- Default reloader detection: 1+ seconds
- `watchfiles` detection: as low as 50 milliseconds

Supported versions: Python 3.10-3.14, Django 4.2-6.0 [10].

### With pytest

While `watchfiles` does not have direct pytest integration, it can be used from the CLI:

```bash
# Re-run tests on file changes
watchfiles 'pytest --lf' src tests

# With Python-only filter
watchfiles --filter python 'pytest -x' .
```

### With asyncio Applications

```python
import asyncio
from watchfiles import awatch, Change

async def monitor_config():
    async for changes in awatch('./config'):
        for change_type, path in changes:
            if change_type == Change.modified and path.endswith('.yaml'):
                print(f"Config changed: {path}")
                await reload_config(path)

async def main():
    await asyncio.gather(
        monitor_config(),
        run_server(),
    )
```

### With Custom Build Tools

```python
from watchfiles import run_process

# Rebuild on changes (command mode)
run_process(
    './src',
    target='npm run build',
    watch_filter=lambda c, p: p.endswith(('.ts', '.tsx', '.css')),
)

# Function mode with callback
def on_change(changes):
    print(f"Rebuilding due to: {changes}")

run_process(
    './src',
    target=build_project,
    callback=on_change,
    debounce=2000,
)
```

---

## 8. Platform Differences

### Linux: inotify

- Uses kernel-level `inotify` for file system event notification
- **Watch limit**: Default `fs.inotify.max_user_watches` is 8192 on many distributions (Ubuntu default), which can be insufficient for large projects [11]
- **Fix**: Increase limit via `sysctl -w fs.inotify.max_user_watches=524288` or persist in `/etc/sysctl.conf`
- **Instance limit**: `fs.inotify.max_user_instances` defaults to 128 [11]

### macOS: FSEvents

- Uses Apple's FSEvents framework
- Generally reliable with no practical watch limits
- File renaming events are properly tracked (fixed in v0.14) [14]

### Windows: ReadDirectoryChangesW

- Uses Windows API `ReadDirectoryChangesW`
- Command splitting behavior differs from Unix (fixed in v0.18.1) [14]
- File modification handling has platform-specific quirks (improved in v0.11) [14]
- Python 3.14 Windows builds added in v1.1.1 [14]

### WSL2 (Windows Subsystem for Linux)

- **Forced polling since v0.18.0**: watchfiles automatically forces polling mode on WSL because inotify does not reliably propagate events across the WSL/Windows filesystem boundary [14]
- Default polling delay increased to 300ms in v0.18.0 [14]

### Docker / Container Mounts

- **Problem**: Docker volumes using bind mounts from the host do not propagate inotify events to the container [11][12]
- **Solution**: Use `force_polling=True` or set `WATCHFILES_FORCE_POLLING=1` environment variable [12]
- **Performance trade-off**: Polling is less efficient but works reliably across all mount types

### Network Filesystems (NFS, CIFS, SMB)

- Native file system events are not propagated over network filesystems
- Must use polling mode (`force_polling=True`) for reliable operation
- `poll_delay_ms` can be tuned to balance responsiveness vs. I/O load

---

## 9. Performance Characteristics

### CPU Efficiency

The Rust-based implementation provides near-zero CPU overhead when idle, as it relies on OS-level event notification rather than polling [1][10]:

| Scenario | CPU Usage |
|----------|-----------|
| watchfiles (native events, idle) | ~0% |
| watchfiles (polling mode, idle) | Low, depends on `poll_delay_ms` |
| Python polling alternatives (idle) | 5-10% periodic spikes |
| Django StatReloader | ~10% every other second |
| django-watchfiles | ~0% |

### Detection Latency

- **Native events**: Changes detected within the `step` interval (default 50ms)
- **Debounce**: Changes are batched for `debounce` ms (default 1600ms) before being yielded
- **Effective latency**: Between 50ms (single change after quiet period) and 1650ms (change during debounce window)
- **Polling mode**: Additional delay of `poll_delay_ms` (default 300ms)

### Memory Usage

- The Rust watcher thread has minimal memory overhead
- Memory scales with the number of watched directories (inotify) rather than files
- No Python objects are created for intermediate events (they are accumulated in Rust)

### Debounce Behavior

The debounce mechanism works at the Rust level:
1. First change detected -> start debounce timer
2. Subsequent changes within the debounce window are accumulated
3. After `debounce_ms` elapses with no new changes, the batch is yielded
4. This prevents rapid successive restarts during multi-file saves (e.g., IDE "save all")

---

## 10. Comparison with Alternatives

### watchfiles vs. watchdog

| Aspect | watchfiles | watchdog |
|--------|------------|----------|
| **Language** | Python + Rust (notify crate) | Pure Python + C extensions |
| **Async Support** | Native (`awatch`, `arun_process`) | No native async |
| **API Style** | Generator/async generator | Observer pattern (event handlers) |
| **Default Behavior** | Debounced batch yields | Individual event callbacks |
| **CLI Tool** | Built-in | Separate (`watchmedo`) |
| **Backend** | notify crate (Rust) | Platform-specific C extensions |
| **PyPI Downloads** | High (Uvicorn dependency) | Very high (mature ecosystem) |
| **Filtering** | Filter classes/callables | Event handler + patterns |
| **Process Restart** | Built-in (`run_process`) | Via `watchmedo` CLI |
| **Framework** | AnyIO (asyncio + trio) | Threading |
| **CPU Usage** | Near-zero (native events) | Low (native events) |
| **Maturity** | Newer (2022 rewrite) | Mature (since 2010) |

**When to use watchfiles**: Async applications, Uvicorn/FastAPI projects, when CPU efficiency matters, when you want a simpler API with debouncing built in.

**When to use watchdog**: When you need the observer pattern, broad community support, or integration with tools that depend on it.

### watchfiles vs. inotify / pyinotify

| Aspect | watchfiles | pyinotify |
|--------|------------|-----------|
| **Cross-platform** | Yes (Linux, macOS, Windows) | Linux only |
| **API Level** | High-level Python | Low-level inotify wrapper |
| **Maintenance** | Active | Largely unmaintained |
| **Polling fallback** | Automatic | No |

### watchfiles vs. fswatch

| Aspect | watchfiles | fswatch |
|--------|------------|--------|
| **Language** | Python library | C++ CLI tool |
| **Integration** | Native Python API | Subprocess/pipe |
| **Cross-platform** | Yes | Yes |
| **Use case** | Python applications | Shell scripts, any language |

---

## 11. Limitations and Trade-offs

### Platform-Specific Behavior
- Events may differ slightly between platforms (e.g., a rename might appear as delete+add on some systems)
- WSL2 automatically falls back to polling, increasing latency and CPU usage
- File renaming tracking was fixed on macOS in v0.14 and on Linux/Windows in v0.13

### Network Filesystem Issues
- Native events do not work on NFS, CIFS, SMB, or other network-mounted filesystems
- Docker bind mounts between host and container do not propagate inotify events
- Must use `force_polling=True` for these scenarios, at the cost of increased CPU and latency

### inotify Watch Limits (Linux)
- Default limit of 8192 watches may be insufficient for large projects
- Each watched directory consumes one inotify watch
- Exceeding the limit results in `OSError: inotify watch limit reached`
- Requires system-level configuration change to increase

### Debounce Implications
- The default 1600ms debounce means changes are not yielded immediately
- For real-time applications, this delay may be unacceptable (reduce `debounce` parameter)
- Very low debounce values may cause excessive process restarts

### Binary Distribution
- Pre-built wheels cover most common platforms
- Uncommon architectures require Rust compiler for source builds
- The Rust dependency makes the build process more complex than pure-Python alternatives

### KeyboardInterrupt Handling
- `awatch` and `arun_process` cannot suppress `KeyboardInterrupt` within the async function (changed in v0.14)
- Requires explicit handling in calling code

---

## 12. Version History

### Major Releases (Chronological)

| Version | Date | Key Changes |
|---------|------|-------------|
| **v0.10** | 2022-03-23 | Complete rewrite using Rust notify; renamed from `watchgod` to `watchfiles` |
| **v0.11** | 2022-03-30 | Restructured CLI and process functions; Windows file handling fixes |
| **v0.12** | 2022-04-01 | Added `stop_event` parameter to `watch()` |
| **v0.13** | 2022-04-08 | Added `rust_timeout` and `yield_on_timeout`; fixed file rename tracking (Linux/Windows) |
| **v0.14** | 2022-05-16 | **Breaking**: `awatch`/`arun_process` no longer suppress `KeyboardInterrupt`; fixed macOS rename; added `force_polling`; aarch64 Linux binaries |
| **v0.15.0** | 2022-06-17 | Switched from setuptools-rust to maturin; exposed kill timeouts |
| **v0.16.0** | 2022-07-21 | RustNotify as context manager; `WATCHFILES_FORCE_POLLING` env var; PyPy wheels |
| **v0.17.0** | 2022-09-11 | Upgraded notify to 5.0.0; added `recursive` option |
| **v0.18.0** | 2022-10-19 | **Forced polling on WSL**; increased default poll delay to 300ms; Python 3.11 support |
| **v0.18.1** | 2022-11-07 | Fixed Windows command splitting; relaxed anyio constraint |
| **v0.19.0** | 2023-03-27 | Switched to ruff linter; PyO3 0.18.2; ppc64le/s390x wheels |
| **v0.20.0** | 2023-08-24 | **Fixed memory leak** in PyO3; added `grace_period` and `ignore_permission_denied`; SIGTERM handling |
| **v0.21.0** | 2023-10-13 | Python 3.12 support; dropped Python 3.7 |
| **v0.22.0** | 2024-05-27 | Updated PyO3; removed Black/isort (ruff only); dropped Python <=3.7 |
| **v0.23.0** | 2024-08-07 | Python 3.13 support; PyO3 0.22.2 |
| **v0.24.0** | 2024-08-28 | Dropped PyPy 3.8; returns "file deleted" instead of raising exceptions |
| **v1.0.0** | 2024-11-25 | **Stable release**: PyO3 0.23; dropped Python 3.8; `poll_delay_ms` configurable via env var |
| **v1.0.4** | 2025-01-10 | **Fixed data loss** via proper locking; notify 7.0.0; uv build tooling; Python 3.13 free-threading testing |
| **v1.0.5** | 2025-04-08 | PyO3 0.24.1; switched to uv publish |
| **v1.1.0** | 2025-06-15 | **notify 8.0.0**; PyO3 0.25.1; Python 3.14 and 3.14t builds |
| **v1.1.1** | 2025-10-14 | Python 3.14 Windows builds |

### Migration from watchgod

Key breaking changes when migrating from `watchgod` to `watchfiles` [3]:
1. `watcher_cls` parameter replaced with `watch_filter` (simple callable)
2. `target` in `run_process`/`arun_process` is now keyword-only (paths are positional)
3. All keyword arguments refined and thoroughly documented
4. Core watching mechanism changed from polling to OS notifications

---

## Methodology

### Search Queries Used
- "watchfiles python library architecture Rust notify crate PyO3 internals"
- "watchfiles vs watchdog python comparison performance benchmark"
- "watchfiles python limitations Docker WSL2 network filesystem NFS inotify"
- "uvicorn watchfiles reloader FastAPI Django integration"
- "watchfiles changelog releases version history major changes"
- "django-watchfiles package Adam Johnson Django runserver"
- "watchfiles force_polling Docker mounted volumes workaround"

### Sources Consulted
- Official documentation (watchfiles.helpmanual.io)
- GitHub repository (samuelcolvin/watchfiles) including releases, issues, and source code
- PyPI package page
- Adam Johnson's blog on django-watchfiles
- Docker community forums
- Various comparison articles

### Evaluation Criteria
- Prioritized official documentation and source code
- Cross-referenced performance claims with benchmarks
- Verified version history against GitHub releases
- Checked platform behavior against issue reports

---

## Confidence Assessment

### High Confidence
- API signatures, parameters, and defaults (from official docs and source)
- Architecture and Rust/PyO3 integration model
- Version history and release dates (from GitHub releases)
- Platform-specific backend selection (inotify, FSEvents, ReadDirectoryChangesW)
- Django/Uvicorn integration patterns
- Filtering system and default exclusions

### Medium Confidence
- Exact memory usage patterns (no formal benchmarks found)
- Comprehensive comparison metrics with watchdog (limited head-to-head benchmarks)
- Detection latency ranges (derived from parameter defaults, not measured)

### Low Confidence
- Internal Rust notify crate behavior details (would require Rust source analysis)
- Exact behavior on uncommon platforms (FreeBSD, etc.)

### Knowledge Gaps
- No formal benchmark suite comparing watchfiles vs. watchdog under identical conditions
- Limited data on behavior at extreme scale (millions of files)
- No information on planned features or roadmap beyond current release

---

## Sources and References

[1] [watchfiles Official Documentation](https://watchfiles.helpmanual.io/) - [Official]
[2] [watchfiles on PyPI](https://pypi.org/project/watchfiles/) - [Official]
[3] [Migration from watchgod](https://watchfiles.helpmanual.io/migrating/) - [Official]
[4] [Rust Backend Direct Usage](https://watchfiles.helpmanual.io/api/rust_backend/) - [Official]
[5] [RustNotify Type Stubs (GitHub)](https://github.com/samuelcolvin/watchfiles/blob/main/watchfiles/_rust_notify.pyi) - [Official]
[6] [run_process API Documentation](https://watchfiles.helpmanual.io/api/run_process/) - [Official]
[7] [Filters API Documentation](https://watchfiles.helpmanual.io/api/filters/) - [Official]
[8] [CLI Documentation](https://watchfiles.helpmanual.io/cli/) - [Official]
[9] [Uvicorn Settings](https://uvicorn.dev/settings/) - [Official]
[10] [Adam Johnson - Introducing django-watchfiles](https://adamj.eu/tech/2025/09/22/introducing-django-watchfiles/) - [Industry]
[11] [WSL2 inotify Limits (GitHub Issue)](https://github.com/microsoft/WSL/issues/4293) - [Community]
[12] [Docker Mounted Volumes File Watching](https://forums.docker.com/t/file-system-watch-does-not-work-with-mounted-volumes/12038) - [Community]
[13] [watchfiles GitHub Repository](https://github.com/samuelcolvin/watchfiles) - [Official]
[14] [watchfiles GitHub Releases](https://github.com/samuelcolvin/watchfiles/releases) - [Official]
[15] [watchdog vs watchfiles (PipTrends)](https://piptrends.com/compare/watchdog-vs-watchfiles) - [Community]
[16] [django-watchfiles GitHub](https://github.com/adamchainz/django-watchfiles) - [Industry]

---

## Recommendations

1. **For Uvicorn/FastAPI projects**: watchfiles is already the default reloader; ensure it is installed via `pip install uvicorn[standard]`.

2. **For Django projects**: Use `django-watchfiles` for significant CPU savings and faster reload detection.

3. **For Docker development**: Set `WATCHFILES_FORCE_POLLING=1` in your Docker environment to ensure reliable file change detection on bind mounts.

4. **For large Linux projects**: Increase `fs.inotify.max_user_watches` to avoid hitting watch limits.

5. **For low-latency needs**: Reduce `debounce` from the default 1600ms, but be aware of potential rapid restart issues.

6. **For WSL2 development**: watchfiles automatically falls back to polling since v0.18.0; no manual configuration needed, but expect slightly higher CPU usage and latency.

## Further Research Needed

- Formal head-to-head performance benchmarks (watchfiles vs. watchdog) under controlled conditions
- Behavior at extreme scale (millions of files, thousands of directories)
- Integration with other frameworks (Flask, Starlette standalone, Celery)
- Impact of free-threading Python (3.14t) on watchfiles performance
- Comparison with newer alternatives that may emerge in the Rust-Python ecosystem

---
*Report generated by Research Agent*
*File location: c:/MY-WorkSpace/rag/researches/2026-03-31_python-watchfiles-library.md*
