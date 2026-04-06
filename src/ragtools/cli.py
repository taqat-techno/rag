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
console = Console()


def _get_settings(content_root: str | None = None):
    """Load settings, optionally overriding content_root."""
    from ragtools.config import Settings

    if content_root:
        return Settings(content_root=content_root)
    return Settings()


def _get_ignore_rules(settings=None, content_root: str | None = None):
    """Create IgnoreRules from settings."""
    from ragtools.ignore import IgnoreRules

    if settings is None:
        settings = _get_settings(content_root)
    return IgnoreRules(
        content_root=settings.content_root,
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
    path: str = typer.Argument(".", help="Root directory to scan for projects"),
    full: bool = typer.Option(False, "--full", help="Force full re-index (ignore state)"),
    project: str = typer.Option(None, "--project", "-p", help="Index only this project"),
    show_ignored: bool = typer.Option(False, "--show-ignored", help="Print ignored files"),
):
    """Index Markdown files. Incremental by default, skips unchanged files."""
    settings = _get_settings(path)
    start = time.time()

    if show_ignored:
        ignore_rules = _get_ignore_rules(settings)
        _print_ignored_files(path, ignore_rules)

    if _probe_service(settings):
        # Route through service
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
        # Direct mode
        ignore_rules = _get_ignore_rules(settings)
        try:
            if full:
                from ragtools.indexing.indexer import run_full_index
                console.print(f"[bold]Full indexing[/bold] from {settings.content_root}")
                stats = run_full_index(settings, project_id=project, ignore_rules=ignore_rules)
            else:
                from ragtools.indexing.indexer import run_incremental_index
                console.print(f"[bold]Incremental indexing[/bold] from {settings.content_root}")
                stats = run_incremental_index(settings, project_id=project, ignore_rules=ignore_rules)
            elapsed = time.time() - start
            _print_index_stats(stats, full, elapsed)
        except Exception as e:
            console.print(f"[red]Indexing failed:[/red] {e}")
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
            checks.append(("Collection", "OK", f"{info.points_count} points"))
        except Exception:
            checks.append(("Collection", "NOT FOUND", f"'{settings.collection_name}' missing"))

    ignore_rules = _get_ignore_rules(settings)
    patterns = ignore_rules.get_all_patterns()
    config_count = len(patterns.get("config", []))
    checks.append(("Ignore rules", "OK", f"{len(patterns['built-in'])} built-in, {config_count} config"))

    table = Table(title="RAG System Health Check")
    table.add_column("Component", style="bold")
    table.add_column("Status")
    table.add_column("Details")
    for name, stat, details in checks:
        color = "green" if stat in ("OK", "RUNNING") else "red" if stat in ("MISSING", "ERROR", "NOT FOUND") else "yellow"
        table.add_row(name, f"[{color}]{stat}[/{color}]", details)
    console.print(table)


@app.command()
def rebuild(
    path: str = typer.Argument(".", help="Root directory to scan for projects"),
):
    """Drop all data and rebuild index from scratch."""
    settings = _get_settings(path)

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
def watch(
    path: str = typer.Argument(".", help="Root directory to watch for changes"),
    debounce: int = typer.Option(3000, "--debounce", "-d", help="Debounce delay in milliseconds"),
):
    """Watch Markdown files and auto-index on changes.

    NOTE: Do not run simultaneously with Claude CLI (MCP server).
    """
    settings = _get_settings()
    if _probe_service(settings):
        import httpx
        try:
            r = httpx.post(f"{_service_url(settings)}/api/watcher/start", timeout=5.0)
            r.raise_for_status()
            console.print(f"[green]Watcher started via service[/green]")
            console.print("Stop with: rag service stop (or POST /api/watcher/stop)")
        except Exception as e:
            console.print(f"[red]Failed to start watcher via service:[/red] {e}")
            raise typer.Exit(1)
    else:
        from ragtools.watcher.observer import run_watch
        run_watch(content_root=path, debounce_ms=debounce)


@app.command()
def version():
    """Show the RAG Tools version."""
    from ragtools import __version__
    console.print(f"ragtools v{__version__}")


@app.command()
def serve():
    """Start the MCP server for Claude CLI integration."""
    from ragtools.integration.mcp_server import main as mcp_main
    console.print("[bold]Starting RAG MCP server (stdio transport)...[/bold]", err=True)
    console.print("Press Ctrl+C to stop.", err=True)
    mcp_main()


# --- Service Subcommands ---


@service_app.command("start")
def service_start():
    """Start the RAG service in the background."""
    from ragtools.service.process import start_service
    settings = _get_settings()
    try:
        pid = start_service(settings)
        console.print(f"[green]Service started[/green] (PID {pid})")
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


def _print_ignored_files(content_root: str, ignore_rules) -> None:
    """Print files that would be ignored during indexing."""
    from ragtools.indexing.scanner import discover_projects

    root = Path(content_root).resolve()
    projects = discover_projects(content_root)
    ignored_count = 0

    for pid, project_dir in projects.items():
        for md in project_dir.rglob("*.md"):
            reason = ignore_rules.get_reason(md, root)
            if reason:
                try:
                    rel = md.relative_to(root)
                except ValueError:
                    rel = md
                console.print(f"  [dim]IGNORED {rel} — {reason}[/dim]")
                ignored_count += 1

    if ignored_count:
        console.print(f"  [dim]{ignored_count} file(s) ignored[/dim]\n")
    else:
        console.print("  [dim]No files ignored[/dim]\n")


if __name__ == "__main__":
    app()
