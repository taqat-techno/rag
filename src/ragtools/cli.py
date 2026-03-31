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
console = Console()


def _get_settings(content_root: str | None = None):
    """Load settings, optionally overriding content_root."""
    from ragtools.config import Settings

    if content_root:
        return Settings(content_root=content_root)
    return Settings()


# --- Core Commands ---


@app.command()
def index(
    path: str = typer.Argument(".", help="Root directory to scan for projects"),
    full: bool = typer.Option(False, "--full", help="Force full re-index (ignore state)"),
    project: str = typer.Option(None, "--project", "-p", help="Index only this project"),
):
    """Index Markdown files. Incremental by default, skips unchanged files."""
    settings = _get_settings(path)
    start = time.time()

    try:
        if full:
            from ragtools.indexing.indexer import run_full_index

            console.print(f"[bold]Full indexing[/bold] from {settings.content_root}")
            stats = run_full_index(settings, project_id=project)
            elapsed = time.time() - start

            table = Table(title="Full Index Complete")
            table.add_column("Metric", style="bold")
            table.add_column("Value")
            table.add_row("Files indexed", str(stats["files_indexed"]))
            table.add_row("Chunks indexed", str(stats["chunks_indexed"]))
            table.add_row("Projects", ", ".join(stats["projects"]) or "none")
            table.add_row("Time", f"{elapsed:.1f}s")
            console.print(table)
        else:
            from ragtools.indexing.indexer import run_incremental_index

            console.print(f"[bold]Incremental indexing[/bold] from {settings.content_root}")
            stats = run_incremental_index(settings, project_id=project)
            elapsed = time.time() - start

            table = Table(title="Incremental Index Complete")
            table.add_column("Metric", style="bold")
            table.add_column("Value")
            table.add_row("Files indexed", str(stats["indexed"]))
            table.add_row("Files skipped", str(stats["skipped"]))
            table.add_row("Files deleted", str(stats["deleted"]))
            table.add_row("Chunks indexed", str(stats["chunks_indexed"]))
            table.add_row("Projects", ", ".join(stats["projects"]) or "none")
            table.add_row("Time", f"{elapsed:.1f}s")
            console.print(table)

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

    try:
        from ragtools.embedding.encoder import Encoder
        from ragtools.retrieval.formatter import format_context_brief
        from ragtools.retrieval.searcher import Searcher

        client = settings.get_qdrant_client()
        encoder = Encoder(settings.embedding_model)
        searcher = Searcher(client=client, encoder=encoder, settings=settings)

        results = searcher.search(
            query=query,
            project_id=project,
            top_k=top_k,
        )

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

    # Python version
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 10):
        checks.append(("Python", "OK", py_ver))
    else:
        checks.append(("Python", "ERROR", f"{py_ver} (need >=3.10)"))

    # qdrant-client
    try:
        from importlib.metadata import version as pkg_version

        qdrant_ver = pkg_version("qdrant-client")
        checks.append(("qdrant-client", "OK", qdrant_ver))
    except Exception:
        checks.append(("qdrant-client", "MISSING", "pip install -e ."))

    # sentence-transformers
    try:
        import sentence_transformers

        checks.append(("sentence-transformers", "OK", sentence_transformers.__version__))
    except ImportError:
        checks.append(("sentence-transformers", "MISSING", "pip install -e ."))

    # pydantic-settings
    try:
        import pydantic_settings

        checks.append(("pydantic-settings", "OK", pydantic_settings.__version__))
    except ImportError:
        checks.append(("pydantic-settings", "MISSING", "pip install -e ."))

    # MCP
    try:
        from importlib.metadata import version as pkg_version

        mcp_ver = pkg_version("mcp")
        checks.append(("mcp", "OK", mcp_ver))
    except Exception:
        checks.append(("mcp", "MISSING", "pip install -e ."))

    # Data directory
    from ragtools.config import Settings

    settings = Settings()
    data_path = Path(settings.qdrant_path)
    if data_path.exists():
        checks.append(("Data directory", "OK", str(data_path)))
    else:
        checks.append(("Data directory", "NOT CREATED", "Run `rag index <path>` first"))

    # SQLite state DB
    state_path = Path(settings.state_db)
    if state_path.exists():
        checks.append(("State DB", "OK", str(state_path)))
    else:
        checks.append(("State DB", "NOT CREATED", "Run `rag index <path>` first"))

    # Collection
    if data_path.exists():
        try:
            client = settings.get_qdrant_client()
            info = client.get_collection(settings.collection_name)
            checks.append(("Collection", "OK", f"{info.points_count} points"))
        except Exception:
            checks.append(("Collection", "NOT FOUND", f"'{settings.collection_name}' missing"))

    # Print results
    table = Table(title="RAG System Health Check")
    table.add_column("Component", style="bold")
    table.add_column("Status")
    table.add_column("Details")
    for name, stat, details in checks:
        if stat == "OK":
            color = "green"
        elif stat in ("MISSING", "ERROR", "NOT FOUND"):
            color = "red"
        else:
            color = "yellow"
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

        start = time.time()
        console.print(f"\n[bold]Rebuilding index[/bold] from {settings.content_root}")
        stats = run_full_index(settings)
        elapsed = time.time() - start

        table = Table(title="Rebuild Complete")
        table.add_column("Metric", style="bold")
        table.add_column("Value")
        table.add_row("Files indexed", str(stats["files_indexed"]))
        table.add_row("Chunks indexed", str(stats["chunks_indexed"]))
        table.add_row("Projects", ", ".join(stats["projects"]) or "none")
        table.add_row("Time", f"{elapsed:.1f}s")
        console.print(table)

    except Exception as e:
        console.print(f"[red]Rebuild failed:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def projects():
    """List indexed projects with file and chunk counts."""
    settings = _get_settings()

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
            file_count = len(records)
            chunk_count = sum(r["chunk_count"] for r in records)
            table.add_row(pid, str(file_count), str(chunk_count))

        # Totals
        table.add_section()
        table.add_row(
            "[bold]Total[/bold]",
            str(summary["total_files"]),
            str(summary["total_chunks"]),
        )

        state.close()
        console.print(table)

    except Exception as e:
        console.print(f"[red]Failed to list projects:[/red] {e}")
        raise typer.Exit(1)


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
    console.print("This process is meant to be launched by Claude CLI.", err=True)
    console.print("Press Ctrl+C to stop.", err=True)
    mcp_main()


if __name__ == "__main__":
    app()
