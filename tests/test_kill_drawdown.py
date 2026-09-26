from datetime import timedelta
from decimal import Decimal

from rhbot.ops import read_kill

from tests.conftest import engine, snapshot


def _exposure(bot, sleeve: str, view, mid: str) -> tuple[Decimal, Decimal]:
    positions = bot.ledger.positions(sleeve)
    exposure = sum((qty * Decimal(mid) for qty in positions.values()), Decimal(0))
    return exposure, bot.mark(sleeve, view)


def test_five_percent_drawdown_cuts_exposure_to_half(tmp_path, now):
    bot = engine(tmp_path, sma_window=3)
    bot.run_once(now=now, snapshot=snapshot(now))
    later = now + timedelta(hours=2)
    view = snapshot(later, mid="94")
    bot.run_once(now=later, snapshot=view)
    assert read_kill(tmp_path) is None
    exposure, equity = _exposure(bot, "buy_and_hold", view, "94")
    assert exposure > 0
    assert exposure <= equity * Decimal("0.50") + Decimal("0.50")
    bot.ledger.close()


def test_seven_point_five_percent_drawdown_cuts_exposure_to_quarter(tmp_path, now):
    bot = engine(tmp_path, sma_window=3)
    bot.run_once(now=now, snapshot=snapshot(now))
    later = now + timedelta(hours=2)
    view = snapshot(later, mid="92")
    bot.run_once(now=later, snapshot=view)
    assert read_kill(tmp_path) is None
    exposure, equity = _exposure(bot, "buy_and_hold", view, "92")
    assert exposure > 0
    assert exposure <= equity * Decimal("0.25") + Decimal("0.50")
    bot.ledger.close()


def test_ten_percent_drawdown_kills_flattens_and_requires_ack(tmp_path, now):
    from datetime import datetime, timezone

    from rhbot.cli import main

    wall = datetime.now(timezone.utc)
    bot = engine(tmp_path, sma_window=3)
    bot.run_once(now=wall, snapshot=snapshot(wall))
    crashed = snapshot(wall, mid="80")
    bot.run_once(now=wall, snapshot=crashed)
    kill = read_kill(tmp_path)
    assert kill is not None
    assert kill["by"] == "risk"
    assert kill["ack_required"] is True
    assert "drawdown" in kill["reason"]
    assert bot.ledger.positions("buy_and_hold") == {}
    trips = bot.ledger.conn.execute(
        "SELECT payload FROM events WHERE kind='kill_trip'"
    ).fetchall()
    assert len(trips) == 1
    trip = __import__("json").loads(trips[0]["payload"])
    assert trip["reason"] == "max_drawdown"
    assert trip["limit_name"] == "max_drawdown_pct"
    assert trip["limit"] == "0.10"
    assert trip["ack_required"] is True
    logged = bot.ledger.conn.execute(
        "SELECT reason, limit_value FROM trade_log WHERE kind='kill_trip'"
    ).fetchall()
    assert len(logged) == 1
    assert logged[0]["reason"] == "max_drawdown"
    assert logged[0]["limit_value"] == "0.10"
    bot.ledger.close()

    assert main(["resume", "--state-dir", str(tmp_path)]) == 2
    assert (tmp_path / "KILL").exists()
    assert main(["resume", "--ack", "--state-dir", str(tmp_path)]) == 0
    assert not (tmp_path / "KILL").exists()


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
