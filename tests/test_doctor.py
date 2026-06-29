"""Tests for `rag doctor` — new Watcher / Index-freshness / Project-path checks
and the `--json` machine-readable mode (report L1/M1/M2/L4)."""

import json

from typer.testing import CliRunner

from ragtools.cli import app as cli_app


def _isolate(monkeypatch, tmp_path):
    # Defaults only + a dead service port so doctor never hits a real service.
    monkeypatch.setenv("RAG_CONFIG_PATH", str(tmp_path / "nope.toml"))
    monkeypatch.setenv("RAG_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("RAG_QDRANT_PATH", str(tmp_path / "qdrant"))
    monkeypatch.setenv("RAG_SERVICE_PORT", "21499")  # nothing listening here


def test_doctor_table_runs_and_has_new_rows(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    res = CliRunner().invoke(cli_app, ["doctor"])
    assert res.exit_code == 0, res.stdout
    assert "RAG System Health Check" in res.stdout
    assert "Watcher" in res.stdout  # new watcher row


def test_doctor_json_is_valid_and_structured(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    res = CliRunner().invoke(cli_app, ["doctor", "--json"])
    assert res.exit_code == 0, res.stdout
    report = json.loads(res.stdout)  # must be pure, parseable JSON
    assert report["install_mode"] in ("packaged", "source")
    assert isinstance(report["version"], str) and report["version"]
    assert isinstance(report["ok"], bool)
    assert isinstance(report["checks"], list) and report["checks"]
    comps = {c["component"] for c in report["checks"]}
    assert {"Python", "Service", "Watcher"} <= comps
    # service is down (dead port) -> watcher reported UNKNOWN
    watcher_row = next(c for c in report["checks"] if c["component"] == "Watcher")
    assert watcher_row["status"] == "unknown"
    assert "recommended_actions" in report and isinstance(report["recommended_actions"], list)
    assert "Logs" in comps
    assert report["log_path"] and report["log_path"].endswith("service.log")
