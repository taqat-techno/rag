"""The Dashboard must not answer two different questions with one number.

The installed v3.4 machine showed "14 projects" above a table listing 15, and
both were correct: the tile came from ``SELECT DISTINCT project_id FROM
file_state`` (projects with at least one indexed FILE) while the table iterated
``config.toml`` (projects that EXIST). A project that is configured, enabled,
and has a real folder — but whose files never landed — appears in one and not
the other, with nothing on screen saying so.

That is not an off-by-one. It is two sources of truth reporting under one
label, and patching the 14 to a 15 would have hidden the real gap: the project
in question had lost 41,832 points and nobody could tell from the dashboard.

The same page rendered ``searchable`` and ``chunks`` as separate tiles holding
the identical number (26,713 twice), because one is the live vector count and
the other is the state DB's chunk total and they happened to agree.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

from ragtools.config import ProjectConfig, Settings
from ragtools.service.owner import QdrantOwner


@pytest.fixture
def owner():
    """Three projects; only two of them have anything indexable."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        configs = []
        for name in ("alpha", "beta"):
            d = root / name
            d.mkdir()
            (d / "doc.md").write_text(
                f"# {name}\n\nLedger reconciliation for {name}.\n", encoding="utf-8")
            configs.append(ProjectConfig(id=name, path=str(d), mode="docs"))

        # Configured, enabled, real folder, ZERO eligible files — the shape
        # that produced the 14-vs-15.
        empty = root / "gamma"
        empty.mkdir()
        (empty / "image.png").write_bytes(b"not markdown")
        configs.append(ProjectConfig(id="gamma", path=str(empty), mode="docs"))

        settings = Settings(
            content_root=str(root),
            qdrant_path=str(root / "qdrant"),
            state_db=str(root / "state.db"),
            data_dir=str(root / "data"),
            collection_strategy="per_project",
            projects=configs,
        )
        o = QdrantOwner(settings=settings, client=Settings.get_memory_client())
        o.run_full_index()
        yield o
        o.close()


def test_status_reports_configured_and_indexed_as_different_numbers(owner):
    """The two questions get two answers, both labelled."""
    status = owner.get_status()

    assert status["projects_configured"] == 3, status
    assert status["projects_indexed"] == 2, status
    assert status["projects_configured"] != status["projects_indexed"], (
        "the fixture is meant to expose the gap; if these agree the test proves nothing"
    )


def test_the_dashboard_tile_does_not_contradict_the_table_beneath_it(owner):
    """A tile saying 14 above a table of 15 is the whole defect."""
    from ragtools.service import pages

    html = pages.ui_dash_status()
    tiles = re.findall(r"<strong>([\d,]+)</strong>\s*<span>([^<]+)</span>", html)
    labels = {label.strip(): value for value, label in tiles}

    projects_label = [k for k in labels if "project" in k.lower()]
    assert projects_label, f"no projects tile found in {labels}"

    rendered = labels[projects_label[0]].replace(",", "")
    # Whatever the tile shows, the label must make clear WHICH question it
    # answers — a bare "projects" over a differing table is the ambiguity.
    assert ("configured" in projects_label[0].lower()
            or "indexed" in projects_label[0].lower()
            or rendered == "3"), (
        f"tile {projects_label[0]!r}={rendered!r} is ambiguous against 3 configured "
        f"/ 2 indexed"
    )


def test_searchable_and_chunks_are_not_the_same_number_under_two_labels(owner):
    """26,713 appeared twice on one row, as `searchable` and as `chunks`.

    They are the live vector count and the recorded chunk total. They agree on
    a healthy index, which is exactly why presenting both as independent facts
    is misleading — four tiles carried three dimensions.
    """
    from ragtools.service import pages

    html = pages.ui_dash_status()
    tiles = re.findall(r"<strong>([\d,]+)</strong>\s*<span>([^<]+)</span>", html)

    by_value: dict[str, list[str]] = {}
    for value, label in tiles:
        by_value.setdefault(value, []).append(label.strip().lower())

    for value, labels in by_value.items():
        assert not ({"searchable", "chunks"} <= set(labels)), (
            f"{value} is presented under both 'searchable' and 'chunks' — one "
            f"quantity, two labels: {tiles}"
        )


def test_a_project_with_no_eligible_files_is_not_reported_as_never_indexed(owner):
    """`Not indexed yet` rendered identically for a project that was scanned and
    legitimately had nothing, one whose folder does not exist, one that is
    disabled, and one whose rebuild FAILED. Four causes, four remedies, one
    string."""
    states = {p["id"]: p for p in owner.get_status_projects()}

    assert states["gamma"]["state"] == "no_eligible_files", states["gamma"]
    assert states["alpha"]["state"] == "indexed", states["alpha"]
    assert states["beta"]["state"] == "indexed", states["beta"]
