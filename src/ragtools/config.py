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
import sys
from pathlib import Path
from typing import Any, Tuple, Type

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

logger = logging.getLogger("ragtools.config")


# --- Project Configuration ---


class ProjectConfig(BaseModel):
    """Configuration for a single project folder."""

    id: str                                          # unique identifier, used in storage keys
    name: str = ""                                   # display name (defaults to id)
    path: str                                        # absolute path to project folder
    enabled: bool = True                             # skip if False
    ignore_patterns: list[str] = Field(default_factory=list)  # per-project ignore patterns

    def model_post_init(self, __context: Any) -> None:
        if not self.name:
            self.name = self.id


# --- Path Resolution ---


def is_packaged() -> bool:
    """Check if running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def get_data_dir() -> Path:
    """Resolve the data directory based on mode.

    Packaged/installed mode: %LOCALAPPDATA%/RAGTools
    Dev mode: ./data (relative to CWD)
    """
    explicit = os.environ.get("RAG_DATA_DIR")
    if explicit:
        return Path(explicit)

    if is_packaged() or (sys.platform == "win32" and os.environ.get("LOCALAPPDATA")):
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            installed_dir = Path(local_app_data) / "RAGTools"
            if is_packaged() or installed_dir.exists():
                installed_dir.mkdir(parents=True, exist_ok=True)
                return installed_dir

    return Path("data").resolve()


def _find_config_path() -> Path | None:
    """Resolve TOML config file location. Returns None if no config file exists."""
    explicit = os.environ.get("RAG_CONFIG_PATH")
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        return None

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            installed = Path(local_app_data) / "RAGTools" / "config.toml"
            if installed.is_file():
                return installed

    dev = Path("ragtools.toml")
    if dev.is_file():
        return dev

    return None


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


def _default_qdrant_path() -> str:
    if is_packaged():
        return str(get_data_dir() / "data" / "qdrant")
    return "data/qdrant"


def _default_state_db() -> str:
    if is_packaged():
        return str(get_data_dir() / "data" / "index_state.db")
    return "data/index_state.db"


# --- Settings ---


class Settings(BaseSettings):
    """Application settings loaded from environment variables with RAG_ prefix."""

    # Qdrant local mode
    qdrant_path: str = Field(default_factory=_default_qdrant_path)
    collection_name: str = "markdown_kb"

    # Embedding
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384

    # Chunking
    chunk_size: int = 400
    chunk_overlap: int = 100

    # Content (legacy — kept for backward compatibility with v1 config)
    content_root: str = "."

    # Projects (v2 — explicit multi-folder project configuration)
    projects: list[ProjectConfig] = Field(default_factory=list)
    config_version: int = 1

    # Retrieval
    top_k: int = 10
    score_threshold: float = 0.3

    # State
    state_db: str = Field(default_factory=_default_state_db)

    # Ignore rules (global — apply to all projects)
    ignore_patterns: list[str] = Field(default_factory=list)
    use_ragignore_files: bool = True

    # Service
    service_host: str = "127.0.0.1"
    service_port: int = 21420
    log_level: str = "INFO"

    # Startup
    startup_enabled: bool = False
    startup_delay: int = 30
    startup_open_browser: bool = False

    model_config = {"env_prefix": "RAG_", "env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def has_explicit_projects(self) -> bool:
        """True if explicit projects are configured (v2 model)."""
        return len(self.projects) > 0

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
        """Create a Qdrant client in local persistent mode."""
        from qdrant_client import QdrantClient

        Path(self.qdrant_path).mkdir(parents=True, exist_ok=True)
        return QdrantClient(path=self.qdrant_path)

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
