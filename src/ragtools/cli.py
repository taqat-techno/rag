"""CLI entry point for RAG Tools."""

import shutil
import sys
import time
from pathlib import Path

# BEFORE rich, typer, or logging. `ragw.exe` — the GUI-subsystem sibling that
# Task Scheduler launches so no console window appears at login — runs this very
# script with `sys.stdout`/`sys.stderr` set to None. `rich.Console()` below
# inspects its stream at construction, and a `logging.StreamHandler` binds one
# permanently, so both have to find a real file object already in place.
from ragtools._streams import ensure_std_streams

ensure_std_streams()

import typer  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

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
storage_app = typer.Typer(help="Choose the storage engine and collection layout.")
app.add_typer(storage_app, name="storage")
tray_app = typer.Typer(
    help="Manage the RAG Tools system-tray icon.",
    invoke_without_command=True,
)
app.add_typer(tray_app, name="tray")
wiki_app = typer.Typer(help="Wiki sync and maintenance.")
app.add_typer(wiki_app, name="wiki")
client_app = typer.Typer(help="Manage MCP client access profiles (who can do what).")
app.add_typer(client_app, name="client")
console = Console()


def _profile_store(settings=None):
    """Open the client-profile store at ``{data_dir}/profiles.db``."""
    from ragtools.profile_store import ProfileStore
    settings = settings or _get_settings()
    return ProfileStore(str(Path(settings.data_dir) / "profiles.db"))


def _get_settings():
    """Load settings, with the configuration brought to the current schema.

    Every CLI command is a deliberate user action, and migration is
    idempotent, atomic, backed up and behaviour-preserving — so doing it here
    costs one TOML read and removes the case where a CLI-only user never
    migrates at all.
    """
    from ragtools.bootstrap import ensure_config_current_once
    from ragtools.config import Settings
    ensure_config_current_once()
    return Settings()


def _get_ignore_rules(settings, content_root=None):
    """Build IgnoreRules from settings."""
    from ragtools.ignore import IgnoreRules
    return IgnoreRules(
        content_root=content_root or settings.content_root,
        global_patterns=settings.ignore_patterns,
        use_ragignore=settings.use_ragignore_files,
        secret_allowlist=settings.secret_allowlist,
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
            url = _service_url(settings)
            r = httpx.post(f"{url}/api/index",
                           json={"project": project, "full": full}, timeout=60.0)
            r.raise_for_status()
            job = r.json()

            # `/api/index` ENQUEUES; it does not index inline. This used to read
            # `r.json()["stats"]`, a key the endpoint stopped returning when
            # indexing became a job — so `rag index` died with KeyError: 'stats'
            # against any running service, on every platform. Found by running
            # the shipped Linux bundle rather than the source tree.
            job_id = job["job_id"]
            terminal = {"succeeded", "failed", "cancelled"}
            last_phase = None
            while job.get("state") not in terminal:
                time.sleep(0.5)
                job = httpx.get(f"{url}/api/jobs/{job_id}", timeout=30.0).json()
                progress = job.get("progress") or {}
                phase, done, total = (progress.get("phase"),
                                      progress.get("done") or 0,
                                      progress.get("total") or 0)
                if phase and (phase, done // 200) != last_phase:
                    console.print(f"  [dim]{phase}: {done}"
                                  + (f"/{total}" if total else "") + "[/dim]")
                    last_phase = (phase, done // 200)

            if job["state"] != "succeeded":
                console.print(f"[red]Indexing {job['state']}:[/red] "
                              f"{job.get('error') or 'no reason reported'}")
                raise typer.Exit(1)

            _print_index_stats(job.get("result") or {}, full, time.time() - start)
        except typer.Exit:
            raise
        except Exception as e:
            console.print(f"[red]Indexing via service failed:[/red] {e}")
            raise typer.Exit(1)
    else:
        console.print("[yellow]Service is not running.[/yellow] Start with: [bold]rag service start[/bold]")
        raise typer.Exit(1)


def _suggest_projects(settings) -> None:
    """Name the projects the user could scope to. A refusal without a next step
    is just a wall."""
    import httpx

    try:
        payload = httpx.get(f"{_service_url(settings)}/api/projects", timeout=15.0).json()
        ids = [p["project_id"] for p in payload.get("projects") or []]
    except Exception:  # noqa: BLE001 — advice is best-effort, never fatal
        ids = []
    if ids:
        console.print("\n[bold]Available projects:[/bold] " + ", ".join(sorted(ids)))
        console.print(f"[dim]Try: rag search \"...\" -p {sorted(ids)[0]}[/dim]")
    else:
        console.print("[dim]No projects are indexed yet. Run: rag index[/dim]")


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
            # The service is fail-closed on scope: an unscoped search is refused
            # with 422 SCOPE_UNRESOLVED rather than silently widened to
            # everything. That is deliberate — but `rag search "q"` with no -p
            # used to surface the raw HTTP error, which tells the user nothing
            # about what to do. Say what the service said, and what to pass.
            if r.status_code == 422:
                detail = (r.json().get("detail") or {})
                if detail.get("error_code") == "SCOPE_UNRESOLVED":
                    console.print(f"[yellow]{detail.get('error', 'scope unresolved')}[/yellow]")
                    _suggest_projects(settings)
                    raise typer.Exit(2)
            r.raise_for_status()
            data = r.json()
            if data["count"] == 0:
                console.print(f"[yellow]No results found for:[/yellow] '{query}'")
                return
            console.print(f"\n[bold]Results for:[/bold] '{query}'\n")
            for i, result in enumerate(data["results"], 1):
                heading_str = " > ".join(result["headings"]) if result["headings"] else "N/A"
                # Say when a hit came from a vendored dependency rather than the
                # project's own code — the CLI showed the two identically, so
                # the one surface a user reads most said the least.
                tag = ""
                if result.get("scope") == "framework":
                    tag = f" [framework: {result.get('scope_source') or 'shared dependency'}]"
                console.print(f"[{i}] ({result['score']:.3f}) {result['project_id']}/{result['file_path']}{tag} | {heading_str}")
                text = result["text"]
                console.print(f"    {text[:200]}{'...' if len(text) > 200 else ''}")
                console.print()
        except typer.Exit:
            raise          # our own controlled exit, not a transport failure
        except Exception as e:
            console.print(f"[red]Search via service failed:[/red] {e}")
            raise typer.Exit(1)
    else:
        # Direct mode
        router = None
        try:
            from ragtools.collection_router import build_router
            from ragtools.embedding.encoder import Encoder
            from ragtools.retrieval.formatter import format_context_brief
            from ragtools.retrieval.searcher import Searcher

            client = settings.get_qdrant_client()
            encoder = Encoder(settings.embedding_model)
            # Routed, exactly as the service routes. An unrouted offline search
            # under per_project reads the legacy collection name, which names
            # nothing — so `rag search` reported "no results" for the same
            # query the service answers.
            router, _reg, _fw = build_router(settings)
            searcher = Searcher(client=client, encoder=encoder, settings=settings,
                                router=router)

            results = searcher.search(
                query=query, project_id=project, top_k=top_k,
                collections=router.read_collections(project_id=project),
                collection_scoped=router.is_per_project,
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
        finally:
            # SQLite handles; on Windows a leaked one locks registry.db.
            if router is not None:
                router.close()


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
            # `None` means the store could not be counted, which is a different
            # fact from "empty" and must not print as `None`.
            _points = data.get("points_count")
            table.add_row("Points", "unknown (storage unreachable)"
                          if _points is None else str(_points))
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
def doctor(
    json_output: bool = typer.Option(
        False, "--json", help="Emit a machine-readable JSON report instead of the table."
    ),
):
    """Check system health and dependencies.

    Use ``--json`` for a stable machine-readable report (install_mode, service,
    index freshness/scale, watcher, projects, checks, recommended_actions) so
    tooling does not have to parse the human table.
    """
    checks: list[tuple[str, str, str]] = []
    data: dict = {}

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
            # ASK THE ROUTER, NOT `settings.collection_name`. Under
            # `per_project` no collection carries that name, so this check
            # called a healthy 15-collection install "Collection NOT FOUND" —
            # the one row a user runs `rag doctor` to trust.
            from ragtools.collection_router import build_router
            from ragtools.service.owner import (
                compute_scale_warning,
                governing_collection,
            )

            # The router holds SQLite handles; a leaked one keeps registry.db
            # locked on Windows, so it is closed before anything is reported.
            router, _reg, _fw = build_router(settings)
            try:
                per_collection = []
                for name in router.all_collections():
                    try:
                        # `count(exact=True)`, not `points_count`: the latter is
                        # an optimizer estimate that can be None on a fresh
                        # collection, which reads as "empty" rather than
                        # "unknown". UNKNOWN IS NOT ZERO.
                        points = int(client.count(collection_name=name,
                                                  exact=True).count)
                    except Exception:  # noqa: BLE001 — record it as unreadable
                        points = None
                    per_collection.append({"name": name, "points": points})
                label = router.display_name()
            finally:
                router.close()

            readable = [c for c in per_collection if c["points"] is not None]
            unreadable = [c["name"] for c in per_collection if c["points"] is None]
            # A partial sum presented as the total is the same failure as a zero
            # presented as empty — a number the caller cannot tell is wrong.
            points_count = sum(c["points"] for c in readable) if not unreadable else None

            # Surface Qdrant local-mode scale warnings (field-report incident).
            #
            # `capabilities` is not optional here. Without it this always
            # assumed the embedded brute-force engine, so `rag doctor` warned
            # about a 20,000-point ceiling on a managed or external server where
            # no such ceiling exists — advising the user to fix something they
            # had already fixed.
            try:
                from ragtools.storage import resolve_backend
                caps = resolve_backend(settings).capabilities()
            except Exception:  # noqa: BLE001 — unknown engine: assume the weakest
                caps = None
            # The ceiling is per collection, not per index: summing every
            # collection and comparing the total would report "over" forever on
            # exactly the layout that fixes the problem.
            worst_points, worst_name, collection_count = governing_collection(readable)
            scale = compute_scale_warning(
                worst_points, capabilities=caps,
                collection=worst_name, collection_count=collection_count)

            total = f"{points_count:,}" if points_count is not None else "unknown"
            if not readable:
                status_label = "NOT FOUND"
                detail = f"{label}: no collection could be read"
            elif scale["level"] == "over":
                status_label = "WARNING"
                detail = (
                    f"{label}, {total} points — {worst_points:,} in "
                    f"'{worst_name}' is OVER the local-mode limit "
                    f"({scale['hard_limit']:,}). Prune or migrate Qdrant."
                )
            elif scale["level"] == "approaching":
                status_label = "OK"
                detail = (
                    f"{label}, {total} points — '{worst_name}' is approaching "
                    f"the local-mode limit ({scale['hard_limit']:,}). "
                    f"Review ignore_patterns."
                )
            else:
                status_label = "OK"
                detail = f"{label}, {total} points"
            if unreadable:
                status_label = "WARNING"
                detail += f" ({len(unreadable)} unreadable: {', '.join(unreadable)})"
            data["index"] = {
                "points_count": points_count,
                "collection_strategy": settings.collection_strategy,
                "collections": per_collection,
                "scale": scale,
            }
            checks.append(("Collection", status_label, detail))
        except Exception as e:
            checks.append(("Collection", "NOT FOUND", f"no routed collection could be read: {e}"))

    ignore_rules = _get_ignore_rules(settings)
    patterns = ignore_rules.get_all_patterns()
    config_count = len(patterns.get("config", []))
    checks.append(("Ignore rules", "OK", f"{len(patterns['built-in'])} built-in, {config_count} config"))

    # Autostart health, on every platform. The old check was Windows-only and
    # answered "is it installed?" with the FIRST match — which is why a machine
    # carrying a scheduled task and two Startup-folder scripts always looked
    # correctly configured. `get_task_info` reports duplicates and superseded
    # entries, and those are the states worth surfacing.
    try:
        from ragtools.service.startup import get_task_info

        info = get_task_info()
        if info is None:
            checks.append(("Login startup", "MISSING", "Register with: rag service install"))
        elif info.get("problem"):
            checks.append(("Login startup", "WARN", info["problem"]))
        else:
            checks.append(("Login startup", "OK", f"{info['method']} · {info['task_name']}"))
    except Exception as e:
        checks.append(("Login startup", "ERROR", str(e)))


    # --- Index freshness (A-008) ---
    if state_path.exists():
        try:
            from ragtools.indexing.state import IndexState
            from ragtools.service.owner import compute_index_freshness
            _st = IndexState(settings.state_db)
            _summary = _st.get_summary()
            _st.close()
            fr = compute_index_freshness(
                _summary.get("last_indexed"), getattr(settings, "stale_index_hours", 24)
            )
            data["freshness"] = fr
            if fr["level"] == "stale":
                checks.append(("Index freshness", "WARNING", fr["message"]))
            elif fr["level"] == "never":
                checks.append(("Index freshness", "OK", "never indexed"))
            elif fr.get("age_seconds") is not None:
                checks.append(("Index freshness", "OK", f"last indexed {fr['age_seconds'] / 3600:.1f}h ago"))
            else:
                checks.append(("Index freshness", "OK", fr["level"]))
        except Exception as e:
            checks.append(("Index freshness", "ERROR", str(e)))

    # --- Watcher (meaningful only when the service is up) ---
    if _probe_service(settings):
        try:
            import httpx
            _wurl = f"http://{settings.service_host}:{settings.service_port}/api/watcher/status"
            w = httpx.get(_wurl, timeout=2.0).json()
            data["watcher"] = w
            if w.get("running"):
                checks.append(("Watcher", "OK", f"running, {w.get('project_count', 0)} project(s)"))
            elif w.get("last_error"):
                checks.append(("Watcher", "ERROR", str(w["last_error"])))
            else:
                checks.append(("Watcher", "WARNING", "not running — POST /api/watcher/start to re-arm"))
        except Exception as e:
            checks.append(("Watcher", "ERROR", str(e)))
    else:
        checks.append(("Watcher", "UNKNOWN", "service not running"))

    # --- Project paths (warn when an enabled project folder is gone) ---
    _enabled = [p for p in settings.projects if getattr(p, "enabled", True)]
    if _enabled:
        _missing = [p.id for p in _enabled if not Path(p.path).is_dir()]
        data["projects"] = {"enabled": len(_enabled), "missing": _missing}
        if _missing:
            checks.append(("Project paths", "WARNING", f"{len(_missing)} missing: {', '.join(_missing)}"))
        else:
            checks.append(("Project paths", "OK", f"{len(_enabled)} project(s)"))

    # --- Log file location (L6: surface where logs live; previously hidden) ---
    _log_path = Path(settings.data_dir) / "logs" / "service.log"
    data["log_path"] = str(_log_path)
    checks.append(("Logs", "OK" if _log_path.exists() else "INFO", str(_log_path)))

    # --- Output ---
    _bad = {"MISSING", "ERROR", "NOT FOUND"}
    _warn = _bad | {"WARNING", "NOT CREATED", "UNKNOWN", "NOT RUNNING"}
    if json_output:
        import json as _json
        from ragtools import __version__
        from ragtools.config import is_packaged
        report = {
            "install_mode": "packaged" if is_packaged() else "source",
            "version": __version__,
            "data_dir": str(data_path),
            "service": {
                "running": _probe_service(settings),
                "url": f"http://{settings.service_host}:{settings.service_port}",
            },
            "index": data.get("index"),
            "freshness": data.get("freshness"),
            "watcher": data.get("watcher"),
            "projects": data.get("projects"),
            "log_path": data.get("log_path"),
            "ok": all(stat not in _bad for _, stat, _ in checks),
            "checks": [{"component": n, "status": s.lower(), "detail": d} for n, s, d in checks],
            "recommended_actions": [d for n, s, d in checks if s in _warn and d],
        }
        typer.echo(_json.dumps(report, indent=2))
        return

    table = Table(title="RAG System Health Check")
    table.add_column("Component", style="bold")
    table.add_column("Status")
    table.add_column("Details")
    for name, stat, details in checks:
        color = "green" if stat in ("OK", "RUNNING") else "red" if stat in _bad else "yellow"
        table.add_row(name, f"[{color}]{stat}[/{color}]", details)
    console.print(table)


class _OfflineRebuildOwner:
    """The minimum ``blocking_reason`` needs when there is no service.

    Deliberately not a real :class:`~ragtools.service.owner.QdrantOwner`:
    constructing one opens the embedded store this branch is about to delete,
    which is both wasteful and a lock we would then have to drop. Reachability
    is not the question offline — the store is a directory on this disk — so it
    answers yes, and the migration check (the one that matters here) runs
    against the real settings.
    """

    def __init__(self, settings):
        self.settings = settings
        self.indexing = False

    def storage_reachable(self):
        return True, "embedded store on local disk"


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
            # A refusal is not a failure to report as one. The service returns
            # 409 with the specific blocking condition; passing that through is
            # the difference between "try again later" and "something broke".
            if r.status_code == 409:
                detail = r.json().get("detail") or {}
                console.print(f"[yellow]Rebuild refused:[/yellow] "
                              f"{detail.get('message', 'the service declined')}")
                raise typer.Exit(2)
            r.raise_for_status()
            stats = r.json()["stats"]
            _print_index_stats(stats, full=True, elapsed=0)
        except typer.Exit:
            raise
        except Exception as e:
            console.print(f"[red]Rebuild via service failed:[/red] {e}")
            raise typer.Exit(1)
    else:
        # THE OFFLINE BRANCH IS EMBEDDED-ONLY, AND SAYING SO MATTERS.
        # `settings.qdrant_path` is the EMBEDDED store. On a managed or external
        # installation that directory is the pre-migration index kept for
        # rollback — deleting it here would destroy the one copy of the data the
        # migration deliberately preserved, while leaving the live engine's
        # collections completely untouched.
        backend = (getattr(settings, "storage_backend", "embedded") or "embedded").lower()
        if backend != "embedded":
            console.print(
                f"[yellow]Rebuild needs the service on this installation.[/yellow]\n"
                f"  storage_backend is '{backend}', so the index lives in a server "
                f"this command cannot start.\n"
                f"  Start the service (`rag service start`) and run this again.")
            raise typer.Exit(2)

        # The fourth door into a rebuild, and it goes through the same gate. The
        # store is a local directory this command is about to delete, so
        # reachability is not in question — a parked migration is. Rebuilding
        # underneath one destroys the work it has already done and orphans its
        # plan, and nothing here would have noticed.
        from ragtools.service import destructive

        try:
            destructive.assert_allowed(
                _OfflineRebuildOwner(settings), operation="rebuild")
        except destructive.OperationRefused as refused:
            console.print(f"[yellow]Rebuild refused:[/yellow] {refused.reason}")
            raise typer.Exit(2)

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
def selfcheck(
    expect_version: str = typer.Option(
        None, "--expect-version",
        help="Fail unless the installation is this version. Defaults to this build's."),
    port: int = typer.Option(None, "--port", help="Service port to probe for /health."),
    quiet: bool = typer.Option(False, "--quiet", help="Print only on failure."),
    as_json: bool = typer.Option(
        False, "--json",
        help="Emit machine-readable results, including the failure category."),
):
    """Verify the INSTALLATION on this machine, not just this executable.

    Run by the Windows installer after copying files. An installer that copied
    files has proven it copied files; it has not proven the machine now runs the
    new version, and the two come apart whenever an old process was still
    holding its own binaries or a scheduled task still names the old path.

    Exits non-zero if any check fails, so a caller can refuse to report success.
    """
    from ragtools import __version__
    from ragtools.selfcheck import (
        CATEGORY_INTEGRITY,
        CATEGORY_MIGRATING,
        CATEGORY_RUNTIME,
        as_dict,
        classify,
        exit_code,
        failures,
        format_report,
        run_selfcheck,
    )

    expected = expect_version or __version__
    checks = run_selfcheck(expected, port=port)
    broken = failures(checks)
    verdict = classify(checks)
    code = exit_code(checks)

    if as_json:
        import json

        console.print_json(json.dumps(as_dict(checks, expected=expected)))
        raise typer.Exit(code)

    if broken and quiet:
        console.print(f"[red]Installation verification FAILED[/red] (expected {expected})")
    if not quiet:
        console.print(f"Verifying the RAG Tools installation against {expected}\n")
    if broken or not quiet:
        console.print(format_report(checks))

    if broken and verdict == CATEGORY_MIGRATING:
        # Not a failure of the installation, and it must not read as one. The
        # rebuild is running and finishes on its own; the only wrong action here
        # is to reinstall or reboot, which is precisely what the single-bit
        # verdict used to advise.
        console.print(f"\n[yellow]{expected} is installed and its index is "
                      f"still being rebuilt.[/yellow]")
        console.print("  Nothing to do — searches report `migrating` until it "
                      "completes. Watch it with `rag status`.")
        raise typer.Exit(code)

    if broken:
        if verdict == CATEGORY_RUNTIME:
            console.print(f"\n[yellow]{expected} is installed correctly, but it "
                          f"is not running properly.[/yellow]")
        else:
            console.print(f"\n[red]This machine is NOT fully running {expected}.[/red]")
        # Name the remedy for what ACTUALLY failed. A fixed sentence blaming a
        # stale process is wrong — and actively misleading — when the finding is
        # a configuration still at the previous schema, which is a different
        # problem with a different one-line fix.
        names = {check.name for check in broken}
        if names & {"config schema", "migration state"}:
            console.print(
                "  The installation is fine; its CONFIGURATION is not. "
                "Run `rag upgrade`."
            )
        if names & {"storage contract"}:
            console.print(
                "  The storage engine or collection layout is not a supported "
                "combination. Run `rag storage show`."
            )
        if names & {"reindex state"}:
            # This had no branch at all, so the one failure whose remedy is a
            # single documented command printed nothing — while the installer
            # told the user to reboot.
            console.print(
                "  The index rebuild stopped before it finished. Fix the cause "
                "shown above, then run `rag upgrade --resume` — completed work "
                "is not repeated."
            )
        if names & {"service health version"} and verdict == CATEGORY_RUNTIME:
            console.print(
                "  The service is not answering. Start it with "
                "`rag service start --wait`."
            )
        if verdict == CATEGORY_INTEGRITY and names & {
                "installed version", "windowed executable",
                "recorded install version", "running processes",
                "autostart targets"}:
            console.print(
                "  For the installation itself, the most common cause is a "
                "process from the previous version still running while files "
                "were replaced. Restart Windows and re-run the installer."
            )
        raise typer.Exit(code)


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
    wait: bool = typer.Option(
        False, "--wait",
        help="Block until the service answers /health, and fail if it never does.",
    ),
    timeout: int = typer.Option(
        180, "--timeout", help="Seconds to wait with --wait."),
):
    """Start the RAG service in the background.

    `--wait` exists for callers that need the start to be VERIFIABLE rather than
    merely requested — the installer above all. Spawning returns immediately and
    the encoder takes several seconds to load, so "the command succeeded" and
    "the service is serving" are different claims, and an installer that reports
    the first while the second is false is how a machine ends up with a
    registered service nobody is running.
    """
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
        console.print(f"  Logs: {Path(settings.data_dir) / 'logs' / 'service.log'}")
        if not wait:
            console.print("  Note: encoder loading takes 5-10 seconds before service is ready.")
    except RuntimeError as e:
        console.print(f"[yellow]{e}[/yellow]")
    except Exception as e:
        console.print(f"[red]Failed to start service:[/red] {e}")
        raise typer.Exit(1)

    if not wait:
        return

    import time as _time

    import httpx

    url = f"http://{settings.service_host}:{settings.service_port}/health"
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            payload = httpx.get(url, timeout=5.0).json()
        except Exception:  # noqa: BLE001 — not listening yet is the normal case
            _time.sleep(2)
            continue
        # `migrating` is serving — it is answering, and truthfully. Waiting for
        # `ready` here would block the installer for the length of a full
        # re-index, which is hours.
        console.print(f"[green]Service is answering[/green] (status: "
                      f"{payload.get('status')}, version {payload.get('version')})")
        return
    console.print(f"[red]The service did not answer {url} within {timeout}s.[/red]")
    console.print(f"  See {Path(settings.data_dir) / 'logs' / 'service.log'}")
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
    """Check if the RAG service is running.

    Exit codes (stable contract — see docs/decisions.md Decision 16):
      0 — service is running, or transiently starting (PID alive but
          /health not yet 200). Both states are operationally 'fine,
          give it a moment'.
      1 — service is down (no PID and no /health response).
      2 — internal error inside the command itself (e.g. settings
          load raised). Distinct from 1 so CI scripts can tell
          'service is down' from 'this CLI invocation broke'.
    """
    from ragtools.service.process import service_status

    try:
        settings = _get_settings()
        info = service_status(settings)
    except Exception as e:
        console.print(f"[red]rag service status failed:[/red] {e}")
        raise typer.Exit(2)

    if info["running"]:
        table = Table(title="RAG Service")
        table.add_column("Metric", style="bold")
        table.add_column("Value")
        table.add_row("Status", f"[green]{info.get('status', 'running')}[/green]")
        table.add_row("PID", str(info.get("pid", "unknown")))
        table.add_row("Port", str(info.get("port", "")))
        table.add_row("Host", str(info.get("host", "")))
        console.print(table)
        return  # exit code 0

    # L5 — a foreign process is holding the port. Our service is NOT running
    # (exit 1, same as down), but say so clearly so the operator doesn't read
    # the down message as "just start it" when the port is actually taken.
    if info.get("status") == "port_occupied_foreign":
        port = info.get("port", "")
        fpid = info.get("foreign_pid")
        who = f" (PID {fpid})" if fpid else ""
        console.print(
            f"[red]Port {port} is occupied by a non-ragtools process{who}.[/red]"
        )
        console.print(
            "The RAG service is not running. Free the port or set a different "
            "service_port, then: rag service start"
        )
        raise typer.Exit(1)

    console.print("[yellow]Service is not running.[/yellow]")
    console.print("Start with: rag service start")
    raise typer.Exit(1)


@service_app.command("run")
def service_run(
    host: str = typer.Option(None, "--host", help="Bind host"),
    port: int = typer.Option(None, "--port", help="Bind port"),
    profile: str = typer.Option(
        None, "--profile",
        help="Runtime profile (installed|dev). Equivalent to RAG_PROFILE, and "
             "the form autostart uses — a scheduled task cannot carry "
             "environment variables.",
    ),
):
    """Start the service in the foreground (internal use)."""
    if profile:
        # Set before anything resolves settings: the profile decides which data
        # root, index and configuration the process uses.
        #
        # This option exists because `AutostartSpec.environment` is expressible
        # in a systemd unit and a launchd plist but NOT in a Windows scheduled
        # task, so one registration yielded `installed` on Linux and `dev` on
        # Windows — the autostarted service silently serving a different index
        # depending on the OS. An argument travels everywhere a command does.
        import os as _os

        from ragtools.devenv import resolve_profile

        resolve_profile({"RAG_PROFILE": profile})      # rejects an unknown value
        _os.environ["RAG_PROFILE"] = profile.strip().lower()

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
            data.append({"id": p.id, "name": p.name, "path": p.path, "enabled": p.enabled, "mode": p.mode, "files": files, "chunks": chunks})

    if not data:
        console.print("[yellow]No projects configured.[/yellow]")
        console.print("Add one with: rag project add --name \"My Docs\" --path /path/to/folder")
        return

    table = Table(title="Configured Projects")
    table.add_column("ID", style="bold")
    table.add_column("Name")
    table.add_column("Path")
    table.add_column("Status")
    table.add_column("Mode")
    table.add_column("Files", justify="right")
    table.add_column("Chunks", justify="right")
    _mode_disp = {"docs": "Docs", "code": "Code", "general": "General"}
    for p in data:
        status = "[green]Enabled[/green]" if p["enabled"] else "[dim]Disabled[/dim]"
        mode_disp = _mode_disp.get(p.get("mode", "docs"), "Docs")
        files = str(p["files"]) if p["files"] > 0 else "[dim]--[/dim]"
        chunks = str(p["chunks"]) if p["chunks"] > 0 else "[dim]--[/dim]"
        table.add_row(p["id"], p["name"], p["path"], status, mode_disp, files, chunks)
    console.print(table)


@project_app.command("add")
def project_add(
    name: str = typer.Option(..., "--name", "-n", help="Display name for the project"),
    path: str = typer.Option(..., "--path", "-p", help="Path to project folder"),
    project_id: str = typer.Option("", "--id", help="Project ID (auto-generated from name if not provided)"),
    mode: str = typer.Option("docs", "--mode", help="Project Mode: docs (documentation only), code (source & config only), or general (both)"),
):
    """Add a new project folder to the configuration."""
    from ragtools.identity import (
        InvalidProjectId,
        slugify_project_id,
        validate_project_id,
    )

    if mode not in ("docs", "code", "general"):
        console.print("[red]--mode must be: docs, code, or general.[/red]")
        raise typer.Exit(2)

    # One shared rule for the id (plan §11.1). Auto-generate from the name when
    # none is given; otherwise VALIDATE the owner-supplied id — the offline
    # branch below used to accept anything, diverging from the HTTP route.
    if not project_id:
        project_id = slugify_project_id(name)
        if not project_id:
            console.print("[red]Could not generate a valid ID from the name.[/red]")
            raise typer.Exit(1)
    else:
        try:
            project_id = validate_project_id(project_id)
        except InvalidProjectId as exc:
            console.print(f"[red]Invalid --id:[/red] {exc}")
            raise typer.Exit(2)

    resolved_path = str(Path(path).resolve())
    if not Path(resolved_path).is_dir():
        console.print(f"[red]Path does not exist or is not a directory:[/red] {path}")
        raise typer.Exit(1)

    settings = _get_settings()
    if _probe_service(settings):
        import httpx
        try:
            r = httpx.post(f"{_service_url(settings)}/api/projects",
                          json={"id": project_id, "name": name, "path": resolved_path,
                                "mode": mode},
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
        new_project = ProjectConfig(id=project_id, name=name, path=resolved_path,
                                    mode=mode)
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


@project_app.command("mode")
def project_mode(
    project_id: str = typer.Argument(..., help="Project ID"),
    mode: str = typer.Argument(..., help="docs = documentation only, code = source & config only, general = both"),
):
    """Set a project's Mode (docs / code / general)."""
    mode = mode.lower()
    if mode not in ("docs", "code", "general"):
        console.print("[red]mode must be: docs, code, or general.[/red]")
        raise typer.Exit(2)

    settings = _get_settings()
    project = next((p for p in settings.projects if p.id == project_id), None)
    if not project:
        console.print(f"[yellow]Project '{project_id}' not found.[/yellow]")
        raise typer.Exit(1)

    if _probe_service(settings):
        import httpx
        try:
            r = httpx.post(f"{_service_url(settings)}/api/projects/{project_id}/mode",
                           json={"mode": mode}, timeout=10.0)
            r.raise_for_status()
        except Exception as e:
            console.print(f"[red]Failed:[/red] {e}")
            raise typer.Exit(1)
        console.print(f"[green]Mode set:[/green] {project_id} → {mode} (reindex scheduled)")
    else:
        # Direct mode: write TOML.
        project.mode = mode  # type: ignore[assignment]  # validated above
        from ragtools.service.pages import _save_projects_to_toml
        _save_projects_to_toml(list(settings.projects))
        console.print(f"[green]Mode set:[/green] {project_id} → {mode}. Run `rag index` to apply.")


@app.command()
def upgrade(
    dry_run: bool = typer.Option(False, "--dry-run", "-n",
                                 help="Show what would change; write nothing."),
    resume: bool = typer.Option(False, "--resume",
                                help="Continue an interrupted or failed re-index."),
):
    """Bring the configuration to the current schema, and report what changed.

    The SAME implementation the service runs at startup — literally the same
    call, not a parallel one. A separate upgrade path is how the automatic and
    manual routes drift until one of them is wrong, and this project already
    shipped two releases where the migration existed and nothing invoked it.

    Ordinarily there is nothing to do here: every entry point migrates on start.
    This exists for the cases where that is not enough — a machine whose service
    will not start, a configuration on a read-only volume that has since been
    made writable, or simply wanting to see the change before it happens.
    """
    from ragtools.bootstrap import ensure_config_current
    from ragtools.config import Settings
    from ragtools.upgrade import relayout

    if resume:
        # The supported retry path. Only units that are pending or failed are
        # attempted, so this is safe to run repeatedly and cheap when there is
        # nothing left to do.
        settings = Settings()
        if _probe_service(settings):
            # FORWARD, DO NOT REFUSE.
            #
            # This used to stop here and tell the user to shut the service down.
            # On a managed installation that advice is a dead end: stopping the
            # service stops the engine with it, and the CLI then cannot build a
            # client at all — `storage_url` is set in-process at service startup
            # and never persisted, so `ManagedBackend` raises. The command the
            # product advertises on /health failed in both states. The service
            # owns the store, so the service does the resume.
            import httpx
            try:
                r = httpx.post(f"{_service_url(settings)}/api/migration/resume",
                               timeout=30.0)
                if r.status_code == 409:
                    detail = r.json().get("detail") or {}
                    console.print(f"[yellow]Cannot resume yet:[/yellow] "
                                  f"{detail.get('message', 'storage is unavailable')}")
                    raise typer.Exit(2)
                r.raise_for_status()
                body = r.json()
                if body.get("status") == "no_migration":
                    console.print("[green]No migration is pending.[/green]")
                    return
                console.print(f"[green]Resuming via the service[/green] — "
                              f"{body.get('state') or 'rebuild started'}")
                console.print("  Progress: `rag status`, /health, or the admin "
                              "panel. Completed work is not repeated.")
                return
            except typer.Exit:
                raise
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]Could not ask the service to resume:[/red] {exc}")
                raise typer.Exit(1)

        backend = (getattr(settings, "storage_backend", "embedded") or "embedded").lower()
        if backend != "embedded":
            console.print(
                f"[yellow]The service is not running.[/yellow]\n"
                f"  storage_backend is '{backend}', so the engine this rebuild "
                f"writes into is started by the service.\n"
                f"  Start it (`rag service start`) — it resumes the rebuild "
                f"automatically.")
            raise typer.Exit(2)

        plan = relayout.active_plan(settings)
        if plan is None:
            console.print("[green]No migration is pending.[/green]")
            return

        from ragtools.service.owner import QdrantOwner

        before = relayout.progress(settings, plan)
        console.print(f"  resuming: {before.describe()}")
        owner = QdrantOwner(settings)
        try:
            report = relayout.run_pending(
                owner, settings, plan_id=plan,
                # An operator running this has fixed the cause; nobody else
                # knows that, which is why automatic retries are bounded and
                # this one is not.
                reset=True,
                progress_cb=lambda unit, phase: (
                    console.print(f"  rebuilding {unit.kind} {unit.unit_id}...")
                    if phase == "start" else None),
            )
        finally:
            owner.close()

        console.print(report.describe())
        for kind, unit_id, error in report.failures:
            console.print(f"  [red]{kind} {unit_id}[/red]: {error}")
        if not report.complete:
            console.print("[yellow]Re-run `rag upgrade --resume` once the cause "
                          "is fixed — completed work is not repeated.[/yellow]")
            raise typer.Exit(1)
        return

    result = ensure_config_current(allow_write=not dry_run)

    if result.config_path:
        console.print(f"  config: {result.config_path}")
    if result.from_version is not None:
        console.print(f"  schema: v{result.from_version} -> v{result.to_version}")
    if result.added_keys:
        console.print(f"  adds:   {', '.join(sorted(result.added_keys))}")
    for note in result.notes:
        console.print(f"  note:   {note}")

    if result.degraded:
        console.print(f"[red]{result.describe()}[/red]")
        raise typer.Exit(1)

    if dry_run:
        verb = "would be created" if result.from_version == 0 else (
            "would be migrated" if not result.already_current else "is already current")
        console.print(f"[yellow]Dry run:[/yellow] the configuration {verb}.")
        return

    console.print(f"[green]{result.describe()}[/green]")

    # State what the machine will actually run on. "Migrated" without saying to
    # what is the kind of success message that hides a surprise.
    settings = Settings()
    console.print(f"  engine: {settings.storage_backend}")
    console.print(f"  layout: {settings.collection_strategy}")
    if result.migrated:
        console.print("Restart the service for the new configuration to take effect.")


@app.command()
def recover(
    retry: bool = typer.Option(
        False, "--retry",
        help="Give an unresolved rebuild a fresh attempt budget and drive it."),
):
    """Show — and optionally re-drive — a rebuild that did not finish.

    A rebuild that ends with failures leaves a durable marker naming the projects
    it could not finish. The service re-drives them on its own, every few
    minutes, re-testing the blocker each time; this command is for seeing that,
    and for the one case the automatic retry deliberately will not cover — a
    project that has spent its bounded attempt budget, which needs somebody who
    knows the cause was fixed to say so.

    **Restarting the service is not the remedy and never was.** The start path
    re-drives an interrupted rebuild with no fresh budget, so a restart returned
    the same banner with a newer timestamp.
    """
    settings = _get_settings()
    if not _probe_service(settings):
        console.print("[yellow]The service is not running.[/yellow] "
                      "It owns the store, so recovery runs there — start it with "
                      "`rag service start`.")
        raise typer.Exit(2)

    import httpx

    try:
        if retry:
            response = httpx.post(f"{_service_url(settings)}/api/recovery/retry",
                                  timeout=30.0)
        else:
            response = httpx.get(f"{_service_url(settings)}/api/recovery",
                                 timeout=10.0)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Could not reach the service:[/red] {exc}")
        raise typer.Exit(1)

    body = response.json()
    if body.get("status") == "clear":
        console.print("[green]No rebuild is unresolved on this installation.[/green]")
        return
    if retry:
        console.print(f"[green]Retrying via the service[/green] — "
                      f"{body.get('state') or 'recovery started'}")
        console.print("  Progress: `rag recover`, /health, or the admin panel. "
                      "Completed work is not repeated.")
        return

    console.print(f"  plan:    {body.get('plan')}")
    console.print(f"  state:   {body.get('state')}")
    recorded = body.get("blocked_reason_recorded")
    if recorded:
        console.print(f"  blocked (as recorded): {recorded}")
    check = body.get("precondition") or {}
    if check:
        verdict = "clear" if check.get("ok") else (check.get("reason") or "blocked")
        ago = check.get("retested_seconds_ago")
        when = f" ({ago:.0f}s ago)" if isinstance(ago, (int, float)) else ""
        console.print(f"  re-tested{when}: {verdict}")
    for unit in body.get("attempts_exhausted") or []:
        console.print(f"  [yellow]exhausted[/yellow]: {unit.get('kind')} "
                      f"{unit.get('id')} after {unit.get('attempts')} attempts")
    console.print(f"  remedy:  {body.get('remedy')}")


# --- Storage commands ---
#
# The storage engine and the collection layout were readable everywhere and
# settable nowhere: no CLI command, no field on the config API, no control in
# the admin panel. `collection_strategy` appeared in one diagnostics template as
# text. So the v3 architecture could be described but not chosen, and the scale
# warning recommended moving to server mode — a thing the installed product had
# no way to do.
#
# Both settings change WHERE VECTORS LIVE, which is why they share a command
# group and a confirmation: `index_identity` correctly stops trusting the state
# DB after either one changes, and the next index run rebuilds from scratch.


def _write_storage_setting(key: str, value: str) -> None:
    from ragtools.service.pages import _update_toml_config

    _update_toml_config(None, {key: value})


def _confirm_relayout(settings, what: str, yes: bool) -> None:
    """Refuse to silently start hours of work.

    Changing the engine or the layout invalidates every file hash in the state
    DB, so the next index run re-embeds the entire corpus. On a large install
    that is hours. It is a perfectly reasonable thing to ask for and a hostile
    thing to do without asking.
    """
    from ragtools.upgrade.preflight import estimate_required_bytes, run_preflight

    points = 0
    try:
        from ragtools.collection_router import build_router

        client = settings.get_qdrant_client()
        # SUM ACROSS THE ROUTED COLLECTIONS. Counting only
        # `settings.collection_name` returned 0 on a per-project install — no
        # collection has that name — so `estimate_required_bytes` below sized an
        # EMPTY corpus and the disk preflight waved through a machine with no
        # room for the re-index it is about to start.
        router, _reg, _fw = build_router(settings)
        try:
            for name in router.all_collections():
                try:
                    points += int(client.count(collection_name=name,
                                               exact=True).count)
                except Exception:  # noqa: BLE001 — not yet created: really 0
                    continue
        finally:
            router.close()
    except Exception:  # noqa: BLE001 — a fresh install has nothing to count
        points = 0

    console.print(f"[yellow]Changing {what} requires a full re-index.[/yellow]")
    if points:
        console.print(f"  Current index: {points:,} points")
        console.print("  Estimated peak disk needed: "
                      f"~{estimate_required_bytes(points) / 1024**3:.1f} GB")

    # Report the whole list rather than one blocker per attempt. The point
    # count is passed through so the disk check sizes the real corpus instead
    # of assuming an empty one.
    report = run_preflight(settings, point_count=points)
    for blocker in report.blockers:
        console.print(f"  [red]blocked:[/red] {blocker.name} — {blocker.detail}")
        if blocker.remedy:
            console.print(f"            {blocker.remedy}")
    if not report.ok:
        console.print("[red]Refusing: preflight found blockers.[/red]")
        raise typer.Exit(1)

    if not yes and not typer.confirm("Continue?", default=False):
        console.print("Unchanged.")
        raise typer.Exit(1)


@storage_app.command("show")
def storage_show():
    """Show the storage engine and collection layout actually in force."""
    settings = _get_settings()
    console.print(f"  storage_backend    : {settings.storage_backend}")
    console.print(f"  collection_strategy: {settings.collection_strategy}")
    console.print(f"  config_version     : {settings.config_version}")
    if settings.storage_url:
        console.print(f"  storage_url        : {settings.storage_url}")


@storage_app.command("reclaim")
def storage_reclaim(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
):
    """Drop collections the current layout no longer uses.

    The last step of a layout change, and deliberately a SEPARATE one. Changing
    the layout writes to new collections and leaves the previous index exactly
    where it was — that is the only rollback the user has, so nothing removes it
    automatically. Once the new layout is built and trusted, this reclaims the
    space.

    Refuses while the current layout is empty: an index that has not been built
    is not one that has been validated, and dropping the old collection then
    would leave the machine with nothing.
    """
    from ragtools.service.owner import QdrantOwner

    settings = _get_settings()
    if _probe_service(settings):
        console.print("[yellow]Stop the service first[/yellow] — it owns the store.")
        raise typer.Exit(1)

    owner = QdrantOwner(settings)
    try:
        # The same preconditions every other collection-dropping door proves.
        # The CLI is a surface, not an exemption: an offline reclaim can drop the
        # previous layout's collections while a PARKED migration still owns
        # them — exactly the state `blocking_reason` refuses.
        from ragtools.service import destructive

        try:
            destructive.assert_allowed(owner, operation="reclaim")
        except destructive.OperationRefused as refused:
            console.print(f"[red]Refusing:[/red] {refused.reason}")
            raise typer.Exit(2)

        # Allow-list, for the reason spelled out in `relayout.obsolete_collections`:
        # `existing - current` on a shared engine is another installation's whole
        # index, and this command deletes what it computes.
        from ragtools.upgrade.relayout import (
            owned_collections,
            unattributed_collections,
        )

        current = set(owner.router.all_collections())
        try:
            existing = ({c.name for c in owner._client.get_collections().collections}
                        & owned_collections(owner))
            strangers = unattributed_collections(owner)
            if strangers:
                console.print(
                    f"[dim]  leaving {len(strangers)} collection(s) this "
                    f"installation did not create untouched: "
                    f"{', '.join(strangers[:5])}[/dim]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not list collections:[/red] {exc}")
            raise typer.Exit(1)

        live = sum(owner._count_points(name) for name in current)
        orphaned = sorted(existing - current)

        console.print(f"  layout:  {owner.router.strategy}")
        console.print(f"  in use:  {len(current)} collection(s), {live:,} points")
        if not orphaned:
            console.print("[green]Nothing to reclaim.[/green]")
            return
        for name in orphaned:
            console.print(f"  orphan:  {name} ({owner._count_points(name):,} points)")

        if live == 0:
            console.print(
                "[red]Refusing:[/red] the current layout holds no points, so it "
                "has not been built yet. Run `rag index` first — dropping the "
                "previous collections now would leave nothing to search."
            )
            raise typer.Exit(1)

        if not yes and not typer.confirm(
                f"Permanently delete {len(orphaned)} collection(s)?", default=False):
            console.print("Unchanged.")
            raise typer.Exit(1)

        for name in orphaned:
            try:
                owner._client.delete_collection(name)
                console.print(f"[green]dropped[/green] {name}")
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]could not drop {name}:[/red] {exc}")
    finally:
        owner.close()


@storage_app.command("reap")
def storage_reap(
    apply: bool = typer.Option(
        False, "--apply", help="Actually delete. Default is a dry run."),
    grace_hours: float = typer.Option(
        24.0, "--grace-hours",
        help="How long a collection must have been seen orphaned first."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
):
    """Report — and only with --apply, drop — orphaned generation collections.

    A rebuild builds into `proj_<uuid>_g<n>` and swaps to it once the replacement
    is verified, dropping the superseded collection last. A crash between those
    steps, or a drop that fails, leaves a generation nobody points at. `rag
    storage reclaim` cannot see them: it works from the collections the registry
    currently points AT.

    Deliberately a dry run by default. Reaping is the one destructive addition in
    this release, and the collection shape it looks for — `proj_` plus 32 hex —
    is exactly what another installation on a shared engine produces. So nothing
    is deleted on a name: the sweep names every candidate AND every exclusion,
    and only a collection this installation's registry can account for, whose
    project is unambiguous, that is nobody's active pointer, that no unresolved
    rebuild references, and that has sat orphaned past the grace period is ever
    a candidate.
    """
    from ragtools import generation_reaper
    from ragtools.service.owner import QdrantOwner

    settings = _get_settings()
    if _probe_service(settings):
        console.print("[yellow]Stop the service first[/yellow] — it owns the store.")
        raise typer.Exit(1)

    owner = QdrantOwner(settings)
    try:
        report = generation_reaper.reap(
            owner, apply=False, grace_seconds=grace_hours * 3600.0)

        if not report.allowed:
            console.print(f"[red]Refusing:[/red] {report.refusal}")
            for note in report.notes:
                console.print(f"  note:    {note}")
            raise typer.Exit(1)

        for cand in report.excluded:
            console.print(f"  keep:    {cand.describe()}")
        for cand in report.candidates:
            console.print(f"  orphan:  {cand.describe()}")
        for note in report.notes:
            console.print(f"  note:    {note}")

        if not report.candidates:
            console.print("[green]Nothing to reap.[/green]")
            return
        if not apply:
            console.print(
                f"[yellow]Dry run:[/yellow] {len(report.candidates)} "
                f"collection(s) would be dropped. Re-run with --apply.")
            return

        if not yes and not typer.confirm(
                f"Permanently delete {len(report.candidates)} collection(s)?",
                default=False):
            console.print("Unchanged.")
            raise typer.Exit(1)

        # Re-swept rather than acting on the list printed above: between the
        # report and the confirmation a rebuild can start, and a stale candidate
        # list is exactly how a live staging collection gets deleted.
        applied = generation_reaper.reap(
            owner, apply=True, grace_seconds=grace_hours * 3600.0)
        if not applied.allowed:
            console.print(f"[red]Refusing:[/red] {applied.refusal}")
            raise typer.Exit(1)
        for name in applied.deleted:
            console.print(f"[green]dropped[/green] {name}")
        for name, error in applied.failures:
            console.print(f"[red]could not drop {name}:[/red] {error}")
        for cand in applied.excluded:
            if generation_reaper.AUDIT_WRITE_FAILED in cand.exclusions:
                console.print(f"[red]not dropped[/red] {cand.describe()}")
    finally:
        owner.close()


@storage_app.command("backend")
def storage_backend(
    backend: str = typer.Argument(..., help="embedded | managed | external"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
):
    """Set the storage engine. Requires a full re-index."""
    backend = backend.lower()
    if backend not in ("embedded", "managed", "external"):
        console.print("[red]backend must be: embedded, managed, or external.[/red]")
        raise typer.Exit(2)

    settings = _get_settings()
    if backend == settings.storage_backend:
        console.print(f"Already {backend}.")
        return

    if backend == "managed":
        # Fail here, with the reason, rather than at the next service start —
        # no release packages a Qdrant binary, so this is a likely outcome.
        from ragtools.service.managed_qdrant import find_qdrant_binary
        try:
            found = find_qdrant_binary(settings)
        except Exception as exc:  # noqa: BLE001 — an explicit bad path raises
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        if not found:
            console.print(
                "[red]No Qdrant executable found.[/red] Managed mode supervises a "
                "native qdrant binary, which this release does not bundle.\n"
                "  Place it in the data directory's `bin/`, put it on PATH, or set "
                "`qdrant_binary` in the config, then re-run this command.")
            raise typer.Exit(1)
        console.print(f"  Using: {found}")

    _confirm_relayout(settings, "the storage engine", yes)
    _write_storage_setting("storage_backend", backend)
    console.print(f"[green]storage_backend = {backend}.[/green] "
                  "Restart the service, then run `rag index` to rebuild.")


@storage_app.command("strategy")
def storage_strategy(
    strategy: str = typer.Argument(..., help="shared | per_project"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
):
    """Set the collection layout. Requires a full re-index.

    `per_project` gives each project its own collection, so one project's query
    never reads another's vectors and each collection is scanned on its own. It
    only relieves the size ceiling if no SINGLE project exceeds it — a project
    that vendors a framework can exceed it alone.
    """
    strategy = strategy.lower()
    if strategy not in ("shared", "per_project"):
        console.print("[red]strategy must be: shared or per_project.[/red]")
        raise typer.Exit(2)

    settings = _get_settings()
    if strategy == settings.collection_strategy:
        console.print(f"Already {strategy}.")
        return

    _confirm_relayout(settings, "the collection layout", yes)
    _write_storage_setting("collection_strategy", strategy)
    console.print(f"[green]collection_strategy = {strategy}.[/green] "
                  "Restart the service, then run `rag index` to rebuild.")


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
    """Register the tray to start at login."""
    from ragtools.platform import PlatformUnsupported, adapter
    from ragtools.service.tray_startup import TRAY_STARTUP_FILENAME, install_tray_task

    # A tray needs a desktop session. Headless installs are fully supported
    # without one, so say that instead of registering something that would fail
    # at every login.
    try:
        if not adapter().has_desktop_session():
            console.print("[yellow]No desktop session — the tray is not applicable here.[/yellow]")
            console.print("The service runs headless; nothing further is needed.")
            raise typer.Exit(0)
    except PlatformUnsupported as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(0)

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

    from ragtools.platform import KIND_TRAY, PlatformUnsupported, adapter

    try:
        impl = adapter()
    except PlatformUnsupported as exc:
        table.add_row("Platform", f"[yellow]{exc}[/yellow]")
        console.print(table)
        return

    table.add_row("Platform", impl.name)
    if not impl.has_desktop_session():
        table.add_row("Desktop session", "[yellow]none — tray not applicable[/yellow]")

    found = impl.find_autostart(KIND_TRAY)
    if any(not r.legacy for r in found):
        table.add_row("Autostart", "[green]Installed[/green]")
        for registration in found:
            table.add_row("  " + ("legacy" if registration.legacy else "entry"),
                          registration.describe())
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


# ---------------------------------------------------------------------------
# Client access profiles (rag client ...)
# ---------------------------------------------------------------------------


@client_app.command("capabilities")
def client_capabilities():
    """List the capability groups you can grant (the security checkboxes)."""
    from ragtools.client_admin import AGENT_GRANTABLE_GROUPS, CAPABILITY_CATALOG

    table = Table(title="Capability groups")
    table.add_column("Group"); table.add_column("Grantable")
    table.add_column("Tier"); table.add_column("What it allows")
    for c in CAPABILITY_CATALOG:
        grantable = "yes" if c.group in AGENT_GRANTABLE_GROUPS else "[dim]owner-only[/dim]"
        table.add_row(c.group, grantable, c.tier, c.description)
    console.print(table)
    console.print("[dim]Destructive ops (delete/restore collection) are a separate "
                  "opt-in: add --allow-destructive.[/dim]")


@client_app.command("list")
def client_list():
    """List configured client profiles."""
    from ragtools.client_admin import profile_summary

    profiles = _profile_store().list()
    if not profiles:
        console.print("[dim]No client profiles. The owner (no RAG_CLIENT_PROFILE) "
                      "has full access by default.[/dim]")
        return
    table = Table(title="Client access profiles")
    table.add_column("ID"); table.add_column("Name"); table.add_column("Scope")
    table.add_column("Capabilities"); table.add_column("Destructive")
    for p in profiles:
        s = profile_summary(p)
        scope = s["scope"] if isinstance(s["scope"], str) else ", ".join(s["scope"]) or "none"
        table.add_row(s["profile_id"], s["display_name"], scope,
                      ", ".join(s["capabilities"]) or "none",
                      "yes" if s["destructive"] else "no")
    console.print(table)


@client_app.command("show")
def client_show(profile_id: str):
    """Show one client profile and its .mcp.json snippet."""
    import json

    from ragtools.client_admin import client_config_snippet, profile_summary

    p = _profile_store().get(profile_id)
    if p is None:
        console.print(f"[red]No such client:[/red] {profile_id}")
        raise typer.Exit(1)
    console.print(profile_summary(p))
    console.print("\n[bold]Client config (.mcp.json):[/bold]")
    console.print(json.dumps(client_config_snippet(p), indent=2))


@client_app.command("add")
def client_add(
    profile_id: str = typer.Argument(..., help="Client id (lowercase, e.g. 'docs-bot')."),
    name: str = typer.Option("", "--name", help="Display name."),
    all_projects: bool = typer.Option(False, "--all-projects", help="Grant all projects."),
    projects: str = typer.Option("", "--projects", help="Comma-separated project ids to scope to."),
    cap: list[str] = typer.Option(None, "--cap", help="Capability group (repeatable). See `rag client capabilities`."),
    allow_destructive: bool = typer.Option(False, "--allow-destructive", help="Permit delete/restore collection."),
):
    """Add or update a client profile with specific access."""
    import json

    from ragtools.client_admin import ClientAdminError, build_profile, client_config_snippet

    proj_list = [x.strip() for x in projects.split(",") if x.strip()] if projects else []
    try:
        profile = build_profile(
            profile_id=profile_id, display_name=name, all_projects=all_projects,
            projects=proj_list, capabilities=list(cap or []),
            allow_destructive=allow_destructive,
        )
    except ClientAdminError as exc:
        console.print(f"[red]Invalid client:[/red] {exc}")
        raise typer.Exit(2)
    store = _profile_store()
    existed = store.get(profile.profile_id) is not None
    store.add(profile)
    console.print(f"[green]Client {'updated' if existed else 'created'}:[/green] "
                  f"{profile.profile_id}")
    console.print("\n[bold]Point the client's MCP at this profile (.mcp.json):[/bold]")
    console.print(json.dumps(client_config_snippet(profile), indent=2))


@client_app.command("remove")
def client_remove(profile_id: str):
    """Remove a client profile."""
    store = _profile_store()
    if store.get(profile_id) is None:
        console.print(f"[red]No such client:[/red] {profile_id}")
        raise typer.Exit(1)
    store.remove(profile_id)
    console.print(f"[green]Client removed:[/green] {profile_id}")


@client_app.command("config")
def client_config(profile_id: str):
    """Print the .mcp.json snippet for a client."""
    import json

    from ragtools.client_admin import client_config_snippet

    p = _profile_store().get(profile_id)
    if p is None:
        console.print(f"[red]No such client:[/red] {profile_id}")
        raise typer.Exit(1)
    console.print(json.dumps(client_config_snippet(p), indent=2))


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
