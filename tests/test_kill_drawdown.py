from datetime import timedelta
from decimal import Decimal

from rhbot.models import Quote
from rhbot.ops import read_kill

from tests.conftest import engine, snapshot


def test_drawdown_engages_the_kill_file(tmp_path, now):
    bot = engine(tmp_path, sma_window=3, min_hold_days=0)
    bot.run_once(now=now, snapshot=snapshot(now))
    assert not (tmp_path / "KILL").exists()
    later = now + timedelta(days=2)
    crashed = snapshot(later, mid="40")
    bot.run_once(now=later, snapshot=crashed)
    kill = read_kill(tmp_path)
    assert kill is not None
    assert kill["by"] == "risk"
    assert "drawdown" in kill["reason"]
    # The kill stops new risk. Buy-and-hold must not have sold itself.
    assert bot.ledger.positions("buy_and_hold")["BTC-USD"] > 0
    bot.ledger.close()


def test_flatten_works_while_killed_and_blocks_new_buys(tmp_path, now):
    bot = engine(tmp_path, sma_window=3, min_hold_days=0)
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
