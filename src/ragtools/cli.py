"""CLI entry point for RAG Tools."""

import shutil
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="rag",
    help="Local Markdown RAG system for Claude CLI.",
    no_args_is_help=True,
)
ignore_app = typer.Typer(help="Manage ignore rules.")
app.add_typer(ignore_app, name="ignore")
service_app = typer.Typer(help="Manage the RAG service.")
app.add_typer(service_app, name="service")
project_app = typer.Typer(help="Manage configured projects.")
app.add_typer(project_app, name="project")
backup_app = typer.Typer(help="Manage state-DB backups (taken before destructive operations).")
app.add_typer(backup_app, name="backup")
tray_app = typer.Typer(
    help="Manage the RAG Tools system-tray icon.",
    invoke_without_command=True,
)
app.add_typer(tray_app, name="tray")
wiki_app = typer.Typer(help="Wiki sync and maintenance.")
app.add_typer(wiki_app, name="wiki")
console = Console()


def _get_settings():
    """Load settings."""
    from ragtools.config import Settings
    return Settings()


def _get_ignore_rules(settings, content_root=None):
    """Build IgnoreRules from settings."""
    from ragtools.ignore import IgnoreRules
    return IgnoreRules(
        content_root=content_root or settings.content_root,
        global_patterns=settings.ignore_patterns,
        use_ragignore=settings.use_ragignore_files,
    )


def _probe_service(settings=None) -> bool:
    """Check if the service is running and healthy."""
    try:
        import httpx
    except ImportError:
        return False
    if settings is None:
        settings = _get_settings()
    try:
        r = httpx.get(
            f"http://{settings.service_host}:{settings.service_port}/health",
            timeout=1.0,
        )
        return r.status_code == 200
    except Exception:
        return False


def _service_url(settings=None) -> str:
    """Build the service base URL."""
    if settings is None:
        settings = _get_settings()
    return f"http://{settings.service_host}:{settings.service_port}"


# --- Core Commands ---


@app.command()
def index(
    full: bool = typer.Option(False, "--full", help="Force full re-index (ignore state)"),
    project: str = typer.Option(None, "--project", "-p", help="Index only this project"),
):
    """Index all configured projects. Incremental by default, skips unchanged files."""
    settings = _get_settings()
    start = time.time()

    if _probe_service(settings):
        import httpx
        try:
            r = httpx.post(
                f"{_service_url(settings)}/api/index",
                json={"project": project, "full": full},
                timeout=300.0,
            )
            r.raise_for_status()
            stats = r.json()["stats"]
            elapsed = time.time() - start
            _print_index_stats(stats, full, elapsed)
        except Exception as e:
            console.print(f"[red]Indexing via service failed:[/red] {e}")
            raise typer.Exit(1)
    else:
        console.print("[yellow]Service is not running.[/yellow] Start with: [bold]rag service start[/bold]")
        raise typer.Exit(1)


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    project: str = typer.Option(None, "--project", "-p", help="Filter to project"),
    top_k: int = typer.Option(10, "--top-k", "-k", help="Number of results"),
):
    """Search the knowledge base."""
    settings = _get_settings()

    if _probe_service(settings):
        import httpx
        try:
            r = httpx.get(
                f"{_service_url(settings)}/api/search",
                params={"query": query, "project": project, "top_k": top_k},
                timeout=10.0,
            )
            r.raise_for_status()
            data = r.json()
            if data["count"] == 0:
                console.print(f"[yellow]No results found for:[/yellow] '{query}'")
                return
            console.print(f"\n[bold]Results for:[/bold] '{query}'\n")
            for i, result in enumerate(data["results"], 1):
                heading_str = " > ".join(result["headings"]) if result["headings"] else "N/A"
                console.print(f"[{i}] ({result['score']:.3f}) {result['project_id']}/{result['file_path']} | {heading_str}")
                text = result["text"]
                console.print(f"    {text[:200]}{'...' if len(text) > 200 else ''}")
                console.print()
        except Exception as e:
            console.print(f"[red]Search via service failed:[/red] {e}")
            raise typer.Exit(1)
    else:
        # Direct mode
        try:
            from ragtools.embedding.encoder import Encoder
            from ragtools.retrieval.formatter import format_context_brief
            from ragtools.retrieval.searcher import Searcher

            client = settings.get_qdrant_client()
            encoder = Encoder(settings.embedding_model)
            searcher = Searcher(client=client, encoder=encoder, settings=settings)

            results = searcher.search(query=query, project_id=project, top_k=top_k)
            if not results:
                console.print(f"[yellow]No results found for:[/yellow] '{query}'")
                raise typer.Exit(0)

            output = format_context_brief(results, query)
            console.print(f"\n[bold]Results for:[/bold] '{query}'\n")
            console.print(output)
        except typer.Exit:
            raise
        except Exception as e:
            console.print(f"[red]Search failed:[/red] {e}")
            raise typer.Exit(1)


@app.command()
def status():
    """Show indexing status and collection statistics."""
    settings = _get_settings()

    if _probe_service(settings):
        import httpx
        try:
            r = httpx.get(f"{_service_url(settings)}/api/status", timeout=5.0)
            r.raise_for_status()
            data = r.json()
            table = Table(title="Index Status (via service)")
            table.add_column("Metric", style="bold")
            table.add_column("Value")
            table.add_row("Total files", str(data.get("total_files", 0)))
            table.add_row("Total chunks", str(data.get("total_chunks", 0)))
            table.add_row("Points", str(data.get("points_count", 0)))
            table.add_row("Projects", ", ".join(data.get("projects", [])) or "none")
            table.add_row("Last indexed", data.get("last_indexed") or "never")
            console.print(table)
        except Exception as e:
            console.print(f"[red]Status check via service failed:[/red] {e}")
            raise typer.Exit(1)
    else:
        # Direct mode
        state_path = Path(settings.state_db)
        if not state_path.exists():
            console.print("[yellow]No index state found.[/yellow] Run `rag index <path>` first.")
            return

        try:
            from ragtools.indexing.state import IndexState

            state = IndexState(settings.state_db)
            summary = state.get_summary()
            state.close()

            table = Table(title="Index Status")
            table.add_column("Metric", style="bold")
            table.add_column("Value")
            table.add_row("Total files", str(summary["total_files"]))
            table.add_row("Total chunks", str(summary["total_chunks"]))
            table.add_row("Projects", ", ".join(summary["projects"]) or "none")
            table.add_row("Last indexed", summary["last_indexed"] or "never")
            table.add_row("State DB", str(state_path))
            table.add_row("Qdrant path", settings.qdrant_path)
            console.print(table)
        except Exception as e:
            console.print(f"[red]Status check failed:[/red] {e}")
            raise typer.Exit(1)


@app.command()
def doctor():
    """Check system health and dependencies."""
    checks: list[tuple[str, str, str]] = []

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 10):
        checks.append(("Python", "OK", py_ver))
    else:
        checks.append(("Python", "ERROR", f"{py_ver} (need >=3.10)"))

    for pkg in ["qdrant-client", "sentence-transformers", "pydantic-settings", "pathspec", "mcp", "fastapi", "httpx"]:
        try:
            from importlib.metadata import version as pkg_version
            checks.append((pkg, "OK", pkg_version(pkg)))
        except Exception:
            checks.append((pkg, "MISSING", "pip install -e ."))

    # Service status
    settings = _get_settings()
    if _probe_service(settings):
        checks.append(("Service", "RUNNING", f"http://{settings.service_host}:{settings.service_port}"))
    else:
        checks.append(("Service", "NOT RUNNING", "Start with: rag service start"))

    # Data directory
    from ragtools.config import Settings
    data_path = Path(settings.qdrant_path)
    if data_path.exists():
        checks.append(("Data directory", "OK", str(data_path)))
    else:
        checks.append(("Data directory", "NOT CREATED", "Run `rag index <path>` first"))

    state_path = Path(settings.state_db)
    if state_path.exists():
        checks.append(("State DB", "OK", str(state_path)))
    else:
        checks.append(("State DB", "NOT CREATED", "Run `rag index <path>` first"))

    if data_path.exists():
        try:
            client = settings.get_qdrant_client()
            info = client.get_collection(settings.collection_name)
            points_count = info.points_count
            # Surface Qdrant local-mode scale warnings (field-report incident).
            from ragtools.service.owner import compute_scale_warning
            scale = compute_scale_warning(points_count)
            if scale["level"] == "over":
                status_label = "WARNING"
                detail = (
                    f"{points_count:,} points — OVER local-mode limit "
                    f"({scale['hard_limit']:,}). Prune or migrate Qdrant."
                )
            elif scale["level"] == "approaching":
                status_label = "OK"
                detail = (
                    f"{points_count:,} points — approaching local-mode limit "
                    f"({scale['hard_limit']:,}). Review ignore_patterns."
                )
            else:
                status_label = "OK"
                detail = f"{points_count} points"
            checks.append(("Collection", status_label, detail))
        except Exception:
            checks.append(("Collection", "NOT FOUND", f"'{settings.collection_name}' missing"))

    ignore_rules = _get_ignore_rules(settings)
    patterns = ignore_rules.get_all_patterns()
    config_count = len(patterns.get("config", []))
    checks.append(("Ignore rules", "OK", f"{len(patterns['built-in'])} built-in, {config_count} config"))

    # Auto-recovery integrations (Windows only). If these silently fail to
    # register during install, the user only finds out the next time a crash
    # or reboot happens — and then too late. Surfacing both states in
    # `rag doctor` closes that visibility gap (see field report: watchdog
    # script existed on disk but Task Scheduler had no entry).
    if sys.platform == "win32":
        try:
            from ragtools.service.startup import is_task_installed
            if is_task_installed():
                checks.append(("Login startup", "OK", "Registered in Startup folder"))
            else:
                checks.append((
                    "Login startup", "MISSING",
                    "Register with: rag service install",
                ))
        except Exception as e:
            checks.append(("Login startup", "ERROR", str(e)))

        try:
            from ragtools.service.watchdog import is_watchdog_installed, TASK_NAME
            if is_watchdog_installed():
                checks.append(("Watchdog", "OK", f"Task: {TASK_NAME} (every 15 min)"))
            else:
                checks.append((
                    "Watchdog", "MISSING",
                    "Register with: rag service watchdog install",
                ))
        except Exception as e:
            checks.append(("Watchdog", "ERROR", str(e)))

    table = Table(title="RAG System Health Check")
    table.add_column("Component", style="bold")
    table.add_column("Status")
    table.add_column("Details")
    for name, stat, details in checks:
        color = "green" if stat in ("OK", "RUNNING") else "red" if stat in ("MISSING", "ERROR", "NOT FOUND") else "yellow"
        table.add_row(name, f"[{color}]{stat}[/{color}]", details)
    console.print(table)


@app.command()
def rebuild():
    """Drop all data and rebuild index from scratch."""
    settings = _get_settings()

    console.print("[yellow]This will delete all indexed data and rebuild from Markdown source.[/yellow]")
    typer.confirm("Continue?", abort=True)

    if _probe_service(settings):
        import httpx
        try:
            console.print(f"\n[bold]Rebuilding index via service[/bold]")
            r = httpx.post(f"{_service_url(settings)}/api/rebuild", timeout=300.0)
            r.raise_for_status()
            stats = r.json()["stats"]
            _print_index_stats(stats, full=True, elapsed=0)
        except Exception as e:
            console.print(f"[red]Rebuild via service failed:[/red] {e}")
            raise typer.Exit(1)
    else:
        qdrant_path = Path(settings.qdrant_path)
        state_path = Path(settings.state_db)

        if qdrant_path.exists():
            shutil.rmtree(qdrant_path)
            console.print(f"  Deleted {qdrant_path}")
        if state_path.exists():
            state_path.unlink()
            console.print(f"  Deleted {state_path}")

        try:
            from ragtools.indexing.indexer import run_full_index
            ignore_rules = _get_ignore_rules(settings)
            start = time.time()
            console.print(f"\n[bold]Rebuilding index[/bold] from {settings.content_root}")
            stats = run_full_index(settings, ignore_rules=ignore_rules)
            elapsed = time.time() - start
            _print_index_stats(stats, full=True, elapsed=elapsed)
        except Exception as e:
            console.print(f"[red]Rebuild failed:[/red] {e}")
            raise typer.Exit(1)


@app.command()
def projects():
    """List indexed projects with file and chunk counts."""
    settings = _get_settings()

    if _probe_service(settings):
        import httpx
        try:
            r = httpx.get(f"{_service_url(settings)}/api/projects", timeout=5.0)
            r.raise_for_status()
            data = r.json()["projects"]
            if not data:
                console.print("[yellow]No projects indexed yet.[/yellow]")
                return
            table = Table(title="Indexed Projects (via service)")
            table.add_column("Project", style="bold")
            table.add_column("Files", justify="right")
            table.add_column("Chunks", justify="right")
            total_files = total_chunks = 0
            for p in data:
                table.add_row(p["project_id"], str(p["files"]), str(p["chunks"]))
                total_files += p["files"]
                total_chunks += p["chunks"]
            table.add_section()
            table.add_row("[bold]Total[/bold]", str(total_files), str(total_chunks))
            console.print(table)
        except Exception as e:
            console.print(f"[red]Projects via service failed:[/red] {e}")
            raise typer.Exit(1)
    else:
        state_path = Path(settings.state_db)
        if not state_path.exists():
            console.print("[yellow]No index state found.[/yellow] Run `rag index <path>` first.")
            return

        try:
            from ragtools.indexing.state import IndexState
            state = IndexState(settings.state_db)
            summary = state.get_summary()
            if not summary["projects"]:
                console.print("[yellow]No projects indexed yet.[/yellow]")
                state.close()
                return

            table = Table(title="Indexed Projects")
            table.add_column("Project", style="bold")
            table.add_column("Files", justify="right")
            table.add_column("Chunks", justify="right")
            for pid in summary["projects"]:
                records = state.get_all_for_project(pid)
                table.add_row(pid, str(len(records)), str(sum(r["chunk_count"] for r in records)))
            table.add_section()
            table.add_row("[bold]Total[/bold]", str(summary["total_files"]), str(summary["total_chunks"]))
            state.close()
            console.print(table)
        except Exception as e:
            console.print(f"[red]Failed to list projects:[/red] {e}")
            raise typer.Exit(1)


@app.command()
def watch():
    """Start the file watcher (via the service).

    The watcher auto-starts with the service. This command is a convenience alias.
    """
    settings = _get_settings()
    if _probe_service(settings):
        import httpx
        try:
            r = httpx.post(f"{_service_url(settings)}/api/watcher/start", timeout=5.0)
            r.raise_for_status()
            console.print("[green]Watcher started via service[/green]")
        except Exception as e:
            console.print(f"[red]Failed to start watcher via service:[/red] {e}")
            raise typer.Exit(1)
    else:
        console.print("[yellow]Service is not running.[/yellow] Start with: [bold]rag service start[/bold]")
        raise typer.Exit(1)


@app.command()
def version():
    """Show the RAG Tools version."""
    from ragtools import __version__
    console.print(f"ragtools v{__version__}")


@app.command()
def serve():
    """Start the MCP server for Claude CLI integration.

    Exposes 3 core tools always (search_knowledge_base, list_projects,
    index_status) plus any optional diagnostic tools the user has granted
    access to in the admin panel's MCP Tool Access card.
    """
    from ragtools.integration.mcp_server import main as mcp_main
    err_console = Console(stderr=True)
    err_console.print("[bold]Starting RAG MCP server (stdio transport)...[/bold]")
    err_console.print("Press Ctrl+C to stop.")
    mcp_main()


# --- Service Subcommands ---


@service_app.command("start")
def service_start(
    no_supervise: bool = typer.Option(
        False,
        "--no-supervise",
        help="Launch the service directly without the auto-restart supervisor "
             "(legacy pre-v2.4.3 behavior).",
    ),
):
    """Start the RAG service in the background."""
    from ragtools.service.process import start_service
    settings = _get_settings()
    try:
        pid = start_service(settings, supervise=not no_supervise)
        if no_supervise:
            console.print(f"[green]Service started[/green] (PID {pid}, unsupervised)")
        else:
            console.print(f"[green]Service started under supervisor[/green] (PID {pid})")
            console.print("  Supervisor will auto-restart the service on crash.")
        console.print(f"  Listening on http://{settings.service_host}:{settings.service_port}")
        console.print(f"  Logs: {Path(settings.qdrant_path).parent / 'logs' / 'service.log'}")
        console.print("  Note: encoder loading takes 5-10 seconds before service is ready.")
    except RuntimeError as e:
        console.print(f"[yellow]{e}[/yellow]")
    except Exception as e:
        console.print(f"[red]Failed to start service:[/red] {e}")
        raise typer.Exit(1)


@service_app.command("stop")
def service_stop():
    """Stop the running RAG service."""
    from ragtools.service.process import stop_service
    settings = _get_settings()
    console.print("Stopping service...")
    if stop_service(settings):
        console.print("[green]Service stopped.[/green]")
    else:
        console.print("[yellow]Service was not running.[/yellow]")


@service_app.command("status")
def service_status_cmd():
    """Check if the RAG service is running."""
    from ragtools.service.process import service_status
    settings = _get_settings()
    info = service_status(settings)
    if info["running"]:
        table = Table(title="RAG Service")
        table.add_column("Metric", style="bold")
        table.add_column("Value")
        table.add_row("Status", f"[green]{info.get('status', 'running')}[/green]")
        table.add_row("PID", str(info.get("pid", "unknown")))
        table.add_row("Port", str(info.get("port", "")))
        table.add_row("Host", str(info.get("host", "")))
        console.print(table)
    else:
        console.print("[yellow]Service is not running.[/yellow]")
        console.print("Start with: rag service start")


@service_app.command("run")
def service_run(
    host: str = typer.Option(None, "--host", help="Bind host"),
    port: int = typer.Option(None, "--port", help="Bind port"),
):
    """Start the service in the foreground (internal use)."""
    from ragtools.service.run import main as run_main
    import sys as _sys
    argv = ["ragtools.service.run"]
    if host:
        argv.extend(["--host", host])
    if port:
        argv.extend(["--port", str(port)])
    _sys.argv = argv
    run_main()


@service_app.command("supervise")
def service_supervise(
    host: str = typer.Option(None, "--host", help="Bind host for the real service"),
    port: int = typer.Option(None, "--port", help="Bind port for the real service"),
    max_failures: int = typer.Option(
        5, "--max-failures",
        help="Give up after this many crashes within --window-seconds.",
    ),
    window_seconds: float = typer.Option(
        300.0, "--window-seconds",
        help="Rolling window (seconds) for counting failures.",
    ),
):
    """Run the supervisor in the foreground. Spawns the real service and
    respawns it on crash. This is what `rag service start` launches by
    default; you normally don't call it directly."""
    from ragtools.service.process import (
        _build_service_run_cmd,
        get_pid_file_path,
    )
    from ragtools.service.supervisor import run_supervisor

    settings = _get_settings()
    if host:
        object.__setattr__(settings, "service_host", host)
    if port:
        object.__setattr__(settings, "service_port", port)

    child_cmd = _build_service_run_cmd(settings)
    data_dir = get_pid_file_path(settings).parent

    exit_code = run_supervisor(
        host=settings.service_host,
        port=settings.service_port,
        data_dir=data_dir,
        child_command=child_cmd,
        max_failures=max_failures,
        window_seconds=window_seconds,
    )
    raise typer.Exit(code=exit_code)


@service_app.command("install")
def service_install(
    delay: int = typer.Option(30, "--delay", "-d", help="Startup delay in seconds"),
):
    """Register to start automatically on Windows login (Task Scheduler)."""
    from ragtools.service.startup import install_task, is_task_installed
    settings = _get_settings()
    try:
        install_task(settings, delay_seconds=delay)
        console.print(f"[green]Startup task installed.[/green]")
        console.print(f"  Task name: RAGTools Service")
        console.print(f"  Trigger: at user logon (delay {delay}s)")
        console.print(f"  Command: python -m ragtools.service.run --from-scheduler")
        console.print(f"\nThe service will start automatically on next login.")
    except RuntimeError as e:
        console.print(f"[red]Install failed:[/red] {e}")
        raise typer.Exit(1)


@service_app.command("uninstall")
def service_uninstall():
    """Remove automatic startup registration."""
    from ragtools.service.startup import uninstall_task
    if uninstall_task():
        console.print("[green]Startup task removed.[/green]")
    else:
        console.print("[yellow]Failed to remove startup task.[/yellow]")


# --- Watchdog (service sub-group) ---

watchdog_app = typer.Typer(help="Scheduled Task watchdog (restarts the service if the supervisor dies).")
service_app.add_typer(watchdog_app, name="watchdog")


@watchdog_app.command("install")
def watchdog_install(
    interval: int = typer.Option(15, "--interval", help="Minutes between health checks. Minimum 1."),
):
    """Register the Windows Task Scheduler watchdog task."""
    from ragtools.service.watchdog import DEFAULT_INTERVAL_MINUTES, install_watchdog_task, TASK_NAME

    if sys.platform != "win32":
        console.print("[yellow]Watchdog install is Windows-only.[/yellow]")
        raise typer.Exit(0)

    if interval < 1:
        console.print("[red]--interval must be at least 1 minute.[/red]")
        raise typer.Exit(2)

    settings = _get_settings()
    if install_watchdog_task(settings, interval_minutes=interval):
        console.print(f"[green]Watchdog installed.[/green]")
        console.print(f"  Task name: {TASK_NAME}")
        console.print(f"  Interval: every {interval} min")
        console.print(f"  Command: rag service watchdog check")
    else:
        console.print("[red]Watchdog install failed — see log.[/red]")
        raise typer.Exit(1)


@watchdog_app.command("uninstall")
def watchdog_uninstall():
    """Remove the watchdog task from Task Scheduler."""
    from ragtools.service.watchdog import uninstall_watchdog_task

    if uninstall_watchdog_task():
        console.print("[green]Watchdog removed (or was not installed).[/green]")
    else:
        console.print("[red]Watchdog uninstall failed — see log.[/red]")
        raise typer.Exit(1)


@watchdog_app.command("status")
def watchdog_status():
    """Show the watchdog task's current state."""
    from ragtools.service.watchdog import TASK_NAME, get_watchdog_info, is_watchdog_installed

    if sys.platform != "win32":
        console.print(f"[dim]Not on Windows — watchdog is a no-op.[/dim]")
        return

    if not is_watchdog_installed():
        console.print(f"[yellow]Watchdog '{TASK_NAME}' is not installed.[/yellow]")
        console.print("Install with: [bold]rag service watchdog install[/bold]")
        return

    info = get_watchdog_info() or {}
    table = Table(title="Watchdog")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for k in ("task_name", "status", "next_run", "last_run", "last_result"):
        table.add_row(k.replace("_", " ").title(), str(info.get(k, "")))
    console.print(table)


@watchdog_app.command("check")
def watchdog_check():
    """Perform one health-check. Invoked by Task Scheduler.

    Always exits 0 so Task Scheduler doesn't mark the task as failed —
    that would suppress Windows's own retry logic and spam notifications.
    """
    from ragtools.service.watchdog import run_check, WatchdogAction

    settings = _get_settings()
    result = run_check(settings)

    if result.action == WatchdogAction.NOTHING:
        console.print("[green]Service is healthy.[/green]")
    elif result.action == WatchdogAction.START:
        pid_str = f" (PID {result.started_pid})" if result.started_pid else ""
        console.print(f"[yellow]Service was dead; relaunched{pid_str}.[/yellow]")
        if result.note:
            console.print(f"[dim]{result.note}[/dim]")
    elif result.action == WatchdogAction.ALREADY_STARTING:
        console.print(f"[dim]Another process is already starting the service.[/dim]")
    # Intentionally do not raise typer.Exit with a non-zero code.


# --- Ignore Subcommands ---


@ignore_app.command("list")
def ignore_list(
    path: str = typer.Argument(".", help="Content root to check for .ragignore files"),
):
    """Show all active ignore patterns grouped by layer."""
    settings = _get_settings(path)
    ignore_rules = _get_ignore_rules(settings, content_root=path)
    patterns = ignore_rules.get_all_patterns()

    console.print("[bold]Built-in defaults[/bold] (not editable):")
    for p in patterns["built-in"]:
        console.print(f"  {p}")

    console.print(f"\n[bold]Global config patterns[/bold] ({len(patterns['config'])} rules):")
    if patterns["config"]:
        for p in patterns["config"]:
            console.print(f"  {p}")
    else:
        console.print("  [dim](none)[/dim]")

    ragignore_files = patterns.get("ragignore_files", {})
    console.print(f"\n[bold].ragignore files[/bold] ({len(ragignore_files)} found):")
    if ragignore_files:
        for filepath, rules in ragignore_files.items():
            console.print(f"  [cyan]{filepath}[/cyan]")
            for r in rules:
                console.print(f"    {r}")
    else:
        console.print("  [dim](none found)[/dim]")


@ignore_app.command("test")
def ignore_test(
    file_path: str = typer.Argument(..., help="File path to test"),
    path: str = typer.Option(".", "--root", "-r", help="Content root"),
):
    """Check if a specific file path would be ignored and why."""
    settings = _get_settings(path)
    ignore_rules = _get_ignore_rules(settings, content_root=path)

    reason = ignore_rules.get_reason(Path(file_path), Path(path))
    if reason:
        console.print(f"[red]IGNORED[/red] — {reason}")
    else:
        console.print(f"[green]NOT IGNORED[/green] — this file would be indexed")


# --- Project Subcommands ---


@project_app.command("list")
def project_list():
    """List all configured projects with status."""
    settings = _get_settings()

    if _probe_service(settings):
        import httpx
        try:
            r = httpx.get(f"{_service_url(settings)}/api/projects/configured", timeout=5.0)
            r.raise_for_status()
            data = r.json()["projects"]
        except Exception as e:
            console.print(f"[red]Failed to get projects from service:[/red] {e}")
            raise typer.Exit(1)
    else:
        # Direct mode: read from settings
        from ragtools.indexing.state import IndexState
        data = []
        state_path = Path(settings.state_db)
        for p in settings.projects:
            files = chunks = 0
            if state_path.exists():
                state = IndexState(settings.state_db)
                records = state.get_all_for_project(p.id)
                files = len(records)
                chunks = sum(r["chunk_count"] for r in records)
                state.close()
            data.append({"id": p.id, "name": p.name, "path": p.path, "enabled": p.enabled, "files": files, "chunks": chunks})

    if not data:
        console.print("[yellow]No projects configured.[/yellow]")
        console.print("Add one with: rag project add --name \"My Docs\" --path /path/to/folder")
        return

    table = Table(title="Configured Projects")
    table.add_column("ID", style="bold")
    table.add_column("Name")
    table.add_column("Path")
    table.add_column("Status")
    table.add_column("Files", justify="right")
    table.add_column("Chunks", justify="right")
    for p in data:
        status = "[green]Enabled[/green]" if p["enabled"] else "[dim]Disabled[/dim]"
        files = str(p["files"]) if p["files"] > 0 else "[dim]--[/dim]"
        chunks = str(p["chunks"]) if p["chunks"] > 0 else "[dim]--[/dim]"
        table.add_row(p["id"], p["name"], p["path"], status, files, chunks)
    console.print(table)


@project_app.command("add")
def project_add(
    name: str = typer.Option(..., "--name", "-n", help="Display name for the project"),
    path: str = typer.Option(..., "--path", "-p", help="Path to project folder"),
    project_id: str = typer.Option("", "--id", help="Project ID (auto-generated from name if not provided)"),
):
    """Add a new project folder to the configuration."""
    import re

    # Auto-generate ID from name if not provided
    if not project_id:
        project_id = re.sub(r'[^a-z0-9-]', '-', name.lower()).strip('-')
        project_id = re.sub(r'-+', '-', project_id)  # collapse multiple hyphens
    if not project_id:
        console.print("[red]Could not generate a valid ID from the name.[/red]")
        raise typer.Exit(1)

    resolved_path = str(Path(path).resolve())
    if not Path(resolved_path).is_dir():
        console.print(f"[red]Path does not exist or is not a directory:[/red] {path}")
        raise typer.Exit(1)

    settings = _get_settings()
    if _probe_service(settings):
        import httpx
        try:
            r = httpx.post(f"{_service_url(settings)}/api/projects",
                          json={"id": project_id, "name": name, "path": resolved_path},
                          timeout=10.0)
            r.raise_for_status()
            console.print(f"[green]Project added:[/green] {project_id} ({name}) → {resolved_path}")
        except httpx.HTTPStatusError as e:
            detail = e.response.json().get("detail", str(e))
            console.print(f"[red]Failed:[/red] {detail}")
            raise typer.Exit(1)
    else:
        # Direct mode: write to TOML
        from ragtools.config import ProjectConfig
        if any(p.id == project_id for p in settings.projects):
            console.print(f"[red]Project ID '{project_id}' already exists.[/red]")
            raise typer.Exit(1)
        new_project = ProjectConfig(id=project_id, name=name, path=resolved_path)
        updated = list(settings.projects) + [new_project]
        from ragtools.service.pages import _save_projects_to_toml
        _save_projects_to_toml(updated)
        console.print(f"[green]Project added:[/green] {project_id} ({name}) → {resolved_path}")


@project_app.command("remove")
def project_remove(
    project_id: str = typer.Argument(..., help="ID of the project to remove"),
):
    """Remove a configured project."""
    settings = _get_settings()

    # Check it exists
    if not any(p.id == project_id for p in settings.projects):
        console.print(f"[yellow]Project '{project_id}' not found.[/yellow]")
        raise typer.Exit(1)

    typer.confirm(f"Remove project '{project_id}'? Indexed data will be kept.", abort=True)

    if _probe_service(settings):
        import httpx
        try:
            r = httpx.delete(f"{_service_url(settings)}/api/projects/{project_id}", timeout=10.0)
            r.raise_for_status()
            console.print(f"[green]Project removed:[/green] {project_id}")
        except Exception as e:
            console.print(f"[red]Failed:[/red] {e}")
            raise typer.Exit(1)
    else:
        updated = [p for p in settings.projects if p.id != project_id]
        from ragtools.service.pages import _save_projects_to_toml
        _save_projects_to_toml(updated)
        console.print(f"[green]Project removed:[/green] {project_id}")


@project_app.command("enable")
def project_enable(
    project_id: str = typer.Argument(..., help="ID of the project to enable"),
):
    """Enable a disabled project."""
    _toggle_project(project_id, enable=True)


@project_app.command("disable")
def project_disable(
    project_id: str = typer.Argument(..., help="ID of the project to disable"),
):
    """Disable a project (stops indexing and watching, keeps data)."""
    _toggle_project(project_id, enable=False)


@project_app.command("add-from-glob")
def project_add_from_glob(
    pattern: str = typer.Argument(
        ...,
        help='Glob pattern matching folders to add (e.g. "D:/Work/*/docs"). Quote it to prevent shell expansion.',
    ),
    exclude: list[str] = typer.Option(
        [], "--exclude", "-x",
        help="Glob pattern to exclude. Can be repeated.",
    ),
    name_prefix: str = typer.Option(
        "", "--name-prefix",
        help="Prefix prepended to every project display name (ids are unchanged).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Show the plan without adding anything.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the interactive confirmation.",
    ),
):
    """Bulk-add projects from a glob pattern.

    Expands the pattern to matching directories, derives a project id from
    each folder basename, skips paths that are already registered, and
    submits the additions in a single pass. Individual failures do not
    abort the run — a summary is printed at the end.
    """
    from ragtools.project_glob import (
        PlanKind,
        derive_plan,
        expand_glob,
        plan_summary,
    )

    settings = _get_settings()

    # --- Phase 1: expand + plan ---
    candidates = expand_glob(pattern, excludes=exclude)
    if not candidates:
        console.print(f"[yellow]No directories matched:[/yellow] {pattern}")
        raise typer.Exit(0)

    plan = derive_plan(candidates, existing=settings.projects, name_prefix=name_prefix)
    summary = plan_summary(plan)
    actionable = [row for row in plan if row.actionable]

    table = Table(title=f"Plan for: {pattern}")
    table.add_column("Status", style="bold")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Path")
    table.add_column("Note", style="dim")
    for row in plan:
        status_style = {
            PlanKind.NEW: "[green]NEW[/green]",
            PlanKind.RENAMED: "[cyan]RENAMED[/cyan]",
            PlanKind.DUPLICATE: "[dim]DUPLICATE[/dim]",
            PlanKind.INVALID: "[red]INVALID[/red]",
        }[row.kind]
        table.add_row(
            status_style,
            row.project_id or "—",
            row.name or "—",
            str(row.path),
            row.reason,
        )
    console.print(table)
    console.print(
        f"Summary: [green]{summary['NEW']} new[/green], "
        f"[cyan]{summary['RENAMED']} renamed[/cyan], "
        f"[dim]{summary['DUPLICATE']} duplicate[/dim], "
        f"[red]{summary['INVALID']} invalid[/red]"
    )

    if dry_run:
        console.print("[dim]--dry-run: no changes applied.[/dim]")
        raise typer.Exit(0)

    if not actionable:
        console.print("[yellow]Nothing to add.[/yellow]")
        raise typer.Exit(0)

    # --- Phase 2: confirm ---
    if not yes:
        typer.confirm(f"Add {len(actionable)} project(s)?", abort=True)

    # --- Phase 3: submit (service first, direct mode fallback) ---
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []  # (id, error)

    if _probe_service(settings):
        import httpx
        url = f"{_service_url(settings)}/api/projects"
        for row in actionable:
            try:
                r = httpx.post(
                    url,
                    json={"id": row.project_id, "name": row.name, "path": str(row.path)},
                    timeout=10.0,
                )
                r.raise_for_status()
                succeeded.append(row.project_id)
            except httpx.HTTPStatusError as e:
                try:
                    detail = e.response.json().get("detail", str(e))
                except Exception:
                    detail = str(e)
                failed.append((row.project_id, detail))
            except Exception as e:
                failed.append((row.project_id, str(e)))
    else:
        # Direct mode: one TOML write at the end to avoid N-way races.
        from ragtools.config import ProjectConfig
        from ragtools.service.pages import _save_projects_to_toml

        updated = list(settings.projects)
        for row in actionable:
            try:
                updated.append(ProjectConfig(
                    id=row.project_id,
                    name=row.name,
                    path=str(row.path),
                ))
                succeeded.append(row.project_id)
            except Exception as e:
                failed.append((row.project_id, str(e)))
        try:
            _save_projects_to_toml(updated)
        except Exception as e:
            console.print(f"[red]Failed to write config:[/red] {e}")
            raise typer.Exit(1)

    # --- Phase 4: report ---
    if succeeded:
        console.print(f"[green]Added {len(succeeded)} project(s):[/green] " + ", ".join(succeeded))
    if failed:
        console.print(f"[red]Failed {len(failed)} project(s):[/red]")
        for pid, err in failed:
            console.print(f"  [red]{pid}[/red]: {err}")
        raise typer.Exit(1)


def _toggle_project(project_id: str, enable: bool):
    """Shared logic for enable/disable."""
    settings = _get_settings()
    project = next((p for p in settings.projects if p.id == project_id), None)
    if not project:
        console.print(f"[yellow]Project '{project_id}' not found.[/yellow]")
        raise typer.Exit(1)

    if project.enabled == enable:
        state = "enabled" if enable else "disabled"
        console.print(f"[dim]Project '{project_id}' is already {state}.[/dim]")
        return

    if _probe_service(settings):
        import httpx
        try:
            r = httpx.post(f"{_service_url(settings)}/api/projects/{project_id}/toggle", timeout=10.0)
            r.raise_for_status()
        except Exception as e:
            console.print(f"[red]Failed:[/red] {e}")
            raise typer.Exit(1)
    else:
        project.enabled = enable
        from ragtools.service.pages import _save_projects_to_toml
        _save_projects_to_toml(list(settings.projects))

    state = "enabled" if enable else "disabled"
    console.print(f"[green]Project {state}:[/green] {project_id}")


# --- Backup commands ---


def _human_size(n: int) -> str:
    """Short, readable byte count for the backup table."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


@backup_app.command("list")
def backup_list():
    """List state-DB backups, newest first."""
    from ragtools.backup import list_backups

    settings = _get_settings()
    backups = list_backups(settings)
    if not backups:
        console.print("[dim]No backups yet.[/dim]")
        return

    table = Table(title="State DB Backups")
    table.add_column("ID", style="bold")
    table.add_column("Timestamp")
    table.add_column("Trigger")
    table.add_column("Size")
    table.add_column("Projects", justify="right")
    table.add_column("Note", style="dim")
    for b in backups:
        table.add_row(
            b.backup_id, b.timestamp, b.trigger,
            _human_size(b.state_db_size),
            str(b.project_count),
            b.note or "",
        )
    console.print(table)
    console.print(f"[dim]{len(backups)} backup(s). "
                  f"Root: {settings.state_db.rsplit('/', 1)[0] if '/' in settings.state_db else '.'}/backups[/dim]")


@backup_app.command("create")
def backup_create(
    note: str = typer.Option("", "--note", "-n", help="Optional description to store in the manifest."),
):
    """Take a manual snapshot of the state DB right now."""
    from ragtools.backup import backup_state_db, prune_backups

    settings = _get_settings()
    target = backup_state_db(settings, trigger="manual", note=note)
    if target is None:
        console.print("[yellow]No backup taken — state DB does not exist yet, or backup failed.[/yellow]")
        raise typer.Exit(1)
    prune_backups(settings)
    console.print(f"[green]Backup created:[/green] {target.name}")


@backup_app.command("prune")
def backup_prune(
    keep: int = typer.Option(None, "--keep", help="Retain this many most-recent backups. Defaults to settings.backup_keep."),
):
    """Delete older backups, keeping only the most recent ones."""
    from ragtools.backup import prune_backups

    settings = _get_settings()
    deleted = prune_backups(settings, keep=keep)
    if deleted:
        console.print(f"[green]Pruned {deleted} old backup(s).[/green]")
    else:
        console.print("[dim]Nothing to prune.[/dim]")


@backup_app.command("restore")
def backup_restore(
    backup_id: str = typer.Argument(..., help="Backup directory name (from `rag backup list`)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
):
    """Restore the state DB from a previous backup.

    A pre-restore safety snapshot is taken automatically so the restore
    itself is reversible.
    """
    from ragtools.backup import restore_backup

    settings = _get_settings()
    if not yes:
        typer.confirm(
            f"Restore state DB from backup '{backup_id}'? "
            "A safety snapshot of the current DB will be taken first.",
            abort=True,
        )
    try:
        safety = restore_backup(settings, backup_id)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Restored from:[/green] {backup_id}")
    if safety:
        console.print(f"[dim]Safety snapshot saved as:[/dim] {safety.name}")


# --- Tray commands ---


@tray_app.callback(invoke_without_command=True)
def tray_default(ctx: typer.Context):
    """When ``rag tray`` is called without a subcommand, run the tray."""
    if ctx.invoked_subcommand is not None:
        return
    # Delegate to the run subcommand so the two paths share code.
    tray_run()


@tray_app.command("run")
def tray_run():
    """Start the system-tray icon in the foreground (blocks until quit)."""
    settings = _get_settings()
    try:
        from ragtools.tray import TrayApp
    except Exception as e:
        console.print(f"[red]Failed to import tray module:[/red] {e}")
        raise typer.Exit(2)
    app_instance = TrayApp(settings=settings)
    rc = app_instance.run()
    raise typer.Exit(code=rc)


@tray_app.command("install")
def tray_install():
    """Register the tray to start on Windows login."""
    if sys.platform != "win32":
        console.print("[yellow]Tray autostart is Windows-only for now.[/yellow]")
        console.print("On macOS/Linux, run `rag tray` manually or use your DE's autostart.")
        raise typer.Exit(0)

    from ragtools.service.tray_startup import install_tray_task, TRAY_STARTUP_FILENAME

    settings = _get_settings()
    try:
        if install_tray_task(settings):
            console.print("[green]Tray autostart installed.[/green]")
            console.print(f"  Script: {TRAY_STARTUP_FILENAME} in the Startup folder")
            console.print("  Trigger: at user login (silent — no console window)")
            console.print("\nStarts automatically on next login. "
                          "Run [bold]rag tray[/bold] to launch it right now.")
        else:
            console.print("[red]Tray install failed.[/red]")
            raise typer.Exit(1)
    except RuntimeError as e:
        console.print(f"[red]Install failed:[/red] {e}")
        raise typer.Exit(1)


@tray_app.command("uninstall")
def tray_uninstall():
    """Remove the tray from Windows login autostart."""
    from ragtools.service.tray_startup import uninstall_tray_task

    if uninstall_tray_task():
        console.print("[green]Tray autostart removed (or was not installed).[/green]")
    else:
        console.print("[red]Tray uninstall failed.[/red]")
        raise typer.Exit(1)


@tray_app.command("status")
def tray_status():
    """Show whether the tray autostart script is registered and if a tray is running."""
    from ragtools.service.tray_startup import (
        TRAY_STARTUP_FILENAME,
        _startup_script_path,
        is_tray_task_installed,
    )

    table = Table(title="Tray")
    table.add_column("Field", style="bold")
    table.add_column("Value")

    if sys.platform != "win32":
        table.add_row("Platform", "[yellow]non-Windows — autostart unsupported[/yellow]")
        console.print(table)
        return

    if is_tray_task_installed():
        table.add_row("Autostart", "[green]Installed[/green]")
        table.add_row("Script", str(_startup_script_path()))
    else:
        table.add_row("Autostart", "[yellow]Not installed[/yellow]")
        table.add_row("Fix", "rag tray install")

    # Is a tray currently running?
    settings = _get_settings()
    from ragtools.tray import _tray_pid_path
    from ragtools.service.process import _process_alive

    pid_file = _tray_pid_path(settings)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if _process_alive(pid):
                table.add_row("Running", f"[green]PID {pid}[/green]")
            else:
                table.add_row("Running", "[dim]No (stale PID file)[/dim]")
        except Exception:
            table.add_row("Running", "[dim]No (unreadable PID file)[/dim]")
    else:
        table.add_row("Running", "[dim]No[/dim]")
    console.print(table)


# --- Helpers ---


def _print_index_stats(stats: dict, full: bool, elapsed: float) -> None:
    """Print indexing statistics in a table."""
    title = "Full Index Complete" if full else "Incremental Index Complete"
    table = Table(title=title)
    table.add_column("Metric", style="bold")
    table.add_column("Value")

    if full or "files_indexed" in stats:
        table.add_row("Files indexed", str(stats.get("files_indexed", 0)))
    else:
        table.add_row("Files indexed", str(stats.get("indexed", 0)))
        table.add_row("Files skipped", str(stats.get("skipped", 0)))
        table.add_row("Files deleted", str(stats.get("deleted", 0)))

    table.add_row("Chunks indexed", str(stats.get("chunks_indexed", 0)))
    table.add_row("Projects", ", ".join(stats.get("projects", [])) or "none")
    if elapsed > 0:
        table.add_row("Time", f"{elapsed:.1f}s")
    console.print(table)


@wiki_app.command("sync", help="Generate a wiki update plan covering changes since a release baseline.")
def wiki_sync(
    since_tag: str = typer.Option(None, "--since-tag", help="Explicit baseline tag (e.g. v2.4.2). Overrides auto-detection."),
    until_ref: str = typer.Option("HEAD", "--until-ref", help="End of the range (default HEAD)."),
    wiki_src: Path = typer.Option(Path("docs/wiki-src"), "--wiki-src", help="Path to the wiki source tree."),
    output: Path = typer.Option(None, "--output", help="Write report to this file (overrides --create-report)."),
    format_: str = typer.Option("markdown", "--format", help="markdown | json | both"),
    create_report: bool = typer.Option(False, "--create-report", help="Write report under tasks/wiki-sync-reports/ with baseline-to-HEAD in the filename."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print summary to stdout only; no file writes."),
):
    """Post-release helper: enumerate everything that changed since the last documented
    baseline and map it to a GitHub Wiki update plan."""
    from ragtools.wiki_sync import run_sync
    import json as _json

    if format_ not in {"markdown", "json", "both"}:
        console.print(f"[red]--format must be one of: markdown, json, both[/red] (got {format_!r})")
        raise typer.Exit(2)

    repo_root = Path.cwd()
    wiki_src_abs = wiki_src if wiki_src.is_absolute() else (repo_root / wiki_src).resolve()

    try:
        baseline, commits, md, data = run_sync(repo_root, since_tag, until_ref, wiki_src_abs)
    except RuntimeError as e:
        console.print(f"[red]wiki sync failed:[/red] {e}")
        raise typer.Exit(1)

    console.print(f"[bold]Baseline:[/bold] {baseline.ref} ([dim]{baseline.reason}[/dim])")
    console.print(f"[bold]Commits analyzed:[/bold] {len(commits)}")
    console.print(f"[bold]Wiki source:[/bold] {wiki_src_abs}")

    if dry_run:
        console.print("\n[yellow]--dry-run set; skipping file writes.[/yellow]")
        if format_ in {"markdown", "both"}:
            console.print("\n--- markdown report ---\n")
            console.print(md)
        if format_ in {"json", "both"}:
            console.print("\n--- json report ---\n")
            console.print(_json.dumps(data, indent=2))
        return

    target_dir: Path
    if output:
        out_path = output if output.is_absolute() else (repo_root / output).resolve()
        md_path = out_path if format_ != "json" else None
        json_path = out_path.with_suffix(".json") if format_ == "both" else (out_path if format_ == "json" else None)
        target_dir = out_path.parent
    elif create_report:
        target_dir = repo_root / "tasks" / "wiki-sync-reports"
        stem = f"{_slug(baseline.ref)}-to-{_slug(until_ref)}"
        md_path = target_dir / f"{stem}.md"
        json_path = target_dir / f"{stem}.json"
    else:
        if format_ in {"markdown", "both"}:
            console.print("\n--- markdown report ---\n")
            console.print(md)
        if format_ in {"json", "both"}:
            console.print("\n--- json report ---\n")
            console.print(_json.dumps(data, indent=2))
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if format_ in {"markdown", "both"} and md_path is not None:
        md_path.write_text(md, encoding="utf-8")
        written.append(md_path)
    if format_ in {"json", "both"} and json_path is not None:
        json_path.write_text(_json.dumps(data, indent=2), encoding="utf-8")
        written.append(json_path)

    for p in written:
        console.print(f"[green]Wrote:[/green] {p}")


def _slug(ref: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in ref).strip("-") or "ref"


def _main() -> None:
    """Entry point that disables Click's Windows glob expansion.

    Click 8+ auto-expands arguments containing *, ?, or [ on Windows because
    cmd.exe historically did not glob. None of rag's commands expect
    shell-expanded arg lists, and `rag project add-from-glob` specifically
    needs the pattern to arrive intact. Disable the auto-expansion so the
    pattern survives to our own glob.glob() call.
    """
    import typer.main as _tm
    cmd = _tm.get_command(app)
    cmd.main(windows_expand_args=False)


if __name__ == "__main__":
    _main()
