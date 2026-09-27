import csv
import io
import json
from datetime import datetime, timezone

import pytest

from rhbot.artifacts import write_new_artifact
from rhbot.cli import main
from tests.conftest import engine, snapshot


def test_dashboard_and_exports_work_offline_without_mutating_paper_state(tmp_path, capsys, monkeypatch):
    state = tmp_path / "state"
    bot = engine(state)
    wall = datetime.now(timezone.utc)
    bot.run_once(now=wall, snapshot=snapshot(wall))
    expected_fills = [dict(row) for name in bot.ledger.sleeve_names() for row in bot.ledger.fills_for(name)]
    bot.ledger.close()
    ledger_bytes = (state / "bot.sqlite").read_bytes()
    heartbeat_bytes = (state / "heartbeat.json").read_bytes()

    def no_network(*args, **kwargs):
        raise AssertionError("operator views must not fetch market data")

    monkeypatch.setattr("httpx.Client.get", no_network)
    dashboard = tmp_path / "reports" / "paper.html"
    assert main(["dashboard", "--state-dir", str(state), "--output", str(dashboard)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["started"]
    assert result["output"] == str(dashboard)
    html = dashboard.read_text()
    assert "Buy and hold" in html and "Weekly DCA" in html and "Daily trend" in html
    assert "<script" not in html

    fills = tmp_path / "reports" / "fills.csv"
    assert main(["export", "fills", "--state-dir", str(state), "--output", str(fills)]) == 0
    result = json.loads(capsys.readouterr().out)
    rows = list(csv.DictReader(io.StringIO(fills.read_text())))
    assert result["rows"] == len(expected_fills) == len(rows)
    assert {r["client_order_id"] for r in rows} == {r["client_order_id"] for r in expected_fills}
    assert {r["book"] for r in rows} == {"paper"}

    equity = tmp_path / "reports" / "equity.csv"
    assert main(["export", "equity", "--include-shadow", "--state-dir", str(state), "--output", str(equity)]) == 0
    result = json.loads(capsys.readouterr().out)
    rows = list(csv.DictReader(io.StringIO(equity.read_text())))
    assert result["include_shadow"] and result["rows"] == len(rows)
    assert {r["book"] for r in rows} == {"paper", "shadow"}
    assert (state / "bot.sqlite").read_bytes() == ledger_bytes
    assert (state / "heartbeat.json").read_bytes() == heartbeat_bytes


def test_views_before_start_create_only_requested_artifacts(tmp_path, capsys):
    state = tmp_path / "not-started"
    target = tmp_path / "empty.html"
    assert main(["dashboard", "--state-dir", str(state), "--output", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["started"] is False
    assert "No paper results yet" in target.read_text()
    assert not state.exists()


@pytest.mark.parametrize("command", [["dashboard"], ["export", "fills"]])
def test_artifact_cli_rejects_invalid_window_and_existing_file(tmp_path, capsys, command):
    target = tmp_path / "report"
    state = tmp_path / "missing"
    args = [*command, "--state-dir", str(state), "--output", str(target)]
    assert main([*args, "--since", "yesterday"]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not target.exists()
    target.write_text("keep this")
    assert main(args) == 2
    assert "already exists" in json.loads(capsys.readouterr().out)["error"]
    assert target.read_text() == "keep this"
    assert not state.exists()


def test_artifact_writer_protects_state_and_symlinked_state(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    link = tmp_path / "linked-state"
    link.symlink_to(state, target_is_directory=True)
    for target in (state, state / "KILL", state / "subdir" / "report.html", link / "report.csv"):
        with pytest.raises(ValueError, match="outside"):
            write_new_artifact(target, "bad output", state)
    assert list(state.iterdir()) == []
    output = write_new_artifact(tmp_path / "report.html", "paper data", state)
    assert output.read_text() == "paper data"
    assert output.stat().st_mode & 0o777 == 0o600
