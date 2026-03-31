"""CLI entry point for RAG Tools."""

import sys

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="rag",
    help="Local Markdown RAG system for Claude CLI.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def version():
    """Show the RAG Tools version."""
    from ragtools import __version__

    console.print(f"ragtools v{__version__}")


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

    # Data directory
    from pathlib import Path
    from ragtools.config import Settings

    settings = Settings()
    data_path = Path(settings.qdrant_path)
    if data_path.exists():
        checks.append(("Data directory", "OK", str(data_path)))
    else:
        checks.append(("Data directory", "NOT CREATED", "Will be created on first index"))

    # SQLite state DB
    state_path = Path(settings.state_db)
    if state_path.exists():
        checks.append(("State DB", "OK", str(state_path)))
    else:
        checks.append(("State DB", "NOT CREATED", "Will be created on first index"))

    # Print results
    table = Table(title="RAG System Health Check")
    table.add_column("Component", style="bold")
    table.add_column("Status")
    table.add_column("Details")
    for name, status, details in checks:
        if status == "OK":
            color = "green"
        elif status in ("MISSING", "ERROR"):
            color = "red"
        else:
            color = "yellow"
        table.add_row(name, f"[{color}]{status}[/{color}]", details)
    console.print(table)


if __name__ == "__main__":
    app()
