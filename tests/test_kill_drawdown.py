from datetime import timedelta
from decimal import Decimal

from rhbot.ops import read_kill

from tests.conftest import engine, snapshot


def _exposure(bot, sleeve: str, view, mid: str) -> tuple[Decimal, Decimal]:
    positions = bot.ledger.positions(sleeve)
    exposure = sum((qty * Decimal(mid) for qty in positions.values()), Decimal(0))
    return exposure, bot.mark(sleeve, view)


def test_entry_cost_does_not_freeze_or_kill(tmp_path, now):
    bot = engine(tmp_path, sma_window=3)
    bot.run_once(now=now, snapshot=snapshot(now))
    assert read_kill(tmp_path) is None
    assert not (tmp_path / "DRAWDOWN_FREEZE").exists()
    assert bot.ledger.positions("buy_and_hold")
    bot.ledger.close()


def test_ten_percent_combined_drawdown_freezes_buys_until_ack(tmp_path, now):
    """Only buy-and-hold is invested, so a drop to 60 is about 13% combined.

    That freezes new buys. It does not flatten, and it does not kill.
    """
    import json

    from rhbot.cli import main
    from rhbot.errors import OrderRejected
    from rhbot.models import OrderIntent

    bot = engine(tmp_path, sma_window=3)
    bot.run_once(now=now, snapshot=snapshot(now))
    later = now + timedelta(hours=2)
    view = snapshot(later, mid="60")
    bot.run_once(now=later, snapshot=view)
    bot.run_once(now=later + timedelta(minutes=5), snapshot=snapshot(later, mid="60"))
    assert read_kill(tmp_path) is None
    freeze = json.loads((tmp_path / "DRAWDOWN_FREEZE").read_text(encoding="utf-8"))
    assert freeze["by"] == "risk"
    assert bot.ledger.positions("buy_and_hold")
    _exposure_left, equity = _exposure(bot, "buy_and_hold", view, "60")
    assert equity > 0
    freezes = bot.ledger.conn.execute(
        "SELECT payload FROM events WHERE kind='drawdown_freeze'"
    ).fetchall()
    assert len(freezes) == 1
    payload = json.loads(freezes[0]["payload"])
    assert payload["reason"] == "drawdown_freeze"
    assert payload["limit_name"] == "drawdown_freeze_pct"
    assert payload["limit"] == "0.10"
    assert Decimal(payload["observed"]) >= Decimal("0.10")
    logged = bot.ledger.conn.execute(
        "SELECT reason, limit_value FROM trade_log WHERE kind='drawdown_freeze'"
    ).fetchall()
    assert len(logged) == 1
    assert logged[0]["reason"] == "drawdown_freeze"
    assert logged[0]["limit_value"] == "0.10"
    assert bot.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind='kill_trip'"
    ).fetchone()["n"] == 0

    with __import__("pytest").raises(OrderRejected) as caught:
        bot.broker.submit(
            "dca_weekly",
            OrderIntent("BTC-USD", "buy", "while_frozen", quote_amount=Decimal("25")),
            "frozen-buy",
            bot._context("dca_weekly", view, later),
            later,
        )
    assert caught.value.reasons[0].startswith("drawdown_freeze")

    sell_at = now + timedelta(days=1)
    sell_view = snapshot(sell_at, mid="60")
    held = bot.ledger.positions("buy_and_hold")["BTC-USD"]
    bot.broker.submit(
        "buy_and_hold",
        OrderIntent("BTC-USD", "sell", "exit", base_quantity=Decimal("0.2")),
        "frozen-sell",
        bot._context("buy_and_hold", sell_view, sell_at),
        sell_at,
    )
    assert bot.ledger.positions("buy_and_hold")["BTC-USD"] < held
    assert bot.ledger.positions("buy_and_hold")["ETH-USD"] > 0
    assert (tmp_path / "DRAWDOWN_FREEZE").exists()

    assert main(["ack-drawdown", "--state-dir", str(tmp_path)]) == 0
    assert not (tmp_path / "DRAWDOWN_FREEZE").exists()
    assert not (tmp_path / "KILL").exists()
    still = sell_at + timedelta(hours=1)
    bot.run_once(now=still, snapshot=snapshot(still, mid="60"))
    assert not (tmp_path / "DRAWDOWN_FREEZE").exists()
    assert len(
        bot.ledger.conn.execute(
            "SELECT payload FROM events WHERE kind='drawdown_freeze'"
        ).fetchall()
    ) == 1

    recovered = now + timedelta(days=2)
    bot.run_once(now=recovered, snapshot=snapshot(recovered, mid="100"))
    again = recovered + timedelta(hours=1)
    bot.run_once(now=again, snapshot=snapshot(again, mid="60"))
    assert (tmp_path / "DRAWDOWN_FREEZE").exists()
    assert read_kill(tmp_path) is None
    assert len(
        bot.ledger.conn.execute(
            "SELECT payload FROM events WHERE kind='drawdown_freeze'"
        ).fetchall()
    ) == 2
    bot.ledger.close()


def test_forty_percent_combined_drawdown_kills_and_requires_ack(tmp_path):
    import json
    from datetime import datetime, timezone

    from rhbot.cli import main
    from rhbot.models import OrderIntent

    wall = datetime.now(timezone.utc)
    opened = wall - timedelta(days=2)
    bot = engine(tmp_path, sma_window=3)
    bot.run_once(now=opened, snapshot=snapshot(opened))
    invest_at = wall - timedelta(days=1)
    view = snapshot(invest_at)
    for symbol in ("BTC-USD", "ETH-USD"):
        bot.broker.submit(
            "trend_daily",
            OrderIntent(symbol, "buy", "trend_entry", quote_amount=Decimal("500")),
            f"trend-{symbol}",
            bot._context("trend_daily", view, invest_at),
            invest_at,
        )
    assert bot.ledger.positions("trend_daily")
    crashed = snapshot(wall, mid="30")
    bot.run_once(now=wall, snapshot=crashed)
    kill = read_kill(tmp_path)
    assert kill is not None
    assert kill["by"] == "risk"
    assert kill["ack_required"] is True
    assert "drawdown" in kill["reason"]
    assert bot.ledger.positions("buy_and_hold") == {}
    assert bot.ledger.positions("trend_daily") == {}
    trips = bot.ledger.conn.execute(
        "SELECT payload FROM events WHERE kind='kill_trip'"
    ).fetchall()
    assert len(trips) == 1
    trip = json.loads(trips[0]["payload"])
    assert trip["reason"] == "max_drawdown"
    assert trip["limit_name"] == "max_drawdown_pct"
    assert trip["limit"] == "0.40"
    assert trip["ack_required"] is True
    assert Decimal(trip["observed"]) >= Decimal("0.40")
    logged = bot.ledger.conn.execute(
        "SELECT reason, limit_value FROM trade_log WHERE kind='kill_trip'"
    ).fetchall()
    assert len(logged) == 1
    assert logged[0]["reason"] == "max_drawdown"
    assert logged[0]["limit_value"] == "0.40"
    bot.run_once(now=wall, snapshot=crashed)
    assert (tmp_path / "KILL").exists()
    bot.ledger.close()

    assert main(["resume", "--state-dir", str(tmp_path)]) == 2
    assert (tmp_path / "KILL").exists()
    assert main(["ack-drawdown", "--state-dir", str(tmp_path)]) == 0
    assert (tmp_path / "KILL").exists()
    assert main(["resume", "--ack", "--state-dir", str(tmp_path)]) == 0
    assert not (tmp_path / "KILL").exists()

    bot = engine(tmp_path, sma_window=3)
    bot.run_once(now=wall, snapshot=crashed)
    assert read_kill(tmp_path) is None
    bot.ledger.close()


def test_flatten_works_while_killed_and_blocks_new_buys(tmp_path, now):
    bot = engine(tmp_path, sma_window=3)
    view = snapshot(now)
    bot.run_once(now=now, snapshot=view)
    from rhbot.ops import engage_kill

    engage_kill(tmp_path, "drill", "operator")
    result = bot.flatten(now=now, snapshot=view)
    assert result["errors"] == []
    assert bot.ledger.positions("buy_and_hold") == {}
    from rhbot.errors import OrderRejected
    from rhbot.models import OrderIntent

    with __import__("pytest").raises(OrderRejected) as caught:
        bot.broker.submit(
            "buy_and_hold",
            OrderIntent("BTC-USD", "buy", "again", quote_amount=Decimal("20")),
            "after-kill",
            bot._context("buy_and_hold", view, now),
            now,
        )
    assert "kill_switch" in caught.value.reasons
    bot.ledger.close()


def test_resume_refuses_when_something_else_is_critical(tmp_path, monkeypatch):
    from rhbot.cli import main

    code = main(["kill", "--state-dir", str(tmp_path), "--reason", "pause"])
    assert code == 0
    assert (tmp_path / "KILL").exists()

    def broken(settings, now=None):
        return {
            "health": "critical",
            "reasons": ["kill_switch", "reconciliation"],
        }

    monkeypatch.setattr("rhbot.cli.assess", broken)
    code = main(["resume", "--state-dir", str(tmp_path)])
    assert code == 2
    assert (tmp_path / "KILL").exists()
