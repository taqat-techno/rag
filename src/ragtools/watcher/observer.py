"""File watcher that triggers incremental indexing on Markdown changes.

Design:
- Uses watchfiles (Rust-based, near-zero CPU) to monitor directories
- Opens Qdrant client ONLY during indexing runs, closes immediately after
- Between runs, the Qdrant data directory is unlocked
- The MCP server can access the data whenever the watcher is idle (most of the time)

Safety:
- Do NOT run `rag watch` and Claude CLI simultaneously
- The watcher is a foreground convenience tool for editing sessions
- Stop it (Ctrl+C) before starting Claude
"""

import time
from pathlib import Path

from rich.console import Console
from watchfiles import watch, Change

from ragtools.config import Settings
from ragtools.indexing.scanner import SKIP_DIRS

console = Console()


def _md_filter(change: Change, path: str) -> bool:
    """Only watch .md file changes, skip noise directories."""
    if not path.endswith(".md"):
        return False
    parts = Path(path).parts
    return not any(part in SKIP_DIRS for part in parts)


def run_watch(
    content_root: str = ".",
    debounce_ms: int = 3000,
    settings: Settings | None = None,
) -> None:
    """Watch for Markdown file changes and trigger incremental indexing.

    Args:
        content_root: Root directory to watch.
        debounce_ms: Milliseconds to wait after last change before indexing.
        settings: Configuration override.
    """
    if settings is None:
        settings = Settings(content_root=content_root)
    else:
        settings = Settings(content_root=content_root, **{
            k: v for k, v in settings.model_dump().items()
            if k != "content_root"
        })

    console.print(f"[bold]Watching[/bold] {content_root} for Markdown changes...")
    console.print(f"  Debounce: {debounce_ms}ms")
    console.print(f"  Press Ctrl+C to stop.\n")

    try:
        for changes in watch(
            content_root,
            watch_filter=_md_filter,
            debounce=debounce_ms,
            recursive=True,
            raise_interrupt=False,
        ):
            if not changes:
                continue

            # Summarize what changed
            added = [(p) for c, p in changes if c == Change.added]
            modified = [(p) for c, p in changes if c == Change.modified]
            deleted = [(p) for c, p in changes if c == Change.deleted]

            console.print(f"\n[yellow]Changes detected[/yellow] at {time.strftime('%H:%M:%S')}:")
            for p in added:
                console.print(f"  [green]+ {_short_path(p, content_root)}[/green]")
            for p in modified:
                console.print(f"  [blue]~ {_short_path(p, content_root)}[/blue]")
            for p in deleted:
                console.print(f"  [red]- {_short_path(p, content_root)}[/red]")

            # Run incremental indexing
            _run_incremental(settings)

    except KeyboardInterrupt:
        console.print("\n[bold]Watcher stopped.[/bold]")


def _run_incremental(settings: Settings) -> None:
    """Run incremental indexing, opening and closing Qdrant within this call."""
    try:
        from ragtools.embedding.encoder import Encoder
        from ragtools.indexing.indexer import (
            ensure_collection,
            delete_file_points,
            index_file,
        )
        from ragtools.indexing.state import IndexState
        from ragtools.indexing.scanner import scan_project, get_relative_path

        client = settings.get_qdrant_client()
        encoder = Encoder(settings.embedding_model)
        state = IndexState(settings.state_db)

        ensure_collection(client, settings.collection_name, encoder.dimension)

        files = scan_project(settings.content_root)
        current_paths = {get_relative_path(fp, settings.content_root) for _, fp in files}
        tracked_paths = state.get_all_paths()
        deleted_paths = tracked_paths - current_paths

        indexed = 0
        skipped = 0
        deleted = 0
        chunks = 0

        # Handle deleted files
        for del_path in deleted_paths:
            delete_file_points(client, settings.collection_name, del_path)
            state.remove(del_path)
            deleted += 1

        # Process current files
        for pid, file_path in files:
            relative_path = get_relative_path(file_path, settings.content_root)
            current_hash = IndexState.hash_file(file_path)

            if not state.file_changed(relative_path, current_hash):
                skipped += 1
                continue

            delete_file_points(client, settings.collection_name, relative_path)
            count = index_file(
                client=client,
                encoder=encoder,
                collection_name=settings.collection_name,
                project_id=pid,
                file_path=file_path,
                relative_path=relative_path,
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
            )
            state.update(relative_path, pid, current_hash, count)
            indexed += 1
            chunks += count

        state.close()

        # Close Qdrant client to release the lock
        del client

        if indexed > 0 or deleted > 0:
            console.print(
                f"  [green]Indexed: {indexed} files ({chunks} chunks) | "
                f"Skipped: {skipped} | Deleted: {deleted}[/green]"
            )
        else:
            console.print(f"  [dim]No indexing needed ({skipped} files unchanged)[/dim]")

    except Exception as e:
        console.print(f"  [red]Indexing error: {e}[/red]")


def _short_path(full_path: str, root: str) -> str:
    """Make a path relative to the root for display."""
    try:
        return str(Path(full_path).relative_to(Path(root).resolve()))
    except ValueError:
        return full_path
