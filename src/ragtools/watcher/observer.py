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


def is_indexable_change(path: str, ignore_rules: IgnoreRules, content_root: Path, mode: str) -> bool:
    """Shared watcher predicate: should a changed file be (re)indexed?

    Honors the project Mode (docs / code / general), excludes secret-bearing
    files, and respects ignore rules. Used by both the CLI watcher (observer)
    and the service watcher thread so they behave identically.
    """
    from ragtools.chunking.languages import is_documentation, is_supported
    from ragtools.config import mode_indexes

    p = Path(path)
    if not is_supported(path):
        return False
    if not mode_indexes(mode, is_documentation(path)):
        return False
    if ignore_rules.is_secret(p):
        return False
    return not ignore_rules.is_ignored(p, content_root)


def _make_md_filter(ignore_rules: IgnoreRules, content_root: Path, mode: str):
    """Create a watchfiles filter using ignore rules.

    Accepts indexable files per the project Mode (docs / code / general),
    excludes secrets, plus ``.ragignore`` changes (to trigger a rule reload).
    Returns a closure watchfiles can use as watch_filter.
    """
    def md_filter(change: Change, path: str) -> bool:
        # Accept .ragignore file changes (to trigger rule reload)
        if Path(path).name == RAGIGNORE_FILENAME:
            return True
        return is_indexable_change(path, ignore_rules, content_root, mode)

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
        secret_allowlist=settings.secret_allowlist,
    )

    console.print(f"[bold]Watching[/bold] {content_root} for Markdown changes...")
    console.print(f"  Debounce: {debounce_ms}ms")
    console.print(f"  Press Ctrl+C to stop.\n")

    # v1 legacy CLI watcher (no per-project Mode): map the global flag.
    watch_mode = "general" if getattr(settings, "index_source_code", False) else "docs"

    try:
        for changes in watch(
            content_root,
            watch_filter=_make_md_filter(ignore_rules, root_path, watch_mode),
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

            # Filter to only actual indexable-file changes (not .ragignore changes)
            md_changes = [
                (c, p) for c, p in changes
                if is_indexable_change(p, ignore_rules, root_path, watch_mode)
            ]
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
    """Run incremental indexing, opening and closing Qdrant within this call.

    The `finally` is load-bearing: this runs once per watcher tick, and the
    `except` below deliberately swallows the error and keeps watching. Without
    it, a single failing tick leaked the state and registry SQLite handles for
    the life of the process — one more pair every time it failed again.
    """
    state = router = None
    try:
        from ragtools.embedding.encoder import Encoder
        from ragtools.indexing.indexer import (
            ensure_collection,
            delete_file_points,
            delete_files_points,
            index_file,
        )
        from ragtools.indexing.state import IndexState
        from ragtools.indexing.scanner import scan_project, get_relative_path

        from ragtools.collection_router import build_router

        client = settings.get_qdrant_client()
        encoder = Encoder(settings.embedding_model)
        state = IndexState(settings.state_db)

        # `rag watch` writes vectors, so it must resolve collections exactly the
        # way the service does — otherwise a file edited while the CLI watcher
        # is running lands in the shared collection and is invisible to a
        # per-project search.
        router, _registry, _frameworks = build_router(settings)
        for name in router.all_collections():
            ensure_collection(client, name, encoder.dimension)

        files = scan_project(
            settings.content_root,
            ignore_rules=ignore_rules,
            include_code=getattr(settings, "index_source_code", False),
        )
        current_paths = {get_relative_path(fp, settings.content_root) for _, fp in files}
        tracked_paths = state.get_all_paths()
        deleted_paths = tracked_paths - current_paths

        indexed = 0
        skipped = 0
        deleted = 0
        chunks = 0

        # Handle deleted files. The state row names the owning project, and so
        # the collection — read it before `state.remove` drops the row.
        _drop: dict[str, list[str]] = {}
        for del_path in deleted_paths:
            record = state.get(del_path) or {}
            del_pid = record.get("project_id")
            try:
                targets = [router.write_collection(del_pid)] if del_pid else router.all_collections()
            except Exception:  # noqa: BLE001 — project deregistered; sweep all
                targets = router.all_collections()
            for coll in targets:
                _drop.setdefault(coll, []).append(del_path)
        for coll, paths in _drop.items():
            delete_files_points(client, coll, paths)
        for del_path in deleted_paths:
            state.remove(del_path)
            deleted += 1

        # Process current files
        for pid, file_path in files:
            relative_path = get_relative_path(file_path, settings.content_root)
            current_hash = IndexState.hash_file(file_path)

            if not state.file_changed(relative_path, current_hash):
                skipped += 1
                continue

            collection = router.write_collection(pid)
            ensure_collection(client, collection, encoder.dimension)
            delete_file_points(client, collection, relative_path)
            count = index_file(
                client=client,
                encoder=encoder,
                collection_name=collection,
                project_id=pid,
                file_path=file_path,
                relative_path=relative_path,
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
                secret_allowlist=tuple(settings.secret_allowlist),
            )
            state.update(relative_path, pid, current_hash, count)
            indexed += 1
            chunks += count

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
    finally:
        for handle in (state, router):   # router owns the registry handles
            if handle is not None:
                try:
                    handle.close()
                except Exception:  # noqa: BLE001 — teardown must not mask the error
                    pass


def _short_path(full_path: str, root: str) -> str:
    """Make a path relative to the root for display."""
    try:
        return str(Path(full_path).relative_to(Path(root).resolve()))
    except ValueError:
        return full_path
