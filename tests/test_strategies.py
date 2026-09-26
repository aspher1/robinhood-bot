from datetime import timedelta
from decimal import Decimal

from rhbot.models import Fill
from rhbot.strategies.buyhold import BuyAndHold
from rhbot.strategies.dca import DcaWeekly
from rhbot.strategies.trend import TrendDaily

from tests.conftest import engine, make_settings, padded_closes, snapshot


def _fill(symbol: str, side: str, now) -> Fill:
    qty = Decimal("1")
    return Fill(
        sleeve="test",
        symbol=symbol,
        side=side,
        qty=qty,
        qty_delta=qty if side == "buy" else -qty,
        mid=Decimal("100"),
        fill_price=Decimal("101") if side == "buy" else Decimal("99"),
        cash_delta=Decimal("-101") if side == "buy" else Decimal("99"),
        cost=Decimal("1"),
        notional=Decimal("100"),
        ts=now,
        client_order_id=f"{side}-{symbol}",
        reason="test",
    )


def test_buy_and_hold_deploys_once(tmp_path, now):
    strategy = BuyAndHold(make_settings(tmp_path))
    view = snapshot(now)
    first, state, reason = strategy.decide(view, strategy.initial_state(), {}, Decimal("1000"), Decimal("1000"), now)
    assert reason == "deploy"
    assert len(first) == 2
    assert all(item.quote_amount == Decimal("500.00") for item in first)
    held = {"BTC-USD": Decimal("1"), "ETH-USD": Decimal("1")}
    state = strategy.commit(state, [_fill("BTC-USD", "buy", now), _fill("ETH-USD", "buy", now)], held, now)
    second, _, reason2 = strategy.decide(view, state, held, Decimal("0"), Decimal("1000"), now)
    assert second == []
    assert reason2 == "holding"


def test_dca_alternates_one_coin_at_19_23(tmp_path, now):
    from rhbot.strategies.dca import schedule_index

    strategy = DcaWeekly(make_settings(tmp_path))
    view = snapshot(now)
    state = strategy.initial_state()
    expected = ["BTC-USD", "ETH-USD", "BTC-USD", "ETH-USD"]
    for step, symbol in enumerate(expected):
        when = now + timedelta(days=7 * step)
        assert schedule_index(when, now) == step
        orders, state, reason = strategy.decide(
            view, state, {}, Decimal("1000"), Decimal("1000"), when
        )
        assert reason == "dca_buy"
        assert len(orders) == 1
        assert orders[0].symbol == symbol
        assert orders[0].quote_amount == Decimal("19.23")
        state = strategy.commit(state, [_fill(symbol, "buy", when)], {}, when)
        again, state, reason2 = strategy.decide(
            view, state, {}, Decimal("950"), Decimal("1000"), when
        )
        assert again == []
        assert reason2 == "already_scheduled"


def test_trend_enters_above_band_and_holds_inside_it(tmp_path, now):
    settings = make_settings(tmp_path)
    strategy = TrendDaily(settings)
    last_open = now - timedelta(days=1)
    flat = snapshot(now, closes=padded_closes("10"), last_open=last_open)
    orders, state, reason = strategy.decide(
        flat, strategy.initial_state(), {}, Decimal("1000"), Decimal("1000"), now
    )
    assert orders == []
    assert "hold" in reason

    hot = snapshot(now, closes=padded_closes("12"), last_open=last_open)
    orders, state, reason = strategy.decide(
        hot, strategy.initial_state(), {}, Decimal("1000"), Decimal("1000"), now
    )
    assert {item.symbol for item in orders} == {"BTC-USD", "ETH-USD"}
    assert all(item.side == "buy" and item.quote_amount is not None and item.quote_amount > 0 for item in orders)
    assert orders[0].quote_amount == Decimal("500.00")
    assert orders[1].quote_amount <= Decimal("500.00")
    assert "enter" in reason

    again, _, reason2 = strategy.decide(hot, state, {}, Decimal("1000"), Decimal("1000"), now)
    assert again == []
    assert "already_decided_today" in reason2


def test_trend_min_hold_blocks_exit(tmp_path, now):
    settings = make_settings(tmp_path)
    strategy = TrendDaily(settings)
    last_open = now - timedelta(days=1)
    hot = snapshot(now, closes=padded_closes("12"), last_open=last_open)
    orders, state, _ = strategy.decide(
        hot, strategy.initial_state(), {}, Decimal("1000"), Decimal("1000"), now
    )
    assert orders
    positions = {"BTC-USD": Decimal("1"), "ETH-USD": Decimal("1")}
    state = strategy.commit(state, [], positions, now)
    crash_day = now + timedelta(days=1)
    crash = snapshot(
        crash_day,
        mid="8",
        closes=padded_closes("8", base="12"),
        last_open=crash_day - timedelta(days=1),
    )
    blocked, _, reason = strategy.decide(crash, state, positions, Decimal("0"), Decimal("1000"), crash_day)
    assert blocked == []
    assert "min_hold" in reason
    free_day = now + timedelta(days=7)
    free = snapshot(
        free_day,
        mid="8",
        closes=padded_closes("8", base="12"),
        last_open=free_day - timedelta(days=1),
    )
    # The last close is below the frozen 2% band.
    exits, _, exit_reason = strategy.decide(free, state, positions, Decimal("0"), Decimal("800"), free_day)
    assert {item.side for item in exits} == {"sell"}
    assert "exit" in exit_reason
    flat = strategy.commit(state, [], {}, free_day)
    reentry_day = free_day + timedelta(days=1)
    hot_again = snapshot(
        reentry_day,
        closes=padded_closes("12", base="8"),
        last_open=reentry_day - timedelta(days=1),
    )
    reentry, _, reentry_reason = strategy.decide(
        hot_again, flat, {}, Decimal("800"), Decimal("800"), reentry_day
    )
    assert {item.side for item in reentry} == {"buy"}
    assert "enter" in reentry_reason


def test_each_sleeve_through_the_engine(tmp_path, now):
    bot = engine(tmp_path)
    last_open = now - timedelta(days=1)
    view = snapshot(now, closes=padded_closes("12"), last_open=last_open)
    bot.run_once(now=now, snapshot=view)
    # The 2-trade cap is per book, so all three can trade on day 1.
    assert len(bot.ledger.fills_for("buy_and_hold")) == 2
    assert len(bot.ledger.fills_for("dca_weekly")) == 1
    assert bot.ledger.fills_for("dca_weekly")[0]["symbol"] == "BTC-USD"
    assert len(bot.ledger.fills_for("trend_daily")) == 2
    bot.run_once(now=now, snapshot=view)
    assert len(bot.ledger.fills_for("buy_and_hold")) == 2
    assert len(bot.ledger.fills_for("dca_weekly")) == 1
    assert len(bot.ledger.fills_for("trend_daily")) == 2
    assert bot.ledger.get_meta("paper_day1")
    for name in ("buy_and_hold", "dca_weekly", "trend_daily"):
        ok, detail = bot.ledger.reconcile(name)
        assert ok, detail
    bot.ledger.close()
