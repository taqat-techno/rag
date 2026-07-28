"""The scale warning must describe the risk that actually exists.

The reported symptom was a v3.0.1 install showing 83,922 points in one
collection with ``scale: over``. That warning was correct. Two things about it
were not:

* it recommended "migrating Qdrant to server mode", and no CLI command, API
  field or admin-panel control could do that — `storage_backend` had no setter
  anywhere, so the only remedy offered was one the user could not perform;
* it is computed from the TOTAL across collections, which is the wrong
  arithmetic the moment there is more than one. Under the per-project layout —
  the very layout that fixes the problem — twenty-five comfortable collections
  would sum past the ceiling and warn forever.

The second is the trap worth naming: adopting per-project collections without
fixing this converts a true warning into a permanent false alarm, which teaches
the operator to ignore the one signal that was telling the truth.
"""

from __future__ import annotations

from ragtools.service.owner import (
    _QDRANT_LOCAL_HARD_WARN,
    _QDRANT_LOCAL_SOFT_WARN,
    compute_scale_warning,
    governing_collection,
)


class _Caps:
    def __init__(self, hnsw: bool):
        self.hnsw = hnsw


SERVER = _Caps(True)
EMBEDDED = _Caps(False)


# --- which number the ceiling applies to -----------------------------------


def test_the_governing_count_is_the_largest_collection_not_the_sum():
    per = [{"name": "proj_a", "points": 9_000},
           {"name": "proj_b", "points": 8_000},
           {"name": "proj_c", "points": 7_000}]

    points, name, count = governing_collection(per)

    assert (points, name, count) == (9_000, "proj_a", 3)
    assert points < sum(e["points"] for e in per), "still summing"


def test_many_small_collections_are_not_over_the_limit():
    """24,000 points across three collections is three fast scans, not one
    slow one. Summing them would warn about a problem that does not exist."""
    per = [{"name": f"proj_{i}", "points": 8_000} for i in range(3)]
    points, name, count = governing_collection(per)

    assert compute_scale_warning(points, EMBEDDED, collection=name,
                                 collection_count=count)["level"] == "ok"


def test_one_oversized_collection_still_warns_even_when_others_are_small():
    """The converse. A project that vendors a framework can exceed the ceiling
    alone while the average across collections looks comfortable."""
    per = [{"name": "vendored", "points": 81_000},
           {"name": "small_a", "points": 400},
           {"name": "small_b", "points": 300}]
    points, name, count = governing_collection(per)
    record = compute_scale_warning(points, EMBEDDED, collection=name,
                                   collection_count=count)

    assert record["level"] == "over"
    assert "vendored" in record["message"], "the message does not say which one"


def test_an_empty_index_has_no_governing_collection():
    assert governing_collection([]) == (0, "", 0)
    assert compute_scale_warning(0, EMBEDDED)["level"] == "ok"


# --- the engine decides whether there is a ceiling at all ------------------


def test_a_server_engine_has_no_local_mode_ceiling():
    """HNSW removes the limit the warning describes; repeating it there trains
    the operator to ignore it."""
    record = compute_scale_warning(500_000, SERVER)

    assert record["level"] == "ok"
    assert record["message"] == ""
    assert record["engine"] == "server"


def test_the_embedded_engine_still_warns_at_the_documented_thresholds():
    assert compute_scale_warning(_QDRANT_LOCAL_SOFT_WARN - 1, EMBEDDED)["level"] == "ok"
    assert compute_scale_warning(_QDRANT_LOCAL_SOFT_WARN, EMBEDDED)["level"] == "approaching"
    assert compute_scale_warning(_QDRANT_LOCAL_HARD_WARN, EMBEDDED)["level"] == "over"


def test_omitting_capabilities_assumes_the_weakest_engine():
    """The safe default: unknown engine must not silently mean "no limit"."""
    assert compute_scale_warning(_QDRANT_LOCAL_HARD_WARN)["level"] == "over"


# --- the advice must be actionable ----------------------------------------


def test_the_warning_only_recommends_things_the_product_can_do():
    """Advice that cannot be followed reads as a broken product.

    `storage_backend` had no setter in the CLI, the API or the admin panel, so
    "migrate Qdrant to server mode" named a capability the installed product
    could not activate.
    """
    message = compute_scale_warning(_QDRANT_LOCAL_HARD_WARN, EMBEDDED)["message"]

    assert "migrating Qdrant to server mode" not in message
    assert "rag storage backend" in message, (
        "the remedy is not expressed as a command the user can run"
    )


def test_every_command_the_warning_names_actually_exists():
    """Pins the advice to the CLI. A renamed command would otherwise leave the
    warning quietly recommending something that no longer works."""
    import re

    from typer.main import get_command

    from ragtools.cli import app

    message = compute_scale_warning(_QDRANT_LOCAL_HARD_WARN, EMBEDDED)["message"]
    referenced = re.findall(r"`rag ([a-z]+(?: [a-z]+)*)", message)
    assert referenced, "the warning names no command at all"

    root = get_command(app)
    for phrase in referenced:
        group, _, _rest = phrase.partition(" ")
        assert group in root.commands, (
            f"the scale warning recommends `rag {phrase}`, but `rag {group}` "
            f"is not a command. Available: {sorted(root.commands)}"
        )


# --- naming the collection -------------------------------------------------


def test_a_single_collection_is_not_named_in_the_message():
    """With one collection the name is noise."""
    record = compute_scale_warning(_QDRANT_LOCAL_HARD_WARN, EMBEDDED,
                                   collection="markdown_kb", collection_count=1)

    assert record["message"].startswith("Collection has")


def test_the_offending_collection_is_named_when_there_are_several():
    record = compute_scale_warning(_QDRANT_LOCAL_HARD_WARN, EMBEDDED,
                                   collection="proj_abc", collection_count=9)

    assert "proj_abc" in record["message"]
