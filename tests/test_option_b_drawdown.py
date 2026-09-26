"""Option B client-order ids. The per-book freeze and kill live in test_audit_findings."""

from datetime import timedelta
from decimal import Decimal

from rhbot.models import OrderIntent

from tests.conftest import engine, snapshot


def test_same_daily_intent_returns_the_original_fill(tmp_path, now):
    bot = engine(tmp_path)
    view = snapshot(now)
    bot.run_once(now=now, snapshot=view)
    day = now.date().isoformat()
    intent = OrderIntent("BTC-USD", "buy", "same_day", quote_amount=Decimal("100"))
    client_id = bot._client_id("buy_and_hold", intent, now)
    assert client_id == f"buy_and_hold:BTC-USD:buy:{day}"
    cash = bot.ledger.cash("buy_and_hold")
    qty = bot.ledger.positions("buy_and_hold")["BTC-USD"]
    notional = bot.ledger.conn.execute(
        "SELECT COALESCE(SUM(notional), '0') AS n FROM fills WHERE client_order_id=?",
        (client_id,),
    ).fetchone()["n"]
    first = bot.broker.submit(
        "buy_and_hold", intent, client_id, bot._context("buy_and_hold", view, now), now
    )
    second = bot.broker.submit(
        "buy_and_hold",
        OrderIntent("BTC-USD", "buy", "same_day_again", quote_amount=Decimal("100")),
        bot._client_id("buy_and_hold", intent, now + timedelta(hours=3)),
        bot._context("buy_and_hold", view, now),
        now,
    )
    assert first.client_order_id == client_id
    assert second.client_order_id == client_id
    assert second.notional == first.notional
    assert bot.ledger.cash("buy_and_hold") == cash
    assert bot.ledger.positions("buy_and_hold")["BTC-USD"] == qty
    rows = bot.ledger.conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(notional), '0') AS notional FROM fills WHERE client_order_id=?",
        (client_id,),
    ).fetchone()
    assert rows["n"] == 1
    assert rows["notional"] == notional
    bot.ledger.close()
