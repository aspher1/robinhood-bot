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
    from datetime import timedelta

    bot = engine(tmp_path, sma_window=3)
    bot.ledger.ensure_sleeve("buy_and_hold", now, {})
    view = snapshot(now)
    buy = OrderIntent("BTC-USD", "buy", "test", quote_amount=Decimal("101"))
    bot.broker.submit("buy_and_hold", buy, "buy-1", bot._context("buy_and_hold", view, now), now)
    later = now + timedelta(days=7)
    later_view = snapshot(later)
    sell = OrderIntent("BTC-USD", "sell", "test", base_quantity=Decimal("1"))
    bot.broker.submit(
        "buy_and_hold",
        sell,
        "sell-1",
        bot._context("buy_and_hold", later_view, later),
        later,
    )
    assert bot.ledger.cash("buy_and_hold") == Decimal("998.00000000")
    assert bot.ledger.positions("buy_and_hold") == {}
    ok, _ = bot.ledger.reconcile("buy_and_hold")
    assert ok
    bot.ledger.close()


def test_cost_below_one_percent_is_refused():
    import pytest

    from rhbot.errors import DataError

    quote = Quote(symbol="ETH-USD", ts=WHEN, mid=Decimal("200"), source="test")
    with pytest.raises(DataError):
        execution_price(quote, "buy", Decimal("0.005"))


def test_inclusive_quote_is_floored_at_one_percent():
    quote = Quote(
        symbol="BTC-USD",
        ts=WHEN,
        mid=Decimal("100"),
        source="robinhood",
        bid=Decimal("99.5"),
        ask=Decimal("100.5"),
        spread_included=True,
    )
    buy = OrderIntent("BTC-USD", "buy", "test", quote_amount=Decimal("101"))
    qty, price, cash_delta, cost, _ = plan_fill(buy, quote, Decimal("0.01"))
    assert price == Decimal("101")
    assert qty == Decimal("1")
    assert cash_delta == Decimal("-101")
    assert cost == Decimal("1")
    sell = OrderIntent("BTC-USD", "sell", "test", base_quantity=Decimal("1"))
    _, sprice, scash, _, _ = plan_fill(sell, quote, Decimal("0.01"))
    assert sprice == Decimal("99")
    assert scash == Decimal("99")


def test_wide_inclusive_spread_is_kept():
    quote = Quote(
        symbol="BTC-USD",
        ts=WHEN,
        mid=Decimal("100"),
        source="robinhood",
        bid=Decimal("97"),
        ask=Decimal("103"),
        spread_included=True,
    )
    assert execution_price(quote, "buy", Decimal("0.01")) == Decimal("103")
    assert execution_price(quote, "sell", Decimal("0.01")) == Decimal("97")


def test_smaller_buy_gets_fewer_coins():
    quote = Quote(symbol="BTC-USD", ts=WHEN, mid=Decimal("100"), source="test")
    buy = OrderIntent("BTC-USD", "buy", "test", quote_amount=Decimal("100"))
    qty, price, cash_delta, _, _ = plan_fill(buy, quote, Decimal("0.01"))
    assert price == Decimal("101")
    assert qty < Decimal("1")
    assert cash_delta < 0
    assert abs(cash_delta) <= Decimal("100")


def test_repeat_client_order_id_returns_the_same_fill(tmp_path, now):
    bot = engine(tmp_path)
    bot.ledger.ensure_sleeve("buy_and_hold", now, {})
    view = snapshot(now)
    buy = OrderIntent("BTC-USD", "buy", "test", quote_amount=Decimal("101"))
    ctx = bot._context("buy_and_hold", view, now)
    first = bot.broker.submit("buy_and_hold", buy, "same-id", ctx, now)
    second = bot.broker.submit(
        "buy_and_hold", buy, "same-id", bot._context("buy_and_hold", view, now), now
    )
    assert second.client_order_id == first.client_order_id
    assert second.qty == first.qty
    assert len(bot.ledger.fills_for("buy_and_hold")) == 1
    bot.ledger.close()


def test_one_order_per_symbol_per_bar(tmp_path, now):
    import pytest

    from rhbot.errors import OrderRejected

    bot = engine(tmp_path)
    bot.ledger.ensure_sleeve("buy_and_hold", now, {})
    view = snapshot(now)
    buy = OrderIntent("BTC-USD", "buy", "test", quote_amount=Decimal("100"))
    bot.broker.submit("buy_and_hold", buy, "first", bot._context("buy_and_hold", view, now), now)
    again = OrderIntent("BTC-USD", "buy", "test", quote_amount=Decimal("100"))
    with pytest.raises(OrderRejected) as caught:
        bot.broker.submit(
            "buy_and_hold",
            again,
            "second",
            bot._context("buy_and_hold", view, now),
            now,
        )
    assert caught.value.reasons == ["one_order_per_symbol_per_bar"]
    bot.ledger.close()
