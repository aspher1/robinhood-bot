from datetime import timedelta
from decimal import Decimal

from rhbot.models import Fill
from rhbot.strategies.buyhold import BuyAndHold
from rhbot.strategies.dca import DcaWeekly
from rhbot.strategies.trend import TrendDaily

from tests.conftest import engine, make_settings, snapshot


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


def test_dca_buys_once_per_iso_week(tmp_path, now):
    strategy = DcaWeekly(make_settings(tmp_path))
    view = snapshot(now)
    state = strategy.initial_state()
    first, state, reason = strategy.decide(view, state, {}, Decimal("1000"), Decimal("1000"), now)
    assert reason == "weekly_buy"
    assert [item.quote_amount for item in first] == [Decimal("25"), Decimal("25")]
    state = strategy.commit(
        state,
        [_fill("BTC-USD", "buy", now), _fill("ETH-USD", "buy", now)],
        {},
        now,
    )
    second, state, reason2 = strategy.decide(view, state, {}, Decimal("950"), Decimal("1000"), now)
    assert second == []
    assert reason2 == "already_bought_this_week"
    later = now + timedelta(days=7)
    third, _, reason3 = strategy.decide(view, state, {}, Decimal("950"), Decimal("1000"), later)
    assert reason3 == "weekly_buy"
    assert len(third) == 2


def test_trend_enters_above_band_and_holds_inside_it(tmp_path, now):
    settings = make_settings(tmp_path, sma_window=3, trend_band=Decimal("0.01"))
    strategy = TrendDaily(settings)
    last_open = now - timedelta(days=1)
    flat = snapshot(now, closes=["10", "10", "10"], last_open=last_open)
    orders, state, reason = strategy.decide(
        flat, strategy.initial_state(), {}, Decimal("1000"), Decimal("1000"), now
    )
    assert orders == []
    assert "hold" in reason

    hot = snapshot(now, closes=["10", "10", "12"], last_open=last_open)
    orders, state, reason = strategy.decide(
        hot, strategy.initial_state(), {}, Decimal("1000"), Decimal("1000"), now
    )
    assert {item.symbol for item in orders} == {"BTC-USD", "ETH-USD"}
    assert all(item.side == "buy" for item in orders)
    assert "enter" in reason

    again, _, reason2 = strategy.decide(hot, state, {}, Decimal("1000"), Decimal("1000"), now)
    assert again == []
    assert "already_decided_today" in reason2


def test_trend_min_hold_blocks_exit(tmp_path, now):
    settings = make_settings(tmp_path, sma_window=3, trend_band=Decimal("0.01"))
    strategy = TrendDaily(settings)
    last_open = now - timedelta(days=1)
    hot = snapshot(now, closes=["10", "10", "12"], last_open=last_open)
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
        closes=["10", "10", "12", "8"],
        last_open=crash_day - timedelta(days=1),
    )
    blocked, _, reason = strategy.decide(crash, state, positions, Decimal("0"), Decimal("1000"), crash_day)
    assert blocked == []
    assert "min_hold" in reason
    free_day = now + timedelta(days=7)
    free = snapshot(
        free_day,
        mid="8",
        closes=["12", "12", "8"],
        last_open=free_day - timedelta(days=1),
    )
    # The SMA window is the last 3 closes: 12, 12, 8. Last is below the band.
    exits, _, exit_reason = strategy.decide(free, state, positions, Decimal("0"), Decimal("800"), free_day)
    assert {item.side for item in exits} == {"sell"}
    assert "exit" in exit_reason


def test_each_sleeve_through_the_engine(tmp_path, now):
    bot = engine(tmp_path, sma_window=3, trend_band=Decimal("0.01"))
    last_open = now - timedelta(days=1)
    view = snapshot(now, closes=["10", "10", "12"], last_open=last_open)
    bot.run_once(now=now, snapshot=view)
    # Two trades a day, shared by every sleeve. Buy-and-hold uses the first day.
    assert bot.ledger.positions("buy_and_hold")["BTC-USD"] > 0
    assert bot.ledger.positions("dca_weekly") == {}
    assert bot.ledger.positions("trend_daily") == {}
    assert len(bot.ledger.fills_for("buy_and_hold")) == 2
    bot.run_once(now=now, snapshot=view)
    assert len(bot.ledger.fills_for("buy_and_hold")) == 2

    day1 = now + timedelta(days=1)
    view1 = snapshot(day1, closes=["10", "10", "12"], last_open=day1 - timedelta(days=1))
    bot.run_once(now=day1, snapshot=view1)
    assert len(bot.ledger.fills_for("dca_weekly")) == 2
    assert bot.ledger.fills_for("trend_daily") == []

    day2 = now + timedelta(days=2)
    view2 = snapshot(day2, closes=["10", "10", "12"], last_open=day2 - timedelta(days=1))
    bot.run_once(now=day2, snapshot=view2)
    assert len(bot.ledger.fills_for("trend_daily")) == 2
    assert len(bot.ledger.fills_for("buy_and_hold")) == 2
    for name in ("buy_and_hold", "dca_weekly", "trend_daily"):
        ok, detail = bot.ledger.reconcile(name)
        assert ok, detail
    bot.ledger.close()
