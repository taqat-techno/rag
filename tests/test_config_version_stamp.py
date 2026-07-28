"""No production writer may lower the config schema version.

This is the defect that made wiring the v2->v3 migration pointless on its own.
`_save_projects_to_toml` wrote ``version = 2`` unconditionally, and it is reached
from sixteen production call sites — every project add, remove, edit, mode
change, ignore rule and dependency change, from the CLI, the admin panel and MCP
alike. A migrated config was therefore demoted by the user's very next edit and
re-migrated on the following boot, forever, which also made "has this been
migrated?" unanswerable from the version field.

Measured before the fix::

    start        : 2   strategy: None
    post-migrate : 3   strategy: per_project
    post-UI-edit : 2   strategy: per_project     <- the writer
    next boot    : migration runs again, and rewrites the file again

The rule these tests pin is narrow and mechanical: **writers preserve the
declared version and never invent one.** Deciding the version is the migrator's
job, and it is the only thing allowed to change it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ragtools.config import CONFIG_VERSION


tomllib = pytest.importorskip(
    "tomllib" if sys.version_info >= (3, 11) else "tomli")
tomli_w = pytest.importorskip("tomli_w")


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Point both writers at a throwaway config and hand back its path."""
    path = tmp_path / "config.toml"
    import ragtools.config as cfg

    monkeypatch.setattr(cfg, "get_config_write_path", lambda: path)
    return path


def read(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def write(path: Path, doc: dict) -> None:
    path.write_bytes(tomli_w.dumps(doc).encode("utf-8"))


# --- the single source -----------------------------------------------------


def test_the_schema_version_has_exactly_one_definition():
    """`upgrade.migrate` must re-export it, not redefine it.

    Two literals is how the writers and the migrator came to disagree.
    """
    from ragtools.upgrade.migrate import CONFIG_VERSION as migrate_version

    assert migrate_version is CONFIG_VERSION


def test_no_production_source_file_hardcodes_a_config_version():
    """A literal assignment to the TOML `version` key is the defect itself.

    Parsed rather than grepped. A textual scan also matches the comment that
    explains the old defective line, so the first version of this test failed on
    its own documentation — and the obvious repair, skipping comments, would
    still have matched a docstring. The syntax tree only sees code.
    """
    import ast

    root = Path(__file__).resolve().parents[1] / "src" / "ragtools"
    offenders: list[str] = []

    def literal_int(node) -> bool:
        return isinstance(node, ast.Constant) and isinstance(node.value, int)

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        where = path.relative_to(root)
        for node in ast.walk(tree):
            # doc["version"] = 2
            if isinstance(node, ast.Assign) and literal_int(node.value):
                for target in node.targets:
                    if (isinstance(target, ast.Subscript)
                            and isinstance(target.slice, ast.Constant)
                            and target.slice.value == "version"):
                        offenders.append(f"{where}:{node.lineno}: ['version'] = "
                                         f"{node.value.value}")
            # doc.setdefault("version", 2)
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "setdefault"
                    and len(node.args) == 2
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "version"
                    and literal_int(node.args[1])):
                offenders.append(f"{where}:{node.lineno}: setdefault('version', "
                                 f"{node.args[1].value})")

    assert not offenders, "config version is hard-coded:\n" + "\n".join(offenders)


# --- the writers ------------------------------------------------------------


def test_saving_projects_does_not_demote_a_migrated_config(config_file):
    """The exact reported sequence: migrate, then edit a project."""
    from ragtools.config import ProjectConfig
    from ragtools.service.pages import _save_projects_to_toml

    write(config_file, {"version": CONFIG_VERSION,
                        "collection_strategy": "per_project",
                        "storage_backend": "embedded",
                        "projects": [{"id": "p", "path": "/tmp/p", "mode": "docs"}]})

    _save_projects_to_toml([ProjectConfig(id="p", path="/tmp/p", mode="code")])

    after = read(config_file)
    assert after["version"] == CONFIG_VERSION, "the writer demoted the config"
    assert after["collection_strategy"] == "per_project", "v3 keys were dropped"
    assert after["projects"][0]["mode"] == "code", "the actual edit was lost"


def test_saving_settings_does_not_demote_a_migrated_config(config_file):
    from ragtools.service.pages import _update_toml_config

    write(config_file, {"version": CONFIG_VERSION, "storage_backend": "embedded"})
    _update_toml_config("startup", {"startup_delay": 45})

    after = read(config_file)
    assert after["version"] == CONFIG_VERSION
    assert after["startup"]["startup_delay"] == 45


def test_a_v2_config_is_not_silently_promoted_by_an_edit(config_file):
    """The converse trap. A writer that stamped the CURRENT version would claim
    a migration that never ran, and the v3 keys would still be absent."""
    from ragtools.config import ProjectConfig
    from ragtools.service.pages import _save_projects_to_toml

    write(config_file, {"version": 2,
                        "projects": [{"id": "p", "path": "/tmp/p", "mode": "docs"}]})

    _save_projects_to_toml([ProjectConfig(id="p", path="/tmp/p", mode="general")])

    after = read(config_file)
    assert after["version"] == 2, (
        "an edit promoted a v2 config without migrating it, so the v3 keys "
        "would stay missing while the version claimed otherwise"
    )
    assert "collection_strategy" not in after


def test_a_brand_new_config_is_created_at_the_current_version(config_file):
    """A file this release creates from nothing has no legacy to preserve."""
    from ragtools.config import ProjectConfig
    from ragtools.service.pages import _save_projects_to_toml

    assert not config_file.exists()
    _save_projects_to_toml([ProjectConfig(id="p", path="/tmp/p")])

    assert read(config_file)["version"] == CONFIG_VERSION


# --- the round trip that used to oscillate ---------------------------------


def test_migrate_then_edit_then_boot_converges(config_file):
    """Migration followed by an edit followed by another boot must settle.

    Before the fix this cycled 3 -> 2 -> 3 -> 2 indefinitely, rewriting the
    file on every start.
    """
    from ragtools.config import ProjectConfig
    from ragtools.service.pages import _save_projects_to_toml
    from ragtools.upgrade.migrate import migrate_config

    write(config_file, {"version": 2,
                        "projects": [{"id": "p", "path": "/tmp/p", "mode": "docs"}]})

    first = migrate_config(read(config_file))
    write(config_file, first.document)
    assert read(config_file)["version"] == CONFIG_VERSION

    _save_projects_to_toml([ProjectConfig(id="p", path="/tmp/p", mode="code")])

    second = migrate_config(read(config_file))
    assert second.changed is False, (
        "the config still needs migrating after an ordinary edit — "
        "the writer and the migrator disagree"
    )
    assert read(config_file)["version"] == CONFIG_VERSION
