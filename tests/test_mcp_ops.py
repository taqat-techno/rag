"""Tests for the ragtools-ops MCP server and its shared helpers.

Covers:
  - ``mcp_common`` helpers: envelope, require_proxy, proxy_get/post
  - ``service.logs.tail`` with whitelist + limit behaviour
  - ``mcp_ops_server`` tools exercised with a mocked McpState:
      * tools that need proxy mode refuse cleanly when degraded
      * tools that work from filesystem still function in degraded mode
      * tool responses always carry the ok/mode/as_of envelope
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ragtools.config import Settings
from ragtools.integration import mcp_common, mcp_server as mcp_ops_server
from ragtools.integration.mcp_common import McpState, err, ok, require_proxy


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        qdrant_path=str(tmp_path / "qdrant"),
        state_db=str(tmp_path / "state.db"),
    )


# ---------------------------------------------------------------------------
# mcp_common envelope helpers
# ---------------------------------------------------------------------------


def test_ok_envelope_has_required_fields():
    s = McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s"))
    s.mode = "proxy"
    r = ok(s, {"hello": "world"})
    assert r["ok"] is True
    assert r["mode"] == "proxy"
    assert "as_of" in r
    assert r["data"] == {"hello": "world"}


def test_err_envelope_has_required_fields_and_optional_hint():
    s = McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s"))
    s.mode = "degraded"
    r = err(s, "boom", hint="start the service")
    assert r["ok"] is False
    assert r["mode"] == "degraded"
    assert r["error"] == "boom"
    assert r["hint"] == "start the service"


def test_err_without_hint_omits_hint_key():
    s = McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s"))
    r = err(s, "boom")
    assert "hint" not in r


def test_require_proxy_passes_when_in_proxy_mode():
    s = McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s"))
    s.mode = "proxy"
    s.http = MagicMock()  # required by the guard
    assert require_proxy(s, "tool_x") is None


def test_require_proxy_returns_error_when_degraded():
    s = McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s"))
    s.mode = "degraded"
    r = require_proxy(s, "tool_x")
    assert r is not None
    assert r["ok"] is False
    assert "tool_x" in r["error"]
    assert "service" in r["hint"].lower()


# ---------------------------------------------------------------------------
# Log tail — whitelist, limit clamping, missing-file handling
# ---------------------------------------------------------------------------


def test_tail_refuses_unknown_source(tmp_path):
    from ragtools.service.logs import tail
    s = _settings(tmp_path)
    r = tail(s, source="/etc/passwd")
    assert "error" in r
    assert "available_sources" in r


def test_tail_returns_empty_when_log_missing(tmp_path):
    from ragtools.service.logs import tail
    s = _settings(tmp_path)
    r = tail(s, source="service")
    assert r["source"] == "service"
    assert r["lines"] == []
    assert "note" in r


def test_tail_returns_last_n_lines(tmp_path):
    from ragtools.service.logs import tail
    s = _settings(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "service.log").write_text("\n".join(f"line-{i}" for i in range(100)))

    r = tail(s, source="service", limit=5)
    assert r["lines"] == ["line-95", "line-96", "line-97", "line-98", "line-99"]
    assert r["truncated"] is True
    assert r["total_lines_in_file"] == 100


def test_tail_clamps_limit_to_500(tmp_path):
    from ragtools.service.logs import tail
    s = _settings(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "service.log").write_text("\n".join(f"x{i}" for i in range(1000)))

    # limit is capped at 500 — pass an obviously bigger value
    r = tail(s, source="service", limit=99999)
    assert len(r["lines"]) == 500


def test_tail_clamps_limit_to_minimum_1(tmp_path):
    from ragtools.service.logs import tail
    s = _settings(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "service.log").write_text("a\nb\nc\n")

    r = tail(s, source="service", limit=0)
    assert len(r["lines"]) == 1


# ---------------------------------------------------------------------------
# Ops server tools — proxy/degraded branches
# ---------------------------------------------------------------------------


@pytest.fixture
def degraded_state(tmp_path, monkeypatch):
    """Force the ops-server module-level state into 'degraded' mode so
    tests don't accidentally hit a live service."""
    s = McpState(_settings(tmp_path))
    s.mode = "degraded"
    s.http = None
    monkeypatch.setattr(mcp_ops_server, "_ops_state", s)
    # Reset per-process write cooldowns so tests don't leak state between runs.
    from ragtools.integration.mcp_common import WriteCooldown
    monkeypatch.setattr(mcp_ops_server, "_write_cooldown", WriteCooldown())
    return s


@pytest.fixture
def proxy_state(tmp_path, monkeypatch):
    """Fake proxy mode with a mocked httpx client."""
    s = McpState(_settings(tmp_path))
    s.mode = "proxy"
    s.http = MagicMock()
    monkeypatch.setattr(mcp_ops_server, "_ops_state", s)
    from ragtools.integration.mcp_common import WriteCooldown
    monkeypatch.setattr(mcp_ops_server, "_write_cooldown", WriteCooldown())
    return s


def _mock_200(client: MagicMock, payload: dict) -> None:
    client.get.return_value = MagicMock(status_code=200, json=lambda: payload)


def test_service_status_refuses_in_degraded_mode(degraded_state):
    r = mcp_ops_server.service_status()
    assert r["ok"] is False
    assert "not running" in r["error"].lower()
    assert "hint" in r


def test_service_status_merges_status_and_watcher(proxy_state):
    calls = []

    def fake_get(path, **kw):
        calls.append(path)
        if path == "/api/status":
            return MagicMock(status_code=200, json=lambda: {"points_count": 42})
        if path == "/api/watcher/status":
            return MagicMock(status_code=200, json=lambda: {"running": True})
        return MagicMock(status_code=500, text="no")

    proxy_state.http.get.side_effect = fake_get

    r = mcp_ops_server.service_status()
    assert r["ok"] is True
    assert r["data"]["status"] == {"points_count": 42}
    assert r["data"]["watcher"] == {"running": True}
    assert "/api/status" in calls
    assert "/api/watcher/status" in calls


def test_recent_activity_forwards_and_filters(proxy_state):
    _mock_200(proxy_state.http, {
        "events": [
            {"id": 1, "level": "info",    "message": "a"},
            {"id": 2, "level": "warning", "message": "b"},
            {"id": 3, "level": "error",   "message": "c"},
        ]
    })
    r = mcp_ops_server.recent_activity(level="error")
    assert r["ok"] is True
    assert r["data"]["count"] == 1
    assert r["data"]["events"][0]["level"] == "error"


def test_recent_activity_clamps_limit(proxy_state):
    _mock_200(proxy_state.http, {"events": []})
    mcp_ops_server.recent_activity(limit=99999)
    # proxy_get receives the clamped value in params
    call = proxy_state.http.get.call_args
    assert call.kwargs["params"]["limit"] == 200


def test_tail_logs_works_in_degraded_mode(degraded_state, tmp_path):
    log_dir = Path(degraded_state.settings.qdrant_path).parent / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "service.log").write_text("hello\nworld\n")

    r = mcp_ops_server.tail_logs(source="service", limit=10)
    assert r["ok"] is True
    assert r["data"]["lines"] == ["hello", "world"]


def test_tail_logs_rejects_bad_source_in_degraded_mode(degraded_state):
    r = mcp_ops_server.tail_logs(source="/etc/passwd")
    assert r["ok"] is False
    assert "hint" in r  # contains valid-sources list


def test_crash_history_works_in_degraded_mode(degraded_state):
    # No marker files created → empty result, still ok
    r = mcp_ops_server.crash_history()
    assert r["ok"] is True
    assert r["data"]["count"] == 0


def test_crash_history_returns_items_when_marker_present(degraded_state, tmp_path):
    import json
    log_dir = Path(degraded_state.settings.qdrant_path).parent / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "last_crash.json").write_text(json.dumps({
        "timestamp": "2026-04-18T10:00:00Z",
        "exception_type": "RuntimeError",
        "message": "boom",
    }))

    r = mcp_ops_server.crash_history()
    assert r["ok"] is True
    assert r["data"]["count"] == 1
    assert r["data"]["items"][0]["kind"] == "service_crash"


def test_get_config_works_in_degraded_mode(degraded_state):
    """Config tool uses filesystem fallback — must always succeed even
    when the service is down."""
    r = mcp_ops_server.get_config()
    assert r["ok"] is True
    assert r["data"]["embedding_model"]  # non-empty
    assert "chunk_size" in r["data"]


def test_get_config_in_proxy_mode_forwards(proxy_state):
    _mock_200(proxy_state.http, {"chunk_size": 400, "top_k": 10})
    r = mcp_ops_server.get_config()
    assert r["ok"] is True
    assert r["data"]["chunk_size"] == 400


def test_get_ignore_rules_works_in_degraded_mode(degraded_state):
    r = mcp_ops_server.get_ignore_rules()
    assert r["ok"] is True
    assert "built_in" in r["data"]
    assert isinstance(r["data"]["built_in"], list)


def test_get_paths_returns_absolute_paths(degraded_state):
    r = mcp_ops_server.get_paths()
    assert r["ok"] is True
    for key in ("data_dir", "qdrant_path", "state_db", "logs_dir", "backups_dir"):
        assert key in r["data"]
        assert Path(r["data"][key]).is_absolute()


def test_system_health_refuses_in_degraded_mode(degraded_state):
    r = mcp_ops_server.system_health()
    assert r["ok"] is False
    assert "hint" in r


def test_list_indexed_paths_works_in_degraded_mode_with_empty_db(degraded_state):
    # No state DB → clean error
    r = mcp_ops_server.list_indexed_paths()
    assert r["ok"] is False
    assert "state db" in r["error"].lower()


def test_list_indexed_paths_reads_state_db(degraded_state, tmp_path):
    import sqlite3
    db_path = Path(degraded_state.settings.state_db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE file_state (file_path TEXT PRIMARY KEY, "
            "project_id TEXT, file_hash TEXT, mtime REAL)"
        )
        conn.executemany(
            "INSERT INTO file_state VALUES (?, ?, ?, ?)",
            [
                ("a/x.md", "a", "h1", 1.0),
                ("a/y.md", "a", "h2", 2.0),
                ("b/z.md", "b", "h3", 3.0),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    # No project filter
    r = mcp_ops_server.list_indexed_paths()
    assert r["ok"] is True
    assert r["data"]["count"] == 3

    # Project filter
    r = mcp_ops_server.list_indexed_paths(project="a")
    assert r["ok"] is True
    assert r["data"]["count"] == 2
    assert all(row["project"] == "a" for row in r["data"]["files"])


# ---------------------------------------------------------------------------
# Envelope discipline — every tool response must carry ok/mode/as_of
# ---------------------------------------------------------------------------


def test_every_tool_returns_envelope_shape(degraded_state, tmp_path):
    """Run each tool and assert the response carries the mandatory envelope
    fields, so agents can always branch on ``ok`` and reason about
    ``mode`` / ``as_of``."""
    log_dir = Path(degraded_state.settings.qdrant_path).parent / "logs"
    log_dir.mkdir(parents=True)

    tools_to_call = [
        ("service_status", lambda: mcp_ops_server.service_status()),
        ("recent_activity", lambda: mcp_ops_server.recent_activity()),
        ("tail_logs", lambda: mcp_ops_server.tail_logs(source="service")),
        ("crash_history", lambda: mcp_ops_server.crash_history()),
        ("get_config", lambda: mcp_ops_server.get_config()),
        ("get_ignore_rules", lambda: mcp_ops_server.get_ignore_rules()),
        ("get_paths", lambda: mcp_ops_server.get_paths()),
        ("system_health", lambda: mcp_ops_server.system_health()),
        ("list_indexed_paths", lambda: mcp_ops_server.list_indexed_paths()),
    ]
    for name, invoke in tools_to_call:
        r = invoke()
        assert "ok" in r, f"{name} missing 'ok'"
        assert "mode" in r, f"{name} missing 'mode'"
        assert "as_of" in r, f"{name} missing 'as_of'"
        assert r["mode"] in ("proxy", "degraded", "direct"), f"{name} mode={r['mode']}"


def test_ops_server_declares_all_phase1_tools():
    """Compile-time inventory check for diagnostics."""
    expected = {
        "service_status",
        "recent_activity",
        "tail_logs",
        "crash_history",
        "get_config",
        "get_ignore_rules",
        "get_paths",
        "system_health",
        "list_indexed_paths",
    }
    for name in expected:
        assert hasattr(mcp_ops_server, name), f"ops server missing {name}"


def test_ops_server_declares_all_phase2_tools():
    """Compile-time inventory check for project-inspection (Family A)."""
    expected = {
        "project_status",
        "project_summary",
        "list_project_files",
        "get_project_ignore_rules",
        "preview_ignore_effect",
    }
    for name in expected:
        assert hasattr(mcp_ops_server, name), f"ops server missing {name}"


# ---------------------------------------------------------------------------
# Family A — project-inspection tools
# ---------------------------------------------------------------------------


def test_project_status_refuses_in_degraded_mode(degraded_state):
    r = mcp_ops_server.project_status("alpha")
    assert r["ok"] is False
    assert "hint" in r


def test_project_status_forwards_in_proxy_mode(proxy_state):
    _mock_200(proxy_state.http, {
        "project_id": "alpha", "files": 10, "chunks": 40, "enabled": True,
    })
    r = mcp_ops_server.project_status("alpha")
    assert r["ok"] is True
    assert r["data"]["project_id"] == "alpha"
    assert r["data"]["files"] == 10
    call = proxy_state.http.get.call_args
    assert call.args[0] == "/api/projects/alpha/status"


def test_project_summary_forwards_with_top_files_param(proxy_state):
    _mock_200(proxy_state.http, {"project_id": "alpha", "top_files": []})
    mcp_ops_server.project_summary("alpha", top_files=5)
    call = proxy_state.http.get.call_args
    assert call.kwargs["params"]["top_files"] == 5


def test_list_project_files_forwards_limit(proxy_state):
    _mock_200(proxy_state.http, {"project_id": "alpha", "files": [], "count": 0})
    mcp_ops_server.list_project_files("alpha", limit=50)
    call = proxy_state.http.get.call_args
    assert call.kwargs["params"]["limit"] == 50


def test_get_project_ignore_rules_forwards(proxy_state):
    _mock_200(proxy_state.http, {
        "project_id": "alpha",
        "built_in": ["*.pyc"],
        "config_global": [],
        "config_project": ["archive/"],
    })
    r = mcp_ops_server.get_project_ignore_rules("alpha")
    assert r["ok"] is True
    assert r["data"]["config_project"] == ["archive/"]


def test_preview_ignore_effect_posts_pattern(proxy_state):
    proxy_state.http.post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"project_id": "alpha", "pattern": "tmp/", "would_exclude": ["tmp/x.md"], "count": 1},
    )
    r = mcp_ops_server.preview_ignore_effect("alpha", "tmp/")
    assert r["ok"] is True
    assert r["data"]["count"] == 1

    # Verify we actually POSTed the pattern as JSON body (not as query string)
    call = proxy_state.http.post.call_args
    assert call.args[0] == "/api/projects/alpha/ignore/preview"
    assert call.kwargs["json"] == {"pattern": "tmp/"}


def test_preview_ignore_effect_handles_non_200(proxy_state):
    proxy_state.http.post.return_value = MagicMock(
        status_code=422, text='{"detail":"Pattern is required"}',
    )
    r = mcp_ops_server.preview_ignore_effect("alpha", "")
    assert r["ok"] is False
    assert "422" in r["error"]


# ---------------------------------------------------------------------------
# Family B — project-scoped writes
# ---------------------------------------------------------------------------


def test_run_index_refuses_in_degraded_mode(degraded_state):
    r = mcp_ops_server.run_index("alpha")
    assert r["ok"] is False
    assert "hint" in r


def test_run_index_posts_incremental(proxy_state):
    proxy_state.http.post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"stats": {"indexed": 3, "skipped": 10, "deleted": 0}},
    )
    r = mcp_ops_server.run_index("alpha")
    assert r["ok"] is True
    call = proxy_state.http.post.call_args
    assert call.args[0] == "/api/index"
    assert call.kwargs["json"] == {"project": "alpha", "full": False}


def test_reindex_project_requires_confirm_token_equal_to_project():
    """Safety guard: an injection like reindex_project('other', confirm_token='yes')
    must be rejected."""
    s = McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s"))
    s.mode = "proxy"
    s.http = MagicMock()
    # Replace module state directly — we want to test the guard before any
    # HTTP gets dispatched.
    import ragtools.integration.mcp_server as m
    orig = m._ops_state
    m._ops_state = s
    try:
        r = m.reindex_project("alpha", confirm_token="yes")
        assert r["ok"] is False
        assert "confirm_token" in r["error"]
        # And verify the HTTP layer was NOT called.
        assert not s.http.post.called
    finally:
        m._ops_state = orig


def test_reindex_project_proceeds_when_confirm_token_matches(proxy_state):
    proxy_state.http.post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"status": "reindexed", "project_id": "alpha",
                      "stats": {"files_indexed": 10}},
    )
    r = mcp_ops_server.reindex_project("alpha", confirm_token="alpha")
    assert r["ok"] is True
    assert proxy_state.http.post.called
    call = proxy_state.http.post.call_args
    assert call.args[0] == "/api/projects/alpha/reindex"


def test_reindex_project_guard_before_proxy_check():
    """The confirm_token guard must reject BEFORE the proxy-mode check —
    a wrong-token call should never hit the network even in proxy mode."""
    s = McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s"))
    s.mode = "proxy"
    s.http = MagicMock()
    import ragtools.integration.mcp_server as m
    orig = m._ops_state
    m._ops_state = s
    try:
        r = m.reindex_project("alpha", confirm_token="")
        assert r["ok"] is False
        assert not s.http.post.called
    finally:
        m._ops_state = orig


def test_add_project_ignore_rule_posts_pattern(proxy_state):
    proxy_state.http.post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"status": "added", "project_id": "alpha", "pattern": "tmp/",
                      "ignore_patterns_count": 1},
    )
    r = mcp_ops_server.add_project_ignore_rule("alpha", "tmp/")
    assert r["ok"] is True
    call = proxy_state.http.post.call_args
    assert call.args[0] == "/api/projects/alpha/ignore"
    assert call.kwargs["json"] == {"pattern": "tmp/"}


def test_remove_project_ignore_rule_sends_delete(proxy_state):
    proxy_state.http.delete = MagicMock(
        return_value=MagicMock(
            status_code=200,
            json=lambda: {"status": "removed", "project_id": "alpha",
                          "pattern": "tmp/", "ignore_patterns_count": 0},
        ),
    )
    r = mcp_ops_server.remove_project_ignore_rule("alpha", "tmp/")
    assert r["ok"] is True
    call = proxy_state.http.delete.call_args
    assert call.args[0] == "/api/projects/alpha/ignore"
    assert call.kwargs["params"] == {"pattern": "tmp/"}


def test_add_project_ignore_rule_refuses_in_degraded_mode(degraded_state):
    r = mcp_ops_server.add_project_ignore_rule("alpha", "tmp/")
    assert r["ok"] is False
    assert "hint" in r


def test_remove_project_ignore_rule_refuses_in_degraded_mode(degraded_state):
    r = mcp_ops_server.remove_project_ignore_rule("alpha", "tmp/")
    assert r["ok"] is False


def test_ops_server_declares_all_phase3_tools():
    """Compile-time inventory check for Family B."""
    expected = {
        "run_index",
        "reindex_project",
        "add_project_ignore_rule",
        "remove_project_ignore_rule",
    }
    for name in expected:
        assert hasattr(mcp_ops_server, name), f"server missing {name}"


# ---------------------------------------------------------------------------
# Release-readiness invariants — catch regressions that would change the
# contract agents rely on. These are the contracts that matter enough to
# freeze.
# ---------------------------------------------------------------------------


def test_core_tools_registration_is_decorator_not_conditional():
    """The 3 core tools MUST always be registered via @mcp_app.tool().
    If anyone tries to move them into the conditional registrar loop,
    this test flags it."""
    import inspect
    src = inspect.getsource(mcp_ops_server)
    for name in ("search_knowledge_base", "list_projects", "index_status"):
        # Check the function definition is immediately preceded by @mcp_app.tool()
        # — not in the ops-tools loop list.
        idx = src.find(f"def {name}(")
        assert idx > 0, f"{name} missing"
        preceding = src[:idx].rsplit("\n", 3)[-3:]
        assert any("@mcp_app.tool()" in line for line in preceding), (
            f"{name} must be decorated with @mcp_app.tool() to remain a core tool"
        )


def test_core_tools_NOT_in_optional_registrar_list():
    """Core tools must never appear in the conditional registration list
    — that's the slippage vector that would turn them off by accident."""
    import inspect
    src = inspect.getsource(mcp_ops_server._register_ops_tools)
    for name in ("search_knowledge_base", "list_projects", "index_status"):
        assert f'"{name}"' not in src, (
            f"{name} must NOT be in _register_ops_tools — it's a core tool"
        )


def test_register_ops_tools_is_safe_when_settings_is_none(monkeypatch, caplog):
    """If somehow _register_ops_tools is called before _initialize, it must
    NOT crash with a NameError / AttributeError. Uses a logger instead of
    assert so running with python -O doesn't strip the guard."""
    import logging
    monkeypatch.setattr(mcp_ops_server, "_settings", None)
    with caplog.at_level(logging.ERROR):
        # Should not raise.
        mcp_ops_server._register_ops_tools()
    # And should have logged something helpful.
    assert any("before _initialize" in rec.message or "_initialize" in rec.message
               for rec in caplog.records)


def test_all_write_tools_require_proxy_mode(degraded_state):
    """Every write tool must refuse cleanly in degraded mode. A write tool
    that silently falls back to a filesystem path would be a safety hole."""
    writes = [
        ("run_index", ("alpha",), {}),
        ("add_project_ignore_rule", ("alpha", "x"), {}),
        ("remove_project_ignore_rule", ("alpha", "x"), {}),
        # reindex_project has a confirm_token guard BEFORE the proxy check,
        # so it rejects on confirm_token first. Exercise the proxy-only
        # guard by passing a valid confirm_token.
        ("reindex_project", ("alpha",), {"confirm_token": "alpha"}),
    ]
    for name, args, kwargs in writes:
        fn = getattr(mcp_ops_server, name)
        r = fn(*args, **kwargs)
        assert r["ok"] is False, f"{name} should refuse in degraded mode"
        assert "service" in (r.get("error", "") + r.get("hint", "")).lower(), (
            f"{name} should hint at starting the service"
        )


def test_reindex_project_confirm_token_guard_is_immutable():
    """The confirm-token guard must check BEFORE the proxy gate and BEFORE
    any HTTP call. This test verifies via a simulated proxy state that the
    guard fires even when the network WOULD succeed."""
    from unittest.mock import MagicMock
    s = McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s"))
    s.mode = "proxy"
    s.http = MagicMock()
    s.http.post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})

    orig = mcp_ops_server._ops_state
    try:
        mcp_ops_server._ops_state = s

        # Empty token, wrong token — both must reject.
        for bad_token in ("", "yes", "other_project", None):
            r = mcp_ops_server.reindex_project("alpha", confirm_token=bad_token or "")
            assert r["ok"] is False, f"confirm_token={bad_token!r} should reject"
            assert not s.http.post.called, "HTTP must NOT be called for bad tokens"
    finally:
        mcp_ops_server._ops_state = orig


def test_proxy_delete_helper_exists():
    """Regression guard: the write tools rely on proxy_delete existing."""
    from ragtools.integration import mcp_common
    assert hasattr(mcp_common, "proxy_delete")
    assert callable(mcp_common.proxy_delete)


# ---------------------------------------------------------------------------
# error_code envelope (A2)
# ---------------------------------------------------------------------------


def test_err_envelope_carries_error_code():
    s = McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s"))
    s.mode = "degraded"
    from ragtools.integration.mcp_common import err
    from ragtools.integration import mcp_errors
    r = err(s, "test", code=mcp_errors.SERVICE_DOWN)
    assert r["error_code"] == "SERVICE_DOWN"


def test_err_envelope_defaults_code_to_unknown_and_warns(caplog):
    s = McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s"))
    from ragtools.integration.mcp_common import err
    import logging as _logging
    with caplog.at_level(_logging.WARNING):
        r = err(s, "oops")
    assert r["error_code"] == "UNKNOWN"
    assert any("without explicit code" in rec.message for rec in caplog.records)


def test_require_proxy_returns_startup_failed_in_failed_mode():
    from ragtools.integration.mcp_common import require_proxy
    s = McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s"))
    s.mode = "failed"
    s.http = None
    r = require_proxy(s, "anything")
    assert r["error_code"] == "STARTUP_FAILED"


def test_require_proxy_returns_degraded_code_in_degraded_mode(degraded_state):
    from ragtools.integration.mcp_common import require_proxy
    r = require_proxy(degraded_state, "anything")
    assert r["error_code"] == "DEGRADED_MODE"


def test_confirm_token_mismatch_has_specific_code():
    s = McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s"))
    s.mode = "proxy"
    s.http = MagicMock()
    import ragtools.integration.mcp_server as m
    orig = m._ops_state
    m._ops_state = s
    try:
        r = m.reindex_project("alpha", confirm_token="wrong")
        assert r["error_code"] == "CONFIRM_TOKEN_MISMATCH"
    finally:
        m._ops_state = orig


def test_service_down_tools_use_service_down_code(degraded_state):
    r = mcp_ops_server.service_status()
    assert r["error_code"] == "SERVICE_DOWN"
    r = mcp_ops_server.project_status("alpha")
    assert r["error_code"] == "SERVICE_DOWN"
    r = mcp_ops_server.system_health()
    assert r["error_code"] == "SERVICE_DOWN"


# ---------------------------------------------------------------------------
# Session ID (A5)
# ---------------------------------------------------------------------------


def test_mcp_state_generates_session_id():
    s = McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s"))
    assert s.session_id
    assert len(s.session_id) == 4
    # hex: 0-9a-f
    assert all(c in "0123456789abcdef" for c in s.session_id)


def test_mcp_state_session_ids_are_unique_per_instance():
    ids = {
        McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s")).session_id
        for _ in range(50)
    }
    # With 4 hex chars (65k possible), collision in 50 samples is extremely unlikely.
    assert len(ids) >= 49


def test_mcp_session_header_set_on_proxy_client(monkeypatch):
    """The McpState.http client should carry X-MCP-Session by default."""
    # Simulate a live service so McpState.initialize picks proxy mode.
    import httpx
    real = httpx.get

    def fake_get(url, **kw):
        class R:
            status_code = 200
        return R()

    monkeypatch.setattr(httpx, "get", fake_get)
    s = McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s"))
    try:
        s.initialize(probe_retries=1, retry_sleep=0)
        assert s.mode == "proxy"
        assert s.http is not None
        assert s.http.headers.get("X-MCP-Session") == s.session_id
    finally:
        if s.http:
            s.http.close()
    # Restore
    monkeypatch.setattr(httpx, "get", real)


# ---------------------------------------------------------------------------
# Cooldown (B2)
# ---------------------------------------------------------------------------


def test_write_cooldown_blocks_second_call_inside_window():
    from ragtools.integration.mcp_common import WriteCooldown
    clock = [0.0]
    def tick(): return clock[0]
    cd = WriteCooldown({"run_index": 2.0}, clock=tick)
    assert cd.check("run_index") is None       # first call clear
    cd.mark("run_index")
    clock[0] = 0.5                              # 0.5s later — still in window
    remaining = cd.check("run_index")
    assert remaining is not None
    assert 1.0 <= remaining <= 2.0


def test_write_cooldown_allows_call_after_window():
    from ragtools.integration.mcp_common import WriteCooldown
    clock = [0.0]
    def tick(): return clock[0]
    cd = WriteCooldown({"run_index": 1.0}, clock=tick)
    cd.mark("run_index")
    clock[0] = 2.0                              # past the window
    assert cd.check("run_index") is None


def test_write_cooldown_is_per_tool():
    from ragtools.integration.mcp_common import WriteCooldown
    cd = WriteCooldown({"run_index": 5.0, "reindex_project": 5.0})
    cd.mark("run_index")
    # reindex_project is independent
    assert cd.check("reindex_project") is None


def test_run_index_returns_cooldown_error_when_blocked(proxy_state):
    """After a successful run_index, an immediate second call should
    return COOLDOWN without hitting the backend."""
    import ragtools.integration.mcp_server as m
    proxy_state.http.post.return_value = MagicMock(status_code=200, json=lambda: {"stats": {}})
    # Fresh cooldown state
    from ragtools.integration.mcp_common import WriteCooldown
    orig = m._write_cooldown
    m._write_cooldown = WriteCooldown({"run_index": 10.0})
    try:
        r1 = m.run_index("alpha")
        assert r1["ok"] is True
        r2 = m.run_index("alpha")
        assert r2["ok"] is False
        assert r2["error_code"] == "COOLDOWN"
        assert "retry_after_seconds" in r2
    finally:
        m._write_cooldown = orig


def test_reindex_project_cooldown_check_runs_after_confirm_token():
    """The confirm_token mismatch must be reported FIRST (ahead of cooldown)
    so agents don't get confusing signals."""
    import ragtools.integration.mcp_server as m
    from ragtools.integration.mcp_common import WriteCooldown
    # Prime the cooldown so reindex_project would be blocked if reached.
    orig = m._write_cooldown
    m._write_cooldown = WriteCooldown({"reindex_project": 10.0})
    m._write_cooldown.mark("reindex_project")

    # Also fake proxy state
    s = McpState(Settings(qdrant_path="/tmp/q", state_db="/tmp/s"))
    s.mode = "proxy"
    s.http = MagicMock()
    orig_state = m._ops_state
    m._ops_state = s
    try:
        r = m.reindex_project("alpha", confirm_token="wrong")
        # Should reject on confirm_token, NOT on cooldown.
        assert r["error_code"] == "CONFIRM_TOKEN_MISMATCH"
    finally:
        m._ops_state = orig_state
        m._write_cooldown = orig


# ---------------------------------------------------------------------------
# Startup failure (A6)
# ---------------------------------------------------------------------------


def test_safe_initialize_survives_exception(monkeypatch):
    """When _initialize raises, _safe_initialize must catch + set mode=failed."""
    import ragtools.integration.mcp_server as m

    # Reset module state
    orig_mode = m._mode
    orig_err = m._init_error
    orig_ops = m._ops_state
    try:
        m._mode = "uninitialized"
        m._init_error = None
        m._ops_state = None

        def boom() -> None:
            raise RuntimeError("settings load exploded")

        monkeypatch.setattr(m, "_initialize", boom)
        # Must not raise.
        m._safe_initialize()

        assert m._mode == "failed"
        assert m._init_error is not None
        assert "settings load exploded" in m._init_error
        assert m._ops_state is not None
        assert m._ops_state.mode == "failed"
    finally:
        m._mode = orig_mode
        m._init_error = orig_err
        m._ops_state = orig_ops


# ---------------------------------------------------------------------------
# Structured search (A1)
# ---------------------------------------------------------------------------


def test_search_structured_mode_returns_dict(proxy_state, monkeypatch):
    import ragtools.integration.mcp_server as m
    monkeypatch.setattr(m, "_mode", "proxy")
    monkeypatch.setattr(m, "_http_client", proxy_state.http)
    proxy_state.http.get.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "context": "[RAG CONTEXT] stuff",
            "results": [{"score": 0.9, "file_path": "a.md"}],
            "meta": {"query": "x", "count": 1, "project": None, "projects": None,
                      "top_k": 10, "compact": True},
        },
    )
    r = m.search_knowledge_base("x", structured=True)
    assert isinstance(r, dict)
    assert "context" in r
    assert "results" in r
    assert "meta" in r


def test_search_default_mode_still_returns_string(proxy_state, monkeypatch):
    import ragtools.integration.mcp_server as m
    monkeypatch.setattr(m, "_mode", "proxy")
    monkeypatch.setattr(m, "_http_client", proxy_state.http)
    proxy_state.http.get.return_value = MagicMock(
        status_code=200,
        json=lambda: {"formatted": "[RAG CONTEXT] stuff", "count": 1, "results": []},
    )
    r = m.search_knowledge_base("x")  # structured omitted = default False
    assert isinstance(r, str)


def test_search_structured_passes_flag_to_backend(proxy_state, monkeypatch):
    import ragtools.integration.mcp_server as m
    monkeypatch.setattr(m, "_mode", "proxy")
    monkeypatch.setattr(m, "_http_client", proxy_state.http)
    proxy_state.http.get.return_value = MagicMock(
        status_code=200,
        json=lambda: {"context": "", "results": [], "meta": {}},
    )
    m.search_knowledge_base("q", structured=True)
    call = proxy_state.http.get.call_args
    assert call.kwargs["params"].get("structured") == "true"


# ---------------------------------------------------------------------------
# index_status consistency (A3)
# ---------------------------------------------------------------------------


def test_index_status_proxy_and_direct_share_same_keys_set(monkeypatch, tmp_path):
    """Parse the 'Key:' lines from both proxy and direct output and assert
    equality. Protects against future drift."""
    import re
    import ragtools.integration.mcp_server as m

    # Stub direct mode: pretend Qdrant collection has 5 points, state DB empty.
    monkeypatch.setattr(m, "_mode", "direct")
    monkeypatch.setattr(m, "_init_error", None)
    monkeypatch.setattr(m, "_settings", Settings(
        qdrant_path=str(tmp_path / "q"), state_db=str(tmp_path / "s.db"),
    ))

    class _FakeInfo:
        points_count = 5

    class _FakeQdrant:
        def get_collection(self, name): return _FakeInfo()

    monkeypatch.setattr(m, "_get_direct_client", lambda: _FakeQdrant())
    direct_out = m._direct_index_status()
    direct_keys = set(re.findall(r"^  (\w[\w ]*?):", direct_out, re.MULTILINE))

    # Stub proxy mode
    mock = MagicMock()
    mock.get.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "collection_name": "markdown_kb", "total_files": 10,
            "total_chunks": 50, "points_count": 50, "projects": ["a"],
        },
    )
    monkeypatch.setattr(m, "_mode", "proxy")
    monkeypatch.setattr(m, "_http_client", mock)
    proxy_out = m._proxy_index_status()
    proxy_keys = set(re.findall(r"^  (\w[\w ]*?):", proxy_out, re.MULTILINE))

    # Keys must match exactly so agents can rely on either mode's output.
    assert proxy_keys == direct_keys, (
        f"Proxy-only: {proxy_keys - direct_keys}. "
        f"Direct-only: {direct_keys - proxy_keys}."
    )


# ---------------------------------------------------------------------------


def test_optional_tool_names_align_with_settings_defaults():
    """Every tool the registrar knows about must be in the Settings default
    dict. Otherwise disabling via admin UI would have no effect because the
    setting wouldn't be serialisable into ragtools.toml."""
    from ragtools.integration.mcp_server import _register_ops_tools
    import ast, inspect

    # Parse the tool-list tuple inside _register_ops_tools source.
    src = inspect.getsource(_register_ops_tools)
    tree = ast.parse(src)

    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value and node.value not in ("mcp_tools",):
                # Simple heuristic: only underscore-heavy strings look like
                # tool names. Good enough for a contract check.
                if node.value.replace("_", "").isalpha() and len(node.value) > 4:
                    names.add(node.value)

    s = Settings(qdrant_path="/tmp/q", state_db="/tmp/s")
    missing = names - set(s.mcp_tools.keys())
    # Ignore common non-tool strings that might creep in.
    missing.discard("before_initialize")
    missing.discard("optional_tools")
    missing.discard("registered_tools")
    assert not missing, (
        f"Tools referenced in the registrar but absent from Settings.mcp_tools "
        f"defaults: {missing}. Add them to the default dict."
    )


# ---------------------------------------------------------------------------
# Conditional registration — the per-tool access-control contract
# ---------------------------------------------------------------------------


def test_register_ops_tools_skips_disabled_tools(tmp_path, monkeypatch):
    """If the user disables `tail_logs` in settings, it must not be
    registered — invisible to the agent, no token cost."""
    from ragtools.integration import mcp_server

    # Construct a Settings with only ONE optional tool enabled.
    s = _settings(tmp_path)
    s.mcp_tools = {
        "service_status":     True,
        "recent_activity":    False,
        "tail_logs":          False,
        "crash_history":      True,
        "get_config":         False,
        "get_ignore_rules":   False,
        "get_paths":          False,
        "system_health":      False,
        "list_indexed_paths": False,
    }
    monkeypatch.setattr(mcp_server, "_settings", s)

    # Collect which tools `add_tool` gets called with.
    added = []

    class _Recorder:
        def add_tool(self, fn, name=None, **kw):
            added.append(name or getattr(fn, "__name__", "?"))

    monkeypatch.setattr(mcp_server, "mcp_app", _Recorder())

    mcp_server._register_ops_tools()

    assert "service_status" in added
    assert "crash_history" in added
    assert "tail_logs" not in added, "disabled tool must not be registered"
    assert "recent_activity" not in added
    # Only the two tools flagged True in this settings object should register.
    # Project tools default to True via the fallback in the registrar when
    # not present, so they also register — exclude them from the count.
    enabled_by_settings = {k for k, v in s.mcp_tools.items() if v}
    # Tools whose names are absent from settings default to enabled too.
    assert set(added) >= enabled_by_settings


def test_register_ops_tools_defaults_to_enabled_for_unknown_names(tmp_path, monkeypatch):
    """A tool name missing from the access dict must default to ENABLED.
    This is forward-compat: upstream adds a new tool, old user configs
    don't silently block it.
    """
    from ragtools.integration import mcp_server

    s = _settings(tmp_path)
    # Empty dict — the loop defaults each missing key to True.
    s.mcp_tools = {}
    monkeypatch.setattr(mcp_server, "_settings", s)

    added = []

    class _Recorder:
        def add_tool(self, fn, name=None, **kw):
            added.append(name or getattr(fn, "__name__", "?"))

    monkeypatch.setattr(mcp_server, "mcp_app", _Recorder())
    mcp_server._register_ops_tools()

    # With an empty dict the registrar's ``access.get(name, True)`` default
    # kicks in for every tool — so ALL optional tools should register.
    expected_tool_count = 18  # 9 diagnostics + 5 project read + 4 project write
    assert len(added) == expected_tool_count


def test_settings_default_mcp_tools_covers_all_optional_tools():
    """The default mcp_tools dict in Settings must list every tool the
    registrar knows about — otherwise new tools would be unreachable
    without the user explicitly enabling them."""
    s = Settings(qdrant_path="/tmp/q", state_db="/tmp/s")
    expected = {
        # diagnostics
        "service_status", "recent_activity", "tail_logs", "crash_history",
        "get_config", "get_ignore_rules", "get_paths", "system_health",
        "list_indexed_paths",
        # project inspection
        "project_status", "project_summary", "list_project_files",
        "get_project_ignore_rules", "preview_ignore_effect",
        # project-scoped writes
        "run_index", "reindex_project",
        "add_project_ignore_rule", "remove_project_ignore_rule",
    }
    assert set(s.mcp_tools.keys()) == expected


def test_default_mcp_tools_project_on_debug_off():
    """Tiered defaults: project tools enabled, debugging tools disabled.

    A fresh install should give the agent the primary project-work surface
    (9 project tools) but not burden it with operator diagnostics (9 debug
    tools). Debug tools are still visible in the admin UI so the user can
    grant them when troubleshooting."""
    s = Settings(qdrant_path="/tmp/q", state_db="/tmp/s")

    project_tools = {
        "project_status", "project_summary", "list_project_files",
        "get_project_ignore_rules", "preview_ignore_effect",
        "run_index", "reindex_project",
        "add_project_ignore_rule", "remove_project_ignore_rule",
    }
    debug_tools = {
        "service_status", "recent_activity", "tail_logs", "crash_history",
        "get_config", "get_ignore_rules", "get_paths", "system_health",
        "list_indexed_paths",
    }

    for name in project_tools:
        assert s.mcp_tools.get(name) is True, f"project tool '{name}' should default to True"
    for name in debug_tools:
        assert s.mcp_tools.get(name) is False, f"debug tool '{name}' should default to False"


# ---------------------------------------------------------------------------
# Family C — multi-project search (extension of search_knowledge_base)
# ---------------------------------------------------------------------------


def test_searcher_accepts_project_ids_list(tmp_path):
    """The Searcher should build a ``should`` Qdrant filter for multi-project."""
    from unittest.mock import MagicMock
    from ragtools.retrieval.searcher import Searcher
    from ragtools.config import Settings

    fake_client = MagicMock()
    fake_client.query_points.return_value = MagicMock(points=[])
    fake_encoder = MagicMock()
    fake_encoder.encode_query.return_value = MagicMock(tolist=lambda: [0.0])

    s = Settings(qdrant_path=str(tmp_path / "q"), state_db=str(tmp_path / "s"))
    searcher = Searcher(client=fake_client, encoder=fake_encoder, settings=s)
    searcher.search(query="x", project_ids=["a", "b", "c"])

    call = fake_client.query_points.call_args
    query_filter = call.kwargs["query_filter"]
    assert query_filter is not None
    # Multi-project filter uses ``should`` (OR semantics), not ``must``.
    assert query_filter.should is not None
    assert len(query_filter.should) == 3


def test_searcher_single_project_takes_must(tmp_path):
    """Backwards compat: passing ``project_id`` alone keeps the ``must`` filter."""
    from unittest.mock import MagicMock
    from ragtools.retrieval.searcher import Searcher
    from ragtools.config import Settings

    fake_client = MagicMock()
    fake_client.query_points.return_value = MagicMock(points=[])
    fake_encoder = MagicMock()
    fake_encoder.encode_query.return_value = MagicMock(tolist=lambda: [0.0])

    s = Settings(qdrant_path=str(tmp_path / "q"), state_db=str(tmp_path / "s"))
    searcher = Searcher(client=fake_client, encoder=fake_encoder, settings=s)
    searcher.search(query="x", project_id="a")

    call = fake_client.query_points.call_args
    qf = call.kwargs["query_filter"]
    assert qf.must is not None
    assert len(qf.must) == 1


def test_mcp_search_knowledge_base_forwards_projects_as_csv(proxy_state, monkeypatch):
    """The MCP search tool should forward ``projects=[a,b,c]`` as a
    comma-separated ``projects`` query param to the HTTP backend."""
    from ragtools.integration import mcp_server

    # Set proxy-mode globals so search_knowledge_base uses proxy search path.
    monkeypatch.setattr(mcp_server, "_mode", "proxy")
    monkeypatch.setattr(mcp_server, "_http_client", proxy_state.http)

    _mock_200(proxy_state.http, {"formatted": "results"})
    mcp_server.search_knowledge_base("query text", projects=["a", "b", "c"])

    call = proxy_state.http.get.call_args
    assert call.args[0] == "/api/search"
    assert call.kwargs["params"]["projects"] == "a,b,c"
    # And should NOT fall back to passing `project` when `projects` is supplied
    assert "project" not in call.kwargs["params"]
