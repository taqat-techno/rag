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
from ragtools.ignore import IgnoreRules, RAGIGNORE_FILENAME

console = Console()


def _make_md_filter(ignore_rules: IgnoreRules, content_root: Path):
    """Create a watchfiles filter using ignore rules.

    Returns a closure that watchfiles can use as watch_filter.
    """
    def md_filter(change: Change, path: str) -> bool:
        # Accept .ragignore file changes (to trigger rule reload)
        if Path(path).name == RAGIGNORE_FILENAME:
            return True
        if not path.endswith(".md"):
            return False
        return not ignore_rules.is_ignored(Path(path), content_root)

    return md_filter


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

    root_path = Path(content_root).resolve()
    ignore_rules = IgnoreRules(
        content_root=root_path,
        global_patterns=settings.ignore_patterns,
        use_ragignore=settings.use_ragignore_files,
    )

    console.print(f"[bold]Watching[/bold] {content_root} for Markdown changes...")
    console.print(f"  Debounce: {debounce_ms}ms")
    console.print(f"  Press Ctrl+C to stop.\n")

    try:
        for changes in watch(
            content_root,
            watch_filter=_make_md_filter(ignore_rules, root_path),
            debounce=debounce_ms,
            recursive=True,
            raise_interrupt=False,
        ):
            if not changes:
                continue

            # Check if any .ragignore files changed — reload rules
            ragignore_changed = any(
                Path(p).name == RAGIGNORE_FILENAME for _, p in changes
            )
            if ragignore_changed:
                ignore_rules.clear_cache()
                console.print("  [dim].ragignore changed — ignore rules reloaded[/dim]")

            # Filter to only actual .md changes (not .ragignore changes)
            md_changes = [(c, p) for c, p in changes if p.endswith(".md")]
            if not md_changes:
                continue

            # Summarize what changed
            added = [p for c, p in md_changes if c == Change.added]
            modified = [p for c, p in md_changes if c == Change.modified]
            deleted = [p for c, p in md_changes if c == Change.deleted]

            console.print(f"\n[yellow]Changes detected[/yellow] at {time.strftime('%H:%M:%S')}:")
            for p in added:
                console.print(f"  [green]+ {_short_path(p, content_root)}[/green]")
            for p in modified:
                console.print(f"  [blue]~ {_short_path(p, content_root)}[/blue]")
            for p in deleted:
                console.print(f"  [red]- {_short_path(p, content_root)}[/red]")

            # Run incremental indexing
            _run_incremental(settings, ignore_rules)

    except KeyboardInterrupt:
        console.print("\n[bold]Watcher stopped.[/bold]")


def _run_incremental(settings: Settings, ignore_rules: IgnoreRules) -> None:
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

        files = scan_project(settings.content_root, ignore_rules=ignore_rules)
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
