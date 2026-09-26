from datetime import datetime, timezone
from decimal import Decimal

from rhbot.models import OrderIntent, Quote
from rhbot.pricing import execution_price, plan_fill

from tests.conftest import engine, snapshot

WHEN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_public_mid_charges_one_percent_per_side():
    quote = Quote(symbol="BTC-USD", ts=WHEN, mid=Decimal("100"), source="test")
    buy = OrderIntent("BTC-USD", "buy", "test", quote_amount=Decimal("101"))
    qty, price, cash_delta, cost, notional = plan_fill(buy, quote, Decimal("0.01"))
    assert price == Decimal("101")
    assert qty == Decimal("1")
    assert cash_delta == Decimal("-101")
    assert cost == Decimal("1")
    assert notional == Decimal("100")

    sell = OrderIntent("BTC-USD", "sell", "test", base_quantity=Decimal("1"))
    sqty, sprice, scash, scost, _ = plan_fill(sell, quote, Decimal("0.01"))
    assert sqty == Decimal("1")
    assert sprice == Decimal("99")
    assert scash == Decimal("99")
    assert scost == Decimal("1")


def test_round_trip_loses_about_two_percent(tmp_path, now):
    bot = engine(tmp_path, min_hold_days=0, sma_window=3)
    bot.ledger.ensure_sleeve("buy_and_hold", now, {})
    view = snapshot(now)
    buy = OrderIntent("BTC-USD", "buy", "test", quote_amount=Decimal("101"))
    bot.broker.submit("buy_and_hold", buy, "buy-1", bot._context("buy_and_hold", view, now), now)
    sell = OrderIntent("BTC-USD", "sell", "test", base_quantity=Decimal("1"))
    bot.broker.submit("buy_and_hold", sell, "sell-1", bot._context("buy_and_hold", view, now), now)
    assert bot.ledger.cash("buy_and_hold") == Decimal("998.00000000")
    assert bot.ledger.positions("buy_and_hold") == {}
    ok, _ = bot.ledger.reconcile("buy_and_hold")
    assert ok
    bot.ledger.close()


def test_custom_half_percent_cost():
    quote = Quote(symbol="ETH-USD", ts=WHEN, mid=Decimal("200"), source="test")
    assert execution_price(quote, "buy", Decimal("0.005")) == Decimal("201")
    assert execution_price(quote, "sell", Decimal("0.005")) == Decimal("199")


def test_inclusive_bid_ask_is_not_charged_twice():
    quote = Quote(
        symbol="BTC-USD",
        ts=WHEN,
        mid=Decimal("100"),
        source="robinhood",
        bid=Decimal("99.5"),
        ask=Decimal("100.5"),
        spread_included=True,
    )
    buy = OrderIntent("BTC-USD", "buy", "test", quote_amount=Decimal("100.5"))
    qty, price, cash_delta, cost, _ = plan_fill(buy, quote, Decimal("0.01"))
    assert price == Decimal("100.5")
    assert qty == Decimal("1")
    assert cash_delta == Decimal("-100.5")
    assert cost == Decimal("0.5")
    sell = OrderIntent("BTC-USD", "sell", "test", base_quantity=Decimal("1"))
    _, sprice, scash, _, _ = plan_fill(sell, quote, Decimal("0.01"))
    assert sprice == Decimal("99.5")
    assert scash == Decimal("99.5")


def test_smaller_buy_gets_fewer_coins():
    quote = Quote(symbol="BTC-USD", ts=WHEN, mid=Decimal("100"), source="test")
    buy = OrderIntent("BTC-USD", "buy", "test", quote_amount=Decimal("100"))
    qty, price, cash_delta, _, _ = plan_fill(buy, quote, Decimal("0.01"))
    assert price == Decimal("101")
    assert qty < Decimal("1")
    assert cash_delta < 0
    assert abs(cash_delta) <= Decimal("100")
