import json
import subprocess
import sys
from datetime import timedelta

from rhbot.cli import main

from tests.conftest import engine, snapshot


def test_status_before_start(tmp_path, capsys):
    code = main(["status", "--state-dir", str(tmp_path)])
    body = json.loads(capsys.readouterr().out)
    assert code == 0
    assert body["mode"] == "paper"
    assert body["health"] == "idle_ok"
    assert body["running"] is False
    assert "not_started" in body["reasons"]


def test_kill_resume_and_audit(tmp_path, capsys):
    assert main(["kill", "--state-dir", str(tmp_path), "--reason", "pause"]) == 0
    kill_body = json.loads(capsys.readouterr().out)
    assert kill_body["kill_switch"] is True
    assert (tmp_path / "KILL").exists()
    health = main(["health", "--state-dir", str(tmp_path)])
    health_body = json.loads(capsys.readouterr().out)
    assert health == 2
    assert health_body["health"] == "critical"
    assert "kill_switch" in health_body["reasons"]
    assert main(["resume", "--state-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    assert not (tmp_path / "KILL").exists()
    assert main(["audit", "verify", "--state-dir", str(tmp_path)]) == 0
    audit = json.loads(capsys.readouterr().out)
    assert audit["ok"] is True
    assert audit["events"] == 0


def test_selftest_is_offline_and_does_not_touch_state(tmp_path, capsys, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("network was used")

    monkeypatch.setattr("httpx.Client.get", explode)
    code = main(["selftest", "--state-dir", str(tmp_path)])
    body = json.loads(capsys.readouterr().out)
    assert code == 0, body
    assert body["ok"] is True
    assert not (tmp_path / "KILL").exists()
    assert not (tmp_path / "bot.sqlite").exists()


def test_report_after_a_cycle(tmp_path, capsys, now):
    bot = engine(tmp_path, sma_window=3, min_hold_days=0)
    # Use wall-clock quotes so health sees fresh data, while the strategy
    # clock stays explicit for the fills.
    from datetime import datetime, timezone

    wall = datetime.now(timezone.utc)
    view = snapshot(wall)
    bot.run_once(now=wall, snapshot=view)
    bot.ledger.close()
    code = main(["status", "--json", "--state-dir", str(tmp_path)])
    body = json.loads(capsys.readouterr().out)
    assert code == 0, body
    assert body["health"] == "ok"
    assert body["running"] is True
    assert body["seconds_since_successful_action"] is not None
    assert body["seconds_since_successful_action"] < 120
    assert body["data_age_seconds"] is not None
    assert body["data_age_seconds"] < 120
    assert Decimal_positions(body)
    report_code = main(["report", "--since", "24h", "--state-dir", str(tmp_path)])
    report = json.loads(capsys.readouterr().out)
    assert report_code == 0
    assert "buy_and_hold" in report["sleeves"]
    assert "trend_daily" in report["sleeves"]
    assert "dca_weekly" in report["sleeves"]
    md_code = main(["report", "--since", "7d", "--md", "--state-dir", str(tmp_path)])
    markdown = capsys.readouterr().out
    assert md_code == 0
    assert "buy_and_hold" in markdown
    assert main(["audit", "verify", "--state-dir", str(tmp_path)]) == 0
    audit = json.loads(capsys.readouterr().out)
    assert audit["ok"] is True
    assert audit["events"] > 0


def Decimal_positions(body: dict) -> bool:
    assert body["equity"]["buy_and_hold"]
    assert body["error_counts"]["loop"] == 0
    return True


def test_flatten_flag_and_bad_since(tmp_path, capsys):
    assert main(["flatten", "--state-dir", str(tmp_path)]) == 2
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False
    assert main(["report", "--since", "yesterday", "--state-dir", str(tmp_path)]) == 2


def test_module_entrypoint(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "rhbot", "status", "--state-dir", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["mode"] == "paper"


def test_backtest_replay_uses_the_engine(tmp_path, now):
    from rhbot.backtest import replay
    from tests.conftest import make_bars, make_settings

    last_open = now - timedelta(days=1)
    closes = ["10", "10", "10", "12", "12"]
    settings = make_settings(tmp_path, sma_window=3, min_hold_days=0, trend_band=__import__("decimal").Decimal("0.01"))
    bars = {
        "BTC-USD": make_bars("BTC-USD", closes, last_open),
        "ETH-USD": make_bars("ETH-USD", closes, last_open),
    }
    bot = replay(settings, bars)
    assert bot.ledger.positions("buy_and_hold")["BTC-USD"] > 0
    ok, detail = bot.ledger.verify_chain()
    assert ok, detail
    bot.ledger.close()
