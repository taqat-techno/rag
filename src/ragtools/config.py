"""Configuration for RAG Tools.

Priority chain (highest to lowest):
  1. Environment variables (RAG_* prefix)
  2. TOML config file (if found)
  3. Code defaults

TOML config resolution (first match wins):
  1. RAG_CONFIG_PATH env var
  2. %LOCALAPPDATA%/RAGTools/config.toml  (Windows installed/packaged mode)
  3. ./ragtools.toml  (dev mode)

Packaged mode detection:
  If sys.frozen is True (PyInstaller bundle), paths resolve to
  %LOCALAPPDATA%/RAGTools/ instead of relative CWD paths.
"""

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Literal, Tuple, Type

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

logger = logging.getLogger("ragtools.config")


#: The config schema version this release reads and writes.
#:
#: ONE definition, because there used to be three literals and they disagreed.
#: `_save_projects_to_toml` wrote ``2`` unconditionally from sixteen call sites —
#: every project add, edit, mode change, ignore rule and dependency change from
#: the CLI, the admin panel and MCP alike — while `_update_toml_config` stamped
#: ``1`` onto a file that had no version key. A migration to v3 was therefore
#: undone by the user's next edit and re-run on the following boot, forever.
#:
#: Lives here rather than in `ragtools.upgrade` because the version of the
#: schema is a fact about the schema. `upgrade.migrate` re-exports it so the
#: name keeps working where it was already imported.
CONFIG_VERSION = 3


# --- Project Configuration ---

# Canonical project indexing modes (Project Mode, distinct from Project Status).
PROJECT_MODES = ("docs", "code", "general")
ProjectMode = Literal["docs", "code", "general"]


def mode_indexes(mode: str, is_documentation: bool) -> bool:
    """Whether a project in `mode` should index a file, given whether that file
    is documentation (Markdown / text / README) vs source/config/code.

      docs    -> documentation only
      code    -> source / config / code only (NOT docs-only Markdown)
      general -> both

    Secret exclusion is a SEPARATE, always-on layer and is not decided here.
    """
    if mode == "docs":
        return is_documentation
    if mode == "code":
        return not is_documentation
    return True  # general


def _dependency_path_key(path: str | Path) -> str:
    """Identity of a dependency folder, for "is this the same one?".

    Resolved, then case-folded. ``resolve()`` collapses symlinks, junctions,
    ``..`` and relative spellings; case-folding collapses ``C:\\`` vs ``c:\\``
    on Windows. Two spellings of one folder must never become two catalog
    entries — that is precisely the duplication the catalog exists to remove.

    Falls back to the literal string when the path cannot be resolved (it may
    not exist yet), which is still better than treating every spelling as
    distinct.
    """
    try:
        resolved = str(Path(path).resolve())
    except OSError:
        resolved = str(path)
    return os.path.normcase(resolved)


def _slugify_dependency_id(text: str) -> str:
    """Folder name -> a usable catalog id."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug or "dependency"


def _unique_dependency_id(path: Path, taken: set[str]) -> str:
    """A stable, readable id for an adopted legacy path.

    Prefers the folder name; disambiguates with the parent folder before
    falling back to a counter, so two vendored cores both called ``odoo``
    become ``odoo`` and ``pearl-pixels-18-odoo`` rather than ``odoo-2``.
    """
    base = _slugify_dependency_id(path.name)
    if base not in taken:
        return base
    parent = _slugify_dependency_id(path.parent.name)
    if parent:
        combined = f"{parent}-{base}"
        if combined not in taken:
            return combined
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


class DependencyConfig(BaseModel):
    """A shared dependency in the catalog — declared once, used by many projects.

    Deliberately holds INTENT only (id, display name, path). What the folder
    turns out to be — framework name, version, edition, build id, and therefore
    which collection it maps to — is derived at every sync and never stored
    here: a checkout can switch branches or be upgraded in place, and a stale
    stored identity would quietly route a project at the wrong corpus.

    ``path`` is the natural key. Two entries for one resolved folder would
    defeat the entire point of declaring it once.
    """

    id: str                    # slug; the key projects link by
    name: str = ""             # display name (defaults to id)
    path: str                  # absolute path to the dependency root
    enabled: bool = True       # skip without deleting (keeps links intact)

    def model_post_init(self, __context: Any) -> None:
        if not self.name:
            self.name = self.id


class ProjectConfig(BaseModel):
    """Configuration for a single project folder."""

    id: str                                          # unique identifier, used in storage keys
    name: str = ""                                   # display name (defaults to id)
    path: str                                        # absolute path to project folder
    enabled: bool = True                             # Project Status: skip if False
    ignore_patterns: list[str] = Field(default_factory=list)  # per-project ignore patterns
    # Catalog entries this project uses — ids from `Settings.dependencies`.
    # This is the model users manage: declare a shared dependency once, then
    # select it from any number of projects.
    dependencies: list[str] = Field(default_factory=list)
    # LEGACY, still honoured: dependency roots declared as raw paths on the
    # project itself. Superseded by the `dependencies` catalog above, which is
    # migrated to on load; sync takes the union of both so an existing config
    # keeps working untouched.
    dependency_paths: list[str] = Field(default_factory=list)
    # Project Mode — what this project indexes (secret-bearing files always excluded):
    #   "docs"    = documentation / text / Markdown only (default)
    #   "code"    = source / config / code only (no docs-only Markdown)
    #   "general" = both docs and code/config
    mode: ProjectMode = "docs"

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_index_source_code(cls, data):
        """Back-compat: derive `mode` from the legacy per-project
        `index_source_code` when `mode` is absent, then drop the legacy key.
          index_source_code True             -> "general" (old True indexed code AND docs)
          index_source_code False/None/absent -> "docs"
        """
        if not isinstance(data, dict):
            return data
        if "index_source_code" in data:
            data = dict(data)
            legacy = data.pop("index_source_code")
            data.setdefault("mode", "general" if legacy is True else "docs")
        return data

    def model_post_init(self, __context: Any) -> None:
        if not self.name:
            self.name = self.id

    def indexes(self, is_documentation: bool) -> bool:
        """Whether this project indexes a file given whether it is documentation."""
        return mode_indexes(self.mode, is_documentation)


# --- Path Resolution ---


def is_packaged() -> bool:
    """Check if running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def _get_app_dir() -> Path | None:
    """Platform application-data root, or None when this platform has no adapter.

    The three-way OS branch that used to live here now lives behind the adapter
    seam (`ragtools.platform`), so adding a platform is adding an adapter
    rather than editing every module that happens to care about a path.
    """
    from ragtools.platform import PlatformUnsupported, adapter

    try:
        return adapter().app_dir()
    except PlatformUnsupported:
        return None


def get_data_dir() -> Path:
    """Resolve the data directory based on mode.

    Packaged/installed mode:
      Windows: %LOCALAPPDATA%/RAGTools
      macOS:   ~/Library/Application Support/RAGTools
      Linux:   $XDG_DATA_HOME/RAGTools or ~/.local/share/RAGTools
    Dev mode (not frozen): ./data (relative to CWD)

    Dev mode NEVER returns the installed-app directory, even if it exists.
    This prevents the source checkout from silently mounting installed data.
    """
    explicit = os.environ.get("RAG_DATA_DIR")
    if explicit:
        return Path(explicit)

    if is_packaged():
        app_dir = _get_app_dir()
        if app_dir:
            app_dir.mkdir(parents=True, exist_ok=True)
            return app_dir

    return Path("data").resolve()


def _find_config_path() -> Path | None:
    """Resolve TOML config file location. Returns None if no config file exists.

    Dev mode (not frozen) does NOT read the installed app's config.toml.
    """
    explicit = os.environ.get("RAG_CONFIG_PATH")
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        return None

    if is_packaged():
        app_dir = _get_app_dir()
        if app_dir:
            installed = app_dir / "config.toml"
            if installed.is_file():
                return installed

    dev = Path("ragtools.toml")
    if dev.is_file():
        return dev

    return None


def _default_env_file() -> str:
    """Resolve ragtools' OWN .env — never the current working directory's.

    A bare relative ``".env"`` is resolved by pydantic-settings against the CWD,
    so launching from an application repo made Settings load THAT application's
    .env. Its unrelated keys were then rejected by ``extra="forbid"``, and
    pydantic echoed each rejected VALUE into the ValidationError text — turning
    any unrelated app's .env into a secret leak in the caller's output.

    Mirrors :func:`_find_config_path`'s mode isolation: packaged mode anchors to
    the app dir, dev mode stays CWD-local.
    """
    explicit = os.environ.get("RAG_ENV_FILE")
    if explicit:
        return explicit

    if is_packaged():
        app_dir = _get_app_dir()
        if app_dir:
            return str(app_dir / ".env")

    return ".env"


def get_config_write_path() -> Path:
    """Get the correct path for writing config, even if it doesn't exist yet.

    In packaged mode, always uses %LOCALAPPDATA%/RAGTools/config.toml.
    In dev mode, uses ./ragtools.toml (CWD-relative).
    """
    explicit = os.environ.get("RAG_CONFIG_PATH")
    if explicit:
        return Path(explicit)

    existing = _find_config_path()
    if existing:
        return existing

    # No config exists yet — choose the correct location for this mode
    if is_packaged():
        app_dir = _get_app_dir()
        if app_dir:
            app_dir.mkdir(parents=True, exist_ok=True)
            return app_dir / "config.toml"

    return Path("ragtools.toml")


# --- TOML Loading ---


def _load_toml(path: Path) -> dict[str, Any]:
    """Read a TOML config file and return a flat dict suitable for Settings."""
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

    with open(path, "rb") as f:
        data = tomllib.load(f)

    flat: dict[str, Any] = {}
    for key, value in data.items():
        if key == "ignore" and isinstance(value, dict):
            if "patterns" in value:
                flat["ignore_patterns"] = value["patterns"]
            if "use_ragignore_files" in value:
                flat["use_ragignore_files"] = value["use_ragignore_files"]
        elif key == "version":
            flat["config_version"] = value
        elif key == "projects" and isinstance(value, list):
            # [[projects]] TOML array-of-tables → list of dicts
            flat["projects"] = value
        elif isinstance(value, dict):
            for sub_key, sub_value in value.items():
                flat[f"{key}_{sub_key}"] = sub_value
        else:
            flat[key] = value

    return flat


class TomlConfigSource(PydanticBaseSettingsSource):
    """Pydantic settings source that reads from a TOML config file."""

    def __init__(self, settings_cls: Type[BaseSettings]):
        super().__init__(settings_cls)
        config_path = _find_config_path()
        self._data: dict[str, Any] = _load_toml(config_path) if config_path else {}

    def get_field_value(self, field: Any, field_name: str) -> Tuple[Any, str, bool]:
        val = self._data.get(field_name)
        return val, field_name, val is not None

    def __call__(self) -> dict[str, Any]:
        return self._data


# --- Default Path Factories ---


def _resolve_data_dir() -> Path:
    """The single authoritative data directory (S2).

    ``RAG_DATA_DIR`` wins in EVERY mode (fixing the dev no-op where it was
    ignored for storage); a packaged install uses ``<app_dir>/data``; a source
    checkout uses ``<cwd>/data``. Always ABSOLUTE, so running from another
    directory can never split logs/pids/storage across two anchors.

    This is behaviour-preserving for the physical location of both the packaged
    (``<app_dir>/data/...``) and dev (``<cwd>/data/...``) layouts, but replaces
    the ~10 scattered ``Path(qdrant_path).parent`` derivations with one anchor.
    """
    explicit = os.environ.get("RAG_DATA_DIR")
    if explicit:
        return Path(explicit).resolve()
    if is_packaged():
        app_dir = _get_app_dir()
        if app_dir:
            return app_dir / "data"
    return Path("data").resolve()


def _default_data_dir() -> str:
    return str(_resolve_data_dir())


def _default_qdrant_path() -> str:
    return str(_resolve_data_dir() / "qdrant")


def _default_state_db() -> str:
    return str(_resolve_data_dir() / "index_state.db")


def _default_service_port() -> int:
    """Installed app uses 21420; dev/source mode uses 21421 to avoid port collision."""
    return 21420 if is_packaged() else 21421


# --- Settings ---


class Settings(BaseSettings):
    """Application settings loaded from environment variables with RAG_ prefix."""

    # Application data directory (S2) — the single authoritative anchor for
    # logs, PIDs, backups, markers, and the service registry. qdrant_path and
    # state_db default under it but can be overridden independently. Consumers
    # MUST use ``settings.data_dir`` rather than ``Path(qdrant_path).parent``.
    data_dir: str = Field(default_factory=_default_data_dir)

    # Qdrant local mode
    qdrant_path: str = Field(default_factory=_default_qdrant_path)
    collection_name: str = "markdown_kb"

    # Collection strategy (S6/S11). "shared" (default) preserves the v2 model —
    # one collection + payload filter — so nothing changes unless opted in.
    # "per_project" routes indexing/search to a UUID-derived collection per
    # project (the v3 model), available behind this flag until the owner elects
    # the cutover + re-index. Default-off = backward compatible, no test breakage.
    collection_strategy: str = "shared"

    # Storage backend (S3): embedded (default) | managed (S4) | external.
    storage_backend: str = "embedded"
    storage_url: str | None = None
    storage_api_key: str | None = None
    #: Explicit path to the Qdrant executable for managed mode. Without this,
    #: `find_qdrant_binary` searches the packaged app dir, sys.prefix/bin and
    #: <data_dir>/bin. `find_qdrant_binary` has always read this value; it was
    #: never a settings field, so it could not actually be configured.
    #: A path that is set but missing is an ERROR, not a silent downgrade to
    #: embedded — a typo must not look like "unsupported platform".
    qdrant_binary: str | None = None

    # Embedding
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384

    # Chunking
    chunk_size: int = 400
    chunk_overlap: int = 100

    # File support — OPT-IN. When False (default), only documentation
    # (md/README/text) is indexed, preserving the pre-2.6 documentation-only
    # behavior so upgrades don't silently balloon the index. Set True
    # (RAG_INDEX_SOURCE_CODE=1) to also index source code and config/data files.
    index_source_code: bool = False

    # Content (legacy — kept for backward compatibility with v1 config)
    content_root: str = "."

    # Projects (v2 — explicit multi-folder project configuration)
    projects: list[ProjectConfig] = Field(default_factory=list)
    # Shared-dependency catalog: declared once, selected by any project.
    dependencies: list[DependencyConfig] = Field(default_factory=list)
    config_version: int = 1

    # Retrieval
    top_k: int = 10
    score_threshold: float = 0.3

    # Index freshness — warn (in /api/status, /health, rag doctor) when the
    # index's last_indexed timestamp is older than this many hours.
    stale_index_hours: int = 24

    # State
    state_db: str = Field(default_factory=_default_state_db)

    # Ignore rules (global — apply to all projects)
    ignore_patterns: list[str] = Field(default_factory=list)
    use_ragignore_files: bool = True

    # Secret-file exclusion allowlist (gitignore globs) — re-include specific
    # secret-bearing paths a project genuinely needs indexed. Empty = strict.
    secret_allowlist: list[str] = Field(default_factory=list)

    # Service
    service_host: str = "127.0.0.1"
    service_port: int = Field(default_factory=_default_service_port)
    log_level: str = "INFO"

    # Startup
    startup_enabled: bool = False
    startup_delay: int = 30
    startup_open_browser: bool = False

    # Notifications
    desktop_notifications: bool = True
    notification_cooldown_seconds: float = 30.0

    # Backups of the SQLite state DB (taken before destructive operations).
    backup_keep: int = 10

    # Per-tool access control for the MCP server.
    # Core tools (search_knowledge_base, list_projects, index_status) are always
    # registered regardless of this dict — they define the product. Optional
    # tools split by tier:
    #   - Project tools (inspection + maintenance) default ON — this is the
    #     main agent workflow tier for "I'm working on project X" workflows.
    #     Writes have per-tool guards (confirm_token, cooldowns).
    #   - Debugging / diagnostics default OFF — operator-facing tools that
    #     would only distract an agent doing content work. User grants per-tool
    #     when troubleshooting.
    # Disabled tools are not registered, so they're invisible to the agent and
    # cost zero tokens.
    mcp_tools: dict[str, bool] = Field(default_factory=lambda: {
        # Project tools — enabled by default (primary agent workflow tier)
        "project_status":           True,
        "project_summary":          True,
        "list_project_files":       True,
        "get_project_ignore_rules": True,
        "preview_ignore_effect":    True,
        "run_index":                True,
        "reindex_project":          True,
        "add_project":              True,
        "set_project_mode":         True,
        "add_project_ignore_rule":  True,
        "remove_project_ignore_rule": True,
        # Shared dependencies — same tier as the project writes they complement:
        # onboarding a project that vendors a framework needs both halves.
        "list_dependencies":        True,
        "add_dependency":           True,
        "set_project_dependencies": True,
        "remove_dependency":        True,
        # Debugging / diagnostics — disabled by default (opt-in for operators)
        "service_status":           False,
        "recent_activity":          False,
        "tail_logs":                False,
        "crash_history":            False,
        "get_config":               False,
        "get_ignore_rules":         False,
        "get_paths":                False,
        "system_health":            False,
        "list_indexed_paths":       False,
    })

    model_config = {
        "env_prefix": "RAG_",
        "env_file": _default_env_file(),
        "env_file_encoding": "utf-8",
        # Never fail on keys we do not own. With the pydantic-settings default of
        # extra="forbid", a foreign .env raised ValidationError AND echoed the
        # offending VALUES into the error text, turning any unrelated app's .env
        # into a secret leak. Ignoring extras fails safe and keeps values silent.
        "extra": "ignore",
    }

    @model_validator(mode="after")
    def _anchor_data_dir(self):
        """Keep ``data_dir`` consistent with an explicit ``qdrant_path`` (S2).

        Backward-compat: if a caller sets ``qdrant_path`` but not ``data_dir``
        (as most existing tests and configs do), ``data_dir`` follows
        ``qdrant_path``'s parent — preserving the old "artifacts live next to
        the store" behaviour. An explicit ``data_dir`` (or ``RAG_DATA_DIR``)
        always wins and decouples the two.
        """
        fields_set = getattr(self, "model_fields_set", set())
        if "data_dir" not in fields_set and "qdrant_path" in fields_set:
            object.__setattr__(
                self, "data_dir", str(Path(self.qdrant_path).resolve().parent)
            )
        return self

    @model_validator(mode="after")
    def _adopt_legacy_dependency_paths(self):
        """Fold legacy per-project ``dependency_paths`` into the catalog.

        READ-ONLY and idempotent: loading a config must never rewrite it, so
        this only shapes the in-memory model. The TOML is normalised the next
        time the user saves, and ``dependency_paths`` keeps working either way
        (sync takes the union), so an existing install changes nothing until
        someone touches it.

        Two projects that declared the SAME folder collapse to one catalog
        entry — the deduplication the old model could only reach by accident,
        when both spellings happened to resolve to the same build identity.
        """
        by_path: dict[str, DependencyConfig] = {}
        for entry in self.dependencies:
            by_path[_dependency_path_key(entry.path)] = entry
        taken = {entry.id for entry in self.dependencies}
        adopted: list[DependencyConfig] = []

        for project in self.projects:
            for raw in list(project.dependency_paths or []):
                text = str(raw).strip()
                if not text:
                    continue
                candidate = Path(text)
                if not candidate.is_absolute():
                    candidate = Path(project.path) / candidate
                key = _dependency_path_key(str(candidate))

                entry = by_path.get(key)
                if entry is None:
                    entry = DependencyConfig(
                        id=_unique_dependency_id(candidate, taken),
                        name=candidate.name or "dependency",
                        path=str(candidate),
                    )
                    taken.add(entry.id)
                    by_path[key] = entry
                    adopted.append(entry)
                if entry.id not in project.dependencies:
                    project.dependencies.append(entry.id)
            # CONSUME the legacy field, exactly as `_migrate_legacy_index_source_code`
            # pops `index_source_code`. Leaving both populated makes the legacy
            # field a dead control: clearing it would not un-declare anything,
            # because the adopted link still stands — a silent no-op on the one
            # gesture a user would expect to work.
            project.dependency_paths = []

        if adopted:
            self.dependencies = list(self.dependencies) + adopted
        return self

    @property
    def has_explicit_projects(self) -> bool:
        """True if explicit projects are configured (v2 model)."""
        return len(self.projects) > 0

    def dependency(self, dependency_id: str) -> DependencyConfig | None:
        """Catalog lookup by id."""
        return next((d for d in self.dependencies if d.id == dependency_id), None)

    def dependency_paths_for(self, project: ProjectConfig) -> list[str]:
        """Every dependency root a project uses — catalog links plus legacy.

        Delegates so the scanner (which excludes these roots) and the sync
        (which indexes them) can never drift apart — see
        :func:`ragtools.dependency_catalog.resolve_project_dependency_paths`.
        Imported locally: the catalog module imports this one.
        """
        from ragtools.dependency_catalog import resolve_project_dependency_paths

        return resolve_project_dependency_paths(project, self.dependencies)

    @property
    def enabled_projects(self) -> list[ProjectConfig]:
        """Return only enabled projects."""
        return [p for p in self.projects if p.enabled]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ):
        """Priority: init kwargs > env vars > TOML config > .env > defaults."""
        return (
            init_settings,
            env_settings,
            TomlConfigSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    def get_qdrant_client(self):
        """Create a Qdrant client via the configured storage backend (S3).

        Routes through :func:`ragtools.storage.resolve_backend` so the embedded
        default is preserved while ``external`` (and later ``managed``) work
        through the same call. This is the single production constructor.
        """
        from ragtools.storage import resolve_backend

        return resolve_backend(self).client()

    @staticmethod
    def get_memory_client():
        """Create an in-memory Qdrant client for testing."""
        from qdrant_client import QdrantClient

        return QdrantClient(":memory:")


# --- Migration ---


def migrate_v1_to_v2(settings: Settings) -> list[ProjectConfig]:
    """Auto-discover projects from legacy content_root and create ProjectConfig entries.

    Called at runtime when config has no explicit [[projects]] but has a content_root
    that is not the default ".". Does NOT write back to TOML.
    """
    from ragtools.indexing.scanner import discover_projects

    content_root = settings.content_root
    if content_root == ".":
        return []

    try:
        discovered = discover_projects(content_root)
    except (OSError, FileNotFoundError):
        logger.warning("Legacy content_root '%s' not accessible, no projects discovered", content_root)
        return []

    projects = [
        ProjectConfig(
            id=pid,
            name=pid,
            path=str(project_path),
            enabled=True,
        )
        for pid, project_path in discovered.items()
    ]

    if projects:
        logger.info(
            "Auto-migrated %d projects from legacy content_root '%s'. "
            "Consider adding [[projects]] to your config.",
            len(projects), content_root,
        )

    return projects
