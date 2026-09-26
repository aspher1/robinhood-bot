"""Tightenings from the risk review after the replay and calendar-hold fixes."""

import json
from datetime import timedelta
from decimal import Decimal

from rhbot.cli import main
from rhbot.config import frozen_params_hash, load_settings
from rhbot.errors import ConfigError
from rhbot.models import OrderIntent
from rhbot.ops import engage_kill
from rhbot.status import build_report, render_markdown

from tests.conftest import engine, snapshot


def _events(bot, kind: str) -> list[dict]:
    return [
        json.loads(row["payload"])
        for row in bot.ledger.conn.execute(
            "SELECT payload FROM events WHERE kind=? ORDER BY seq",
            (kind,),
        )
    ]


def test_manual_kill_resume_requires_a_verified_human_code(tmp_path, now, monkeypatch):
    bot = engine(tmp_path)
    bot.run_once(now=now, snapshot=snapshot(now))
    bot.ledger.close()
    engage_kill(tmp_path, "manual", "operator")
    assert main(["resume", "--state-dir", str(tmp_path)]) == 2
    assert main(["resume", "--ack", "--state-dir", str(tmp_path)]) == 2
    secret = tmp_path / "human-code"
    secret.write_text("resume-ok\n", encoding="utf-8")
    monkeypatch.setenv("RHBOT_HUMAN_RESUME_FILE", str(secret))
    assert main(["resume", "--ack", "--human-code", "nope", "--state-dir", str(tmp_path)]) == 2
    assert (tmp_path / "KILL").exists()
    bot = engine(tmp_path)
    assert [item for item in _events(bot, "resume") if item.get("by") == "human"] == []
    bot.ledger.close()
    assert main(["resume", "--ack", "--human-code", "resume-ok", "--state-dir", str(tmp_path)]) == 0
    assert not (tmp_path / "KILL").exists()
    bot = engine(tmp_path)
    resumed = _events(bot, "resume")
    assert resumed[-1]["by"] == "human"
    bot.ledger.close()


def test_frozen_params_are_rejected_and_hashed(tmp_path, now):
    path = tmp_path / "config.yaml"
    state = tmp_path / "state"
    state.mkdir()
    path.write_text('sma_window: 50\ntrend_band: "0"\ndca_notional: "500"\n', encoding="utf-8")
    try:
        load_settings(str(path), str(state))
        raised = False
    except ConfigError:
        raised = True
    assert raised

    bot = engine(tmp_path / "run")
    bot.run_once(now=now, snapshot=snapshot(now))
    assert bot.ledger.get_meta("frozen_params_hash") == frozen_params_hash()
    fills = len(bot.ledger.fills_for("dca_weekly"))
    bot.ledger.set_meta("frozen_params_hash", "0" * 64)
    try:
        bot.run_once(now=now + timedelta(days=1), snapshot=snapshot(now + timedelta(days=1)))
        refused = False
    except RuntimeError as exc:
        refused = "frozen" in str(exc)
    assert refused
    assert len(bot.ledger.fills_for("dca_weekly")) == fills
    bot.ledger.close()


def test_same_cycle_second_order_sees_the_first(tmp_path, now):
    bot = engine(tmp_path)
    strategy = next(item for item in bot.strategies if item.name == "buy_and_hold")

    def decide(view, state, positions, cash, equity, when):
        del view, positions, cash, equity, when
        return (
            [
                OrderIntent("BTC-USD", "buy", "buy_and_hold", quote_amount=Decimal("100")),
                OrderIntent("BTC-USD", "buy", "buy_and_hold", quote_amount=Decimal("100")),
            ],
            state,
            "deploy",
        )

    strategy.decide = decide
    bot.run_once(now=now, snapshot=snapshot(now))
    fills = [row for row in bot.ledger.fills_for("buy_and_hold") if row["symbol"] == "BTC-USD"]
    assert len(fills) == 1
    assert any(
        item["reason"] in ("one_order_per_symbol_per_bar", "duplicate_client_order_id")
        for item in _events(bot, "risk_denial")
    )
    bot.ledger.close()


def test_second_flatten_the_same_day_sells(tmp_path, now, monkeypatch):
    bot = engine(tmp_path)
    bot.run_once(now=now, snapshot=snapshot(now))
    bot.ledger.conn.execute("DELETE FROM positions WHERE sleeve='trend_daily'")
    bot.ledger.conn.execute(
        "INSERT INTO positions(sleeve, symbol, qty) VALUES('trend_daily', 'BTC-USD', '0.05000000')"
    )
    bot.ledger.conn.commit()
    first = bot.flatten(now=now, snapshot=snapshot(now))
    assert first["errors"] == []
    assert bot.ledger.positions("trend_daily") == {}
    bot.ledger.conn.execute("DELETE FROM positions WHERE sleeve='trend_daily'")
    bot.ledger.conn.execute(
        "INSERT INTO positions(sleeve, symbol, qty) VALUES('trend_daily', 'BTC-USD', '0.05000000')"
    )
    bot.ledger.conn.commit()
    second = bot.flatten(now=now, snapshot=snapshot(now))
    assert second["errors"] == []
    assert second["remaining"] == []
    assert bot.ledger.positions("trend_daily") == {}
    ids = [
        row["client_order_id"]
        for row in bot.ledger.fills_for("trend_daily")
        if row["reason"] == "flatten"
    ]
    assert len(ids) == 2
    assert len(set(ids)) == 2
    bot.ledger.close()

    def unsold(self, *args, **kwargs):
        del self, args, kwargs
        return {
            "fills": [],
            "errors": [],
            "remaining": [{"sleeve": "trend_daily", "symbol": "BTC-USD", "qty": "0.05000000"}],
        }

    monkeypatch.setattr("rhbot.cli.Engine.flatten", unsold)
    assert main(["flatten", "--paper", "--state-dir", str(tmp_path)]) == 2


def test_shadow_book_honors_the_kill_file(tmp_path, now):
    engage_kill(tmp_path, "manual", "operator")
    bot = engine(tmp_path)
    bot.run_once(now=now, snapshot=snapshot(now))
    assert bot.ledger.positions("dca_weekly") == {}
    assert bot.ledger.shadow_positions("dca_weekly_shadow") == {}
    bot.ledger.close()


def test_killed_dca_denial_is_once_per_state_change(tmp_path, now):
    bot = engine(tmp_path)
    bot.run_once(now=now, snapshot=snapshot(now))
    bot.ledger.conn.execute("DELETE FROM positions WHERE sleeve='dca_weekly'")
    bot.ledger.conn.commit()
    bot.ledger.save_overlay("dca_weekly", {"state": "KILLED", "kill_acked_peak": ""})
    later = now + timedelta(days=7)
    bot.run_once(now=later, snapshot=snapshot(later))
    bot.run_once(now=later + timedelta(minutes=1), snapshot=snapshot(later + timedelta(minutes=1)))
    killed = [
        item
        for item in _events(bot, "risk_denial")
        if item["sleeve"] == "dca_weekly" and item["reason"] == "killed"
    ]
    assert len(killed) == 1
    bot.ledger.close()


def test_report_freeze_column_counts_freeze_trips(tmp_path, now):
    bot = engine(tmp_path)
    bot.run_once(now=now, snapshot=snapshot(now))
    bot.ledger.conn.execute("UPDATE sleeves SET cash='800.00000000' WHERE name='trend_daily'")
    bot.ledger.conn.commit()
    later = now + timedelta(days=1)
    bot.run_once(now=later, snapshot=snapshot(later))
    assert bot.ledger.overlay_row("trend_daily")["state"] == "FROZEN"
    report = build_report(bot.settings, "30d", now=later + timedelta(days=1))
    freezes = report["sleeves"]["trend_daily"]["drawdown_freezes"]
    assert len(freezes) >= 1
    row = next(line for line in render_markdown(report).splitlines() if line.startswith("| trend_daily |"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[8] != "0"
    bot.ledger.close()
