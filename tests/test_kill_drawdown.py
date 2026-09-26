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


def test_ack_drawdown_requires_a_strategy_and_note(tmp_path):
    from rhbot.cli import main

    with __import__("pytest").raises(SystemExit) as caught:
        main(["ack-drawdown", "--state-dir", str(tmp_path)])
    assert caught.value.code == 2


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
