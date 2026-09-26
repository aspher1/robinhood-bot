"""Trend entries size to each coin's cash and the existing caps."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from rhbot.backtest import replay
from rhbot.models import Bar, MarketSnapshot, OrderIntent, Quote
from rhbot.money import D, q8, q_cent
from rhbot.overlay import mark_to_bid_equity
from rhbot.pricing import plan_fill
from rhbot.risk import EPS
from rhbot.strategies.trend import TrendDaily, largest_entry_quote, sleeve_cash_map

from tests.conftest import engine, make_bars, make_settings, padded_closes, snapshot

UTC = timezone.utc


def _view(now, btc_closes, eth_closes, btc_mid="100", eth_mid="100") -> MarketSnapshot:
    last_open = now - timedelta(days=1)
    quotes = {
        "BTC-USD": Quote(
            symbol="BTC-USD",
            ts=now,
            mid=Decimal(btc_mid),
            bid=Decimal(btc_mid),
            ask=Decimal(btc_mid),
            source="test",
        ),
        "ETH-USD": Quote(
            symbol="ETH-USD",
            ts=now,
            mid=Decimal(eth_mid),
            bid=Decimal(eth_mid),
            ask=Decimal(eth_mid),
            source="test",
        ),
    }
    return MarketSnapshot(
        bars={
            "BTC-USD": make_bars("BTC-USD", btc_closes, last_open),
            "ETH-USD": make_bars("ETH-USD", eth_closes, last_open),
        },
        quotes=quotes,
        source="test",
    )


def _entries(bot, sleeve="trend_daily"):
    rows = bot.ledger.conn.execute(
        "SELECT payload FROM events WHERE kind='decision' ORDER BY seq"
    ).fetchall()
    found = []
    for row in rows:
        payload = json.loads(row["payload"])
        if payload.get("sleeve") != sleeve:
            continue
        for order in payload.get("orders") or []:
            if order.get("reason") == "trend_entry":
                found.append(order)
    return found


def _assert_order_within_caps(settings, order, equity, cash, positions, quotes, turnover, turnover_base):
    amount = D(order["quote_amount"])
    symbol = order["symbol"]
    assert amount >= settings.min_order_notional
    assert largest_entry_quote(
        symbol,
        sleeve_cash=amount,
        ledger_cash=cash,
        equity=equity,
        positions=positions,
        quotes=quotes,
        turnover_today=turnover,
        turnover_base=turnover_base,
        settings=settings,
    ) >= amount
    intent = OrderIntent(symbol, "buy", "trend_entry", quote_amount=amount)
    qty, _px, _cash, _cost, notional = plan_fill(intent, quotes[symbol], settings.cost_per_side)
    trade_cap = min(settings.max_position_pct, settings.max_total_exposure_pct)
    assert notional <= equity * trade_cap + EPS
    assert (positions.get(symbol, Decimal(0)) + qty) * quotes[symbol].mid <= equity * trade_cap + EPS
    exposure = Decimal(0)
    for name, held in positions.items():
        exposure += held * quotes[name].mid
    exposure += qty * quotes[symbol].mid
    assert exposure <= equity * settings.max_total_exposure_pct + EPS
    assert turnover + amount <= turnover_base * settings.max_daily_turnover_pct + EPS
    return qty, notional


def test_both_sleeves_enter_when_equity_is_under_1000(tmp_path, now):
    bot = engine(tmp_path)
    flat = _view(now, padded_closes("10"), padded_closes("10"))
    bot.run_once(now=now, snapshot=flat)
    bot.ledger.conn.execute(
        """
        UPDATE sleeves
        SET cash='999.00000000', last_equity='999.00000000'
        WHERE name='trend_daily'
        """
    )
    bot.ledger.conn.commit()
    state = bot.ledger.strategy_state("trend_daily")
    state["sleeve_cash"] = {"BTC-USD": "499.50000000", "ETH-USD": "499.50000000"}
    bot.ledger.save_strategy_state("trend_daily", state)
    later = now + timedelta(days=1)
    hot = _view(later, padded_closes("12"), padded_closes("12"))
    bot.run_once(now=later, snapshot=hot)
    fills = [row for row in bot.ledger.fills_for("trend_daily") if row["reason"] == "trend_entry"]
    assert {row["symbol"] for row in fills} == {"BTC-USD", "ETH-USD"}
    orders = [order for order in _entries(bot) if order["symbol"] in ("BTC-USD", "ETH-USD")]
    assert [order["symbol"] for order in orders] == ["BTC-USD", "ETH-USD"]
    equity = Decimal("999")
    cash = Decimal("999")
    turnover_base = equity
    positions: dict[str, Decimal] = {}
    turnover = Decimal(0)
    for order in orders:
        qty, notional = _assert_order_within_caps(
            bot.settings, order, equity, cash, positions, hot.quotes, turnover, turnover_base
        )
        symbol = order["symbol"]
        positions[symbol] = qty
        cash = q8(cash + plan_fill(
            OrderIntent(symbol, "buy", "trend_entry", quote_amount=D(order["quote_amount"])),
            hot.quotes[symbol],
            bot.settings.cost_per_side,
        )[2])
        turnover = q8(turnover + notional)
        equity = mark_to_bid_equity(cash, positions, hot.quotes, bot.settings.cost_per_side)
    denials = bot.ledger.conn.execute(
        "SELECT payload FROM events WHERE kind='risk_denial'"
    ).fetchall()
    for row in denials:
        payload = json.loads(row["payload"])
        assert not (
            payload.get("sleeve") == "trend_daily" and payload.get("reason") == "per_trade_cap"
        )
    bot.ledger.close()


def test_reentry_uses_the_reduced_sleeve_cash(tmp_path, now):
    bot = engine(tmp_path)
    entry = _view(now, padded_closes("12"), padded_closes("10"))
    bot.run_once(now=now, snapshot=entry)
    first = [row for row in bot.ledger.fills_for("trend_daily") if row["reason"] == "trend_entry"]
    assert [row["symbol"] for row in first] == ["BTC-USD"]
    opened = sleeve_cash_map(bot.ledger.strategy_state("trend_daily"))
    assert opened["ETH-USD"] == Decimal("500.00000000")
    assert opened["BTC-USD"] < Decimal("500")

    exit_day = now + timedelta(days=7)
    btc_hist = padded_closes("12") + ["10"] * 6 + ["8"]
    eth_hist = padded_closes("10") + ["10"] * 7
    exited = _view(exit_day, btc_hist, eth_hist, btc_mid="95", eth_mid="100")
    bot.run_once(now=exit_day, snapshot=exited)
    assert bot.ledger.positions("trend_daily") == {}
    after = sleeve_cash_map(bot.ledger.strategy_state("trend_daily"))
    assert after["ETH-USD"] == Decimal("500.00000000")
    assert after["BTC-USD"] < opened["BTC-USD"] + Decimal("500")
    assert after["BTC-USD"] < Decimal("500")
    assert after["BTC-USD"] > Decimal("400")

    again = exit_day + timedelta(days=1)
    btc_back = btc_hist + ["30"]
    eth_back = eth_hist + ["30"]
    restart = _view(again, btc_back, eth_back)
    bot.run_once(now=again, snapshot=restart)
    reentries = [
        order
        for order in _entries(bot)
        if order["symbol"] in ("BTC-USD", "ETH-USD")
    ]
    # The first list includes the original BTC entry. Keep the restart day only.
    restart_orders = reentries[-2:]
    assert [order["symbol"] for order in restart_orders] == ["BTC-USD", "ETH-USD"]
    btc_amount = D(restart_orders[0]["quote_amount"])
    eth_amount = D(restart_orders[1]["quote_amount"])
    assert btc_amount == q_cent(after["BTC-USD"])
    assert btc_amount < Decimal("500")
    assert eth_amount > btc_amount
    assert eth_amount <= Decimal("500")
    fills = [row for row in bot.ledger.fills_for("trend_daily") if row["reason"] == "trend_entry"]
    assert [row["symbol"] for row in fills] == ["BTC-USD", "BTC-USD", "ETH-USD"]
    bot.ledger.close()


def test_entry_never_exceeds_a_cap(tmp_path, now):
    settings = make_settings(tmp_path)
    strategy = TrendDaily(settings)
    later = now + timedelta(days=1)
    hot = _view(later, padded_closes("12"), padded_closes("12"), btc_mid="100", eth_mid="100")
    state = strategy.initial_state()
    state["sleeve_cash"] = {"BTC-USD": "499.50", "ETH-USD": "499.50"}
    orders, _state, reason = strategy.decide(
        hot, state, {}, Decimal("999"), Decimal("999"), later
    )
    assert "enter" in reason
    assert [item.symbol for item in orders] == ["BTC-USD", "ETH-USD"]
    equity = Decimal("999")
    cash = Decimal("999")
    turnover_base = equity
    positions: dict[str, Decimal] = {}
    turnover = Decimal(0)
    for intent in orders:
        order = {"symbol": intent.symbol, "quote_amount": format(intent.quote_amount, "f")}
        qty, notional = _assert_order_within_caps(
            settings, order, equity, cash, positions, hot.quotes, turnover, turnover_base
        )
        _qty, _px, cash_delta, _cost, _notional = plan_fill(
            intent, hot.quotes[intent.symbol], settings.cost_per_side
        )
        positions[intent.symbol] = qty
        cash = q8(cash + cash_delta)
        turnover = q8(turnover + notional)
        equity = mark_to_bid_equity(cash, positions, hot.quotes, settings.cost_per_side)
        assert notional <= Decimal("499.50") + EPS


def test_replay_and_live_size_trend_entries_the_same(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    closes = ["10"] * 199 + ["12"]
    bars = {
        symbol: make_bars(symbol, closes, start + timedelta(days=199))
        for symbol in ("BTC-USD", "ETH-USD")
    }
    entry = datetime(2026, 1, 1, 15, tzinfo=UTC) + timedelta(days=200)
    quotes = {
        symbol: Quote(
            symbol=symbol,
            ts=entry,
            mid=Decimal("12"),
            bid=Decimal("12"),
            ask=Decimal("12"),
            source="test",
        )
        for symbol in bars
    }
    bot = engine(tmp_path)
    bot.run_once(now=entry, snapshot=MarketSnapshot(bars=bars, quotes=quotes, source="test"))
    live = _entries(bot, "trend_daily") + _entries(bot, "trend_daily_shadow")
    paper_day1 = bot.ledger.get_meta("paper_day1")
    bot.ledger.close()
    replayed = replay(make_settings(tmp_path / "unused"), bars, paper_day1=paper_day1)
    try:
        shadow = _entries(replayed, "trend_daily") + _entries(replayed, "trend_daily_shadow")
    finally:
        replayed.ledger.close()
    assert live
    assert [(row["symbol"], row["quote_amount"]) for row in live] == [
        (row["symbol"], row["quote_amount"]) for row in shadow
    ]


def test_trend_skips_an_entry_under_ten_dollars(tmp_path, now):
    bot = engine(tmp_path)
    bot.run_once(now=now, snapshot=_view(now, padded_closes("10"), padded_closes("10")))
    bot.ledger.conn.execute(
        """
        UPDATE sleeves
        SET cash='18.00000000', peak_equity='18.00000000',
            day_start_equity='18.00000000', last_equity='18.00000000'
        WHERE name='trend_daily'
        """
    )
    bot.ledger.conn.execute(
        """
        UPDATE overlay_books
        SET peak='18.00000000', equity='18.00000000', dd='0'
        WHERE sleeve='trend_daily'
        """
    )
    bot.ledger.conn.commit()
    state = bot.ledger.strategy_state("trend_daily")
    state["sleeve_cash"] = {"BTC-USD": "9.00000000", "ETH-USD": "9.00000000"}
    bot.ledger.save_strategy_state("trend_daily", state)
    later = now + timedelta(days=1)
    bot.run_once(now=later, snapshot=_view(later, padded_closes("12"), padded_closes("12")))
    assert [row for row in bot.ledger.fills_for("trend_daily") if row["reason"] == "trend_entry"] == []
    reasons = [
        json.loads(row["payload"]).get("reason", "")
        for row in bot.ledger.conn.execute(
            "SELECT payload FROM events WHERE kind='decision'"
        ).fetchall()
        if json.loads(row["payload"]).get("sleeve") == "trend_daily"
    ]
    assert any("below_min" in reason for reason in reasons)
    bot.ledger.close()
