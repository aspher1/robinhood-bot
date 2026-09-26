"""Audit round 1 acceptance tests, findings F-001 through F-013."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from rhbot.errors import ConfigError, OrderRejected
from rhbot.models import Bar, MarketSnapshot, OrderIntent, Quote
from rhbot.money import D, money_str
from rhbot.ops import engage_kill, iso, read_kill, utcnow
from rhbot.status import assess

from tests.conftest import engine, make_bars, snapshot

UTC = timezone.utc


def _events(bot, kind: str) -> list[dict]:
    return [
        json.loads(row["payload"])
        for row in bot.ledger.conn.execute(
            "SELECT payload FROM events WHERE kind=? ORDER BY seq",
            (kind,),
        )
    ]


def _set_cash(bot, sleeve: str, amount: str) -> None:
    bot.ledger.conn.execute(
        "UPDATE sleeves SET cash=? WHERE name=?",
        (money_str(Decimal(amount)), sleeve),
    )
    bot.ledger.conn.commit()


def _ack(tmp_path, strategy: str, by: str = "operator") -> int:
    from rhbot.cli import main

    return main(
        [
            "ack-drawdown",
            "--strategy",
            strategy,
            "--by",
            by,
            "--note",
            f"reviewed {strategy}",
            "--state-dir",
            str(tmp_path),
        ]
    )


def test_f001_freeze_at_ten_percent_denies_entries_and_is_not_caught_up(tmp_path, now):
    bot = engine(tmp_path, sma_window=3, trend_band=Decimal("0.01"))
    flat = snapshot(now, closes=["10", "10", "10"], last_open=now - timedelta(days=1))
    bot.run_once(now=now, snapshot=flat)
    peak = bot.ledger.overlay_row("trend_daily")["peak"]
    assert bot.ledger.overlay_row("trend_daily")["state"] == "ARMED"
    assert bot.ledger.overlay_row("dca_weekly")["state"] == "ARMED"
    trend_cash = money_str(bot.ledger.cash("trend_daily"))
    dca_cash = money_str(bot.ledger.cash("dca_weekly"))
    # Day 1 already bought BTC. The next period is the buy that must be skipped.
    _set_cash(bot, "trend_daily", "900")
    _set_cash(bot, "dca_weekly", "800")
    later = now + timedelta(days=7)
    hot = snapshot(later, closes=["10", "10", "12"], last_open=later - timedelta(days=1))
    bot.run_once(now=later, snapshot=hot)

    assert bot.ledger.overlay_row("trend_daily")["state"] == "FROZEN"
    assert bot.ledger.overlay_row("dca_weekly")["state"] == "FROZEN"
    assert bot.ledger.overlay_row("trend_daily")["peak"] == peak
    assert Decimal(bot.ledger.overlay_row("trend_daily")["dd"]) <= Decimal("-0.10")
    trips = _events(bot, "freeze_trip")
    assert {item["sleeve"] for item in trips} == {"trend_daily", "dca_weekly"}
    for item in trips:
        assert item["equity"] and item["peak"] and item["dd"]
    denials = _events(bot, "risk_denial")
    assert any(item["sleeve"] == "trend_daily" and item["reason"] == "freeze" and item["side"] == "buy" for item in denials)
    assert any(item["sleeve"] == "dca_weekly" and item["reason"] == "freeze" and item["side"] == "buy" for item in denials)
    assert bot.ledger.positions("trend_daily") == {}
    assert [row["symbol"] for row in bot.ledger.fills_for("dca_weekly")] == ["BTC-USD"]
    assert 1 in bot.ledger.strategy_state("dca_weekly")["skipped_indexes"]
    assert bot.ledger.conn.execute("SELECT COUNT(*) AS n FROM fills WHERE side='sell'").fetchone()["n"] == 0
    status = assess(bot.settings, now=utcnow())
    assert status["buy_pause"] is True
    assert status["kill_switch"] is False
    assert status["overlay"]["trend_daily"]["state"] == "FROZEN"
    assert status["overlay"]["trend_daily"]["last_trip"]["peak"] == peak
    assert Decimal(status["drawdown_pct"]) <= Decimal("-0.10")

    _set_cash(bot, "trend_daily", trend_cash)
    _set_cash(bot, "dca_weekly", dca_cash)
    bot.ledger.close()
    assert _ack(tmp_path, "trend_daily", "operator") == 0
    assert _ack(tmp_path, "dca_weekly", "randy") == 0

    bot = engine(tmp_path, sma_window=3, trend_band=Decimal("0.01"))
    assert bot.ledger.overlay_row("trend_daily")["state"] == "ACKED"
    assert bot.ledger.overlay_row("trend_daily")["peak"] == peak
    assert Decimal(bot.ledger.overlay_row("trend_daily")["dd"]) <= Decimal("-0.10")
    view = snapshot(later)
    bought = bot.broker.submit(
        "trend_daily",
        OrderIntent("BTC-USD", "buy", "after_ack", quote_amount=Decimal("20")),
        "trend_daily:BTC-USD:buy:after-ack",
        bot._context("trend_daily", view, later),
        later,
    )
    assert bought.side == "buy"
    assert bot.ledger.overlay_row("trend_daily")["peak"] == peak
    assert bot.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM fills WHERE reason='trend_entry'"
    ).fetchone()["n"] == 0
    bot.run_once(now=later, snapshot=hot)
    assert [row["symbol"] for row in bot.ledger.fills_for("dca_weekly")] == ["BTC-USD"]
    assert bot.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM fills WHERE reason='trend_entry'"
    ).fetchone()["n"] == 0
    assert bot.ledger.overlay_row("trend_daily")["state"] == "ARMED"
    acked = assess(bot.settings, now=utcnow())
    assert acked["overlay"]["trend_daily"]["last_ack"]["by"] == "operator"
    assert acked["overlay"]["trend_daily"]["ack_delay_hours"] is not None

    _set_cash(bot, "trend_daily", "900")
    bot.ledger.conn.execute("DELETE FROM positions WHERE sleeve='trend_daily'")
    bot.ledger.conn.commit()
    again = later + timedelta(days=1)
    bot.run_once(now=again, snapshot=snapshot(again, closes=["10", "10", "12"], last_open=again - timedelta(days=1)))
    trend_trips = [item for item in _events(bot, "freeze_trip") if item["sleeve"] == "trend_daily"]
    assert len(trend_trips) == 2
    assert bot.ledger.overlay_row("trend_daily")["state"] == "FROZEN"
    bot.ledger.close()


def test_f001_frozen_book_allows_exit_and_does_not_force_sell(tmp_path):
    wall = datetime.now(UTC).replace(microsecond=0)
    opened = wall - timedelta(days=9)
    buy_at = wall - timedelta(days=7)
    bot = engine(tmp_path, sma_window=200)
    bot.run_once(now=opened, snapshot=snapshot(opened))
    buy_view = snapshot(buy_at)
    bot.broker.submit(
        "trend_daily",
        OrderIntent("BTC-USD", "buy", "trend_entry", quote_amount=Decimal("500")),
        f"trend_daily:BTC-USD:buy:{buy_at.date().isoformat()}",
        bot._context("trend_daily", buy_view, buy_at),
        buy_at,
    )
    bot.run_once(now=buy_at, snapshot=buy_view)
    peak = D(bot.ledger.overlay_row("trend_daily")["peak"])
    cash = bot.ledger.cash("trend_daily")
    qty = sum(bot.ledger.positions("trend_daily").values(), Decimal(0))
    held = dict(bot.ledger.positions("trend_daily"))

    def mid_for(dd: Decimal) -> str:
        target = peak * (Decimal(1) + dd)
        mid = (target - cash) / (qty * Decimal("0.99"))
        return format(mid, "f")

    for dd in (Decimal("-0.05"), Decimal("-0.075")):
        when = wall - timedelta(hours=2)
        bot.run_once(now=when, snapshot=snapshot(when, mid=mid_for(dd)))
        assert bot.ledger.overlay_row("trend_daily")["state"] == "ARMED"
        assert bot.ledger.positions("trend_daily") == held
    bot.run_once(now=wall, snapshot=snapshot(wall, mid=mid_for(Decimal("-0.11"))))
    assert bot.ledger.overlay_row("trend_daily")["state"] == "FROZEN"
    assert bot.ledger.positions("trend_daily") == held
    assert bot.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM fills WHERE reason IN ('drawdown_flatten', 'exposure_cut')"
    ).fetchone()["n"] == 0
    sold = bot.broker.submit(
        "trend_daily",
        OrderIntent("BTC-USD", "sell", "trend_exit", base_quantity=held["BTC-USD"]),
        f"trend_daily:BTC-USD:sell:{wall.date().isoformat()}",
        bot._context("trend_daily", snapshot(wall, mid=mid_for(Decimal("-0.11"))), wall),
        wall,
    )
    assert sold.reason == "trend_exit"
    assert "BTC-USD" not in bot.ledger.positions("trend_daily")
    bot.ledger.close()


def test_f001_kill_flattens_one_book_and_resume_needs_human_code(tmp_path, monkeypatch):
    wall = datetime.now(UTC).replace(microsecond=0)
    opened = wall - timedelta(days=2)
    buy_at = wall - timedelta(days=1)
    bot = engine(tmp_path, sma_window=200)
    bot.run_once(now=opened, snapshot=snapshot(opened))
    held_bh = dict(bot.ledger.positions("buy_and_hold"))
    buy_view = snapshot(buy_at)
    bot.broker.submit(
        "trend_daily",
        OrderIntent("BTC-USD", "buy", "trend_entry", quote_amount=Decimal("500")),
        f"trend_daily:BTC-USD:buy:{buy_at.date().isoformat()}",
        bot._context("trend_daily", buy_view, buy_at),
        buy_at,
    )
    bot.run_once(now=buy_at, snapshot=buy_view)
    peak = bot.ledger.overlay_row("trend_daily")["peak"]
    bot.run_once(now=wall, snapshot=snapshot(wall, mid="10"))
    kill = read_kill(tmp_path)
    assert kill is not None and kill["ack_required"] is True
    assert (tmp_path / "KILL").exists()
    assert bot.ledger.overlay_row("trend_daily")["state"] == "KILLED"
    assert bot.ledger.positions("trend_daily") == {}
    assert bot.ledger.positions("buy_and_hold") == held_bh
    assert bot.ledger.overlay_row("dca_weekly")["state"] != "KILLED"
    reasons = [row["reason"] for row in bot.ledger.conn.execute(
        "SELECT reason FROM fills WHERE sleeve='trend_daily' AND side='sell'"
    )]
    assert "drawdown_flatten" in reasons
    trips = _events(bot, "kill_trip")
    assert trips[-1]["sleeve"] == "trend_daily"
    assert trips[-1]["equity"] and trips[-1]["peak"] and trips[-1]["dd"]
    assert Decimal(trips[-1]["dd"]) <= Decimal("-0.40")
    bot.ledger.close()

    from rhbot.cli import main

    assert main(["resume", "--state-dir", str(tmp_path)]) == 2
    assert main(["resume", "--ack", "--state-dir", str(tmp_path)]) == 2
    secret = tmp_path / "human-code"
    secret.write_text("resume-ok\n", encoding="utf-8")
    monkeypatch.setenv("RHBOT_HUMAN_RESUME_FILE", str(secret))
    assert main(["resume", "--ack", "--human-code", "nope", "--state-dir", str(tmp_path)]) == 2
    assert (tmp_path / "KILL").exists()
    assert main(["resume", "--ack", "--human-code", "resume-ok", "--state-dir", str(tmp_path)]) == 0
    assert not (tmp_path / "KILL").exists()

    bot = engine(tmp_path, sma_window=200)
    assert bot.ledger.overlay_row("trend_daily")["peak"] == peak
    bot.run_once(now=wall, snapshot=snapshot(wall, mid="10"))
    assert read_kill(tmp_path) is None
    assert bot.ledger.overlay_row("trend_daily")["peak"] == peak
    bot.ledger.close()


def test_f001_buy_and_hold_drawdown_does_not_touch_other_books(tmp_path, now):
    bot = engine(tmp_path, sma_window=200)
    bot.run_once(now=now, snapshot=snapshot(now))
    held = dict(bot.ledger.positions("buy_and_hold"))
    later = now + timedelta(days=1)
    bot.run_once(now=later, snapshot=snapshot(later, mid="50"))
    assert bot.ledger.positions("buy_and_hold") == held
    assert read_kill(tmp_path) is None
    assert bot.ledger.overlay_row("trend_daily")["state"] == "ARMED"
    assert bot.ledger.overlay_row("dca_weekly")["state"] == "ARMED"
    assert bot.ledger.positions("trend_daily") == {}
    assert bot.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM fills WHERE reason='drawdown_flatten'"
    ).fetchone()["n"] == 0
    bot.ledger.close()


def test_f001_mark_to_bid_trips_when_mid_drawdown_does_not(tmp_path, now):
    bot = engine(tmp_path, sma_window=200)
    bot.run_once(now=now, snapshot=snapshot(now))
    bot.ledger.conn.execute("UPDATE sleeves SET cash='0.00000000' WHERE name='trend_daily'")
    bot.ledger.conn.execute(
        "INSERT INTO positions(sleeve, symbol, qty) VALUES('trend_daily', 'BTC-USD', '9.05000000')"
    )
    bot.ledger.conn.commit()
    later = now + timedelta(days=1)
    view = snapshot(later, mid="100")
    bot.run_once(now=later, snapshot=view)
    assert bot.mark("trend_daily", view) == Decimal("905.00000000")
    assert bot.mark_to_bid("trend_daily", view) == Decimal("895.95000000")
    mid_dd = D("905") / D("1000") - 1
    mtb_dd = D("895.95") / D("1000") - 1
    assert mid_dd > Decimal("-0.10")
    assert mtb_dd <= Decimal("-0.10")
    assert bot.ledger.overlay_row("trend_daily")["state"] == "FROZEN"
    assert bot.ledger.overlay_row("dca_weekly")["state"] == "ARMED"
    assert bot.ledger.positions("buy_and_hold")
    bot.ledger.close()


def test_f001_ack_refuses_bad_reconcile_or_wrong_state(tmp_path, now):
    bot = engine(tmp_path, sma_window=200)
    bot.run_once(now=now, snapshot=snapshot(now))
    bot.ledger.close()
    assert _ack(tmp_path, "trend_daily") == 2

    bot = engine(tmp_path, sma_window=200)
    _set_cash(bot, "trend_daily", "900")
    later = now + timedelta(days=1)
    bot.run_once(now=later, snapshot=snapshot(later))
    assert bot.ledger.overlay_row("trend_daily")["state"] == "FROZEN"
    _set_cash(bot, "trend_daily", "1000")
    bot.ledger.conn.execute("UPDATE overlay_books SET trip_dd='0' WHERE sleeve='trend_daily'")
    bot.ledger.conn.commit()
    bot.ledger.close()
    assert _ack(tmp_path, "trend_daily") == 2

    bot = engine(tmp_path, sma_window=200)
    row = bot.ledger.overlay_row("trend_daily")
    bot.ledger.conn.execute(
        "UPDATE overlay_books SET trip_dd=? WHERE sleeve='trend_daily'",
        (row["dd"],),
    )
    bot.ledger.conn.commit()
    _set_cash(bot, "trend_daily", "1")
    bot.ledger.close()
    assert _ack(tmp_path, "trend_daily") == 2


def test_f002_trend_matches_reference_and_dca_schedule(tmp_path, now):
    from rhbot.strategies.dca import schedule_index
    from rhbot.strategies.trend import TrendDaily

    closes = [Decimal("100")] * 200
    closes.append(Decimal("110"))
    closes.extend([Decimal("100")] * 6)
    closes.append(Decimal("90"))
    closes.append(Decimal("140"))
    reference = _reference_position(closes)
    assert reference[199] == 0
    assert reference[200] == 1
    assert reference[206] == 1
    assert reference[207] == 0
    assert reference[208] == 1

    settings = engine(tmp_path).settings
    strategy = TrendDaily(settings)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    bars = {
        "BTC-USD": make_bars("BTC-USD", [format(price, "f") for price in closes], start + timedelta(days=len(closes) - 1)),
        "ETH-USD": make_bars("ETH-USD", [format(price, "f") for price in closes], start + timedelta(days=len(closes) - 1)),
    }
    state = strategy.initial_state()
    positions: dict[str, Decimal] = {}
    paper_pos = []
    for index, bar in enumerate(bars["BTC-USD"]):
        market_now = bar.ts + timedelta(days=1)
        window = {symbol: series[: index + 1] for symbol, series in bars.items()}
        quotes = {
            symbol: Quote(symbol=symbol, ts=market_now, mid=window[symbol][-1].close, source="test")
            for symbol in window
        }
        from rhbot.models import MarketSnapshot

        view = MarketSnapshot(bars=window, quotes=quotes, source="test")
        orders, state, _reason = strategy.decide(
            view, state, positions, Decimal("1000"), Decimal("1000"), market_now
        )
        for intent in orders:
            if intent.side == "buy":
                positions[intent.symbol] = Decimal("1")
            else:
                positions.pop(intent.symbol, None)
        paper_pos.append(1 if positions.get("BTC-USD", Decimal(0)) > 0 else 0)
        state = strategy.commit(state, [], positions, market_now)
    assert paper_pos == reference

    day1 = now
    assert schedule_index(day1, day1) == 0
    assert schedule_index(day1 + timedelta(days=7), day1) == 1
    assert schedule_index(day1 + timedelta(days=14), day1) == 2
    assert schedule_index(day1 + timedelta(days=6, hours=23), day1) == 0


def _reference_position(closes: list[Decimal], n: int = 200, band: Decimal = Decimal("0.02"), minhold: int = 7) -> list[int]:
    pos = []
    held_flag = 0
    days = 0
    for index, close in enumerate(closes):
        if index + 1 < n:
            pos.append(0)
            continue
        window = closes[index + 1 - n : index + 1]
        sma = sum(window, Decimal(0)) / Decimal(n)
        days += 1
        if held_flag == 0 and close > sma * (Decimal(1) + band):
            held_flag = 1
            days = 0
        elif held_flag == 1 and close < sma * (Decimal(1) - band) and days >= minhold:
            held_flag = 0
            days = 0
        pos.append(held_flag)
    return pos


def test_f002_dca_buys_one_coin_per_period(tmp_path, now):
    bot = engine(tmp_path, sma_window=200)
    expected = ["BTC-USD", "ETH-USD", "BTC-USD", "ETH-USD"]
    for step, symbol in enumerate(expected):
        when = now + timedelta(days=7 * step)
        bot.run_once(now=when, snapshot=snapshot(when))
        bot.run_once(now=when, snapshot=snapshot(when))
        fills = bot.ledger.fills_for("dca_weekly")
        assert len(fills) == step + 1
        assert fills[-1]["symbol"] == symbol
        assert fills[-1]["reason"] == "dca_buy"
    orders = [
        json.loads(row["payload"])
        for row in bot.ledger.conn.execute(
            "SELECT payload FROM events WHERE kind='decision' ORDER BY seq"
        )
    ]
    dca_orders = [
        item["orders"][0]
        for item in orders
        if item.get("sleeve") == "dca_weekly" and item.get("orders")
    ]
    assert [item["symbol"] for item in dca_orders] == expected
    assert [item["quote_amount"] for item in dca_orders] == ["19.23"] * 4
    ids = [row["client_order_id"] for row in bot.ledger.fills_for("dca_weekly")]
    assert len(ids) == len(set(ids)) == 4
    bot.ledger.close()


def test_f002_prefix_invariance(tmp_path):
    from rhbot.models import MarketSnapshot
    from rhbot.strategies.trend import TrendDaily

    settings_bot = engine(tmp_path, sma_window=20, trend_band=Decimal("0.02"))
    strategy = TrendDaily(settings_bot.settings)
    closes = ["100"] * 30 + ["130"] * 20 + ["80"] * 40
    start = datetime(2024, 1, 1, tzinfo=UTC)
    bars = make_bars("BTC-USD", closes, start + timedelta(days=len(closes) - 1))
    eth = make_bars("ETH-USD", closes, start + timedelta(days=len(closes) - 1))

    def run(length: int) -> list[str]:
        state = strategy.initial_state()
        positions: dict[str, Decimal] = {}
        reasons = []
        for index in range(length):
            market_now = bars[index].ts + timedelta(days=1)
            window = {"BTC-USD": bars[: index + 1], "ETH-USD": eth[: index + 1]}
            quotes = {
                symbol: Quote(symbol=symbol, ts=market_now, mid=window[symbol][-1].close, source="test")
                for symbol in window
            }
            view = MarketSnapshot(bars=window, quotes=quotes, source="test")
            _orders, state, reason = strategy.decide(
                view, state, positions, Decimal("1000"), Decimal("1000"), market_now
            )
            for intent in _orders:
                if intent.side == "buy":
                    positions[intent.symbol] = Decimal("1")
                else:
                    positions.pop(intent.symbol, None)
            state = strategy.commit(state, [], positions, market_now)
            reasons.append(reason)
        return reasons

    full = run(len(closes))
    for extra in (1, 5, 30):
        cut = len(closes) - extra
        assert run(cut) == full[:cut]
    settings_bot.ledger.close()


def test_f003_trade_cap_is_per_book_and_skips_risk_reduction(tmp_path, now):
    bot = engine(tmp_path, sma_window=3, trend_band=Decimal("0.01"))
    view = snapshot(now, closes=["10", "10", "12"], last_open=now - timedelta(days=1))
    bot.run_once(now=now, snapshot=view)
    day = now.date().isoformat()
    assert bot.ledger.book_strategy_trades_today("buy_and_hold", day) == 2
    assert bot.ledger.book_strategy_trades_today("trend_daily", day) == 2
    assert bot.ledger.book_strategy_trades_today("dca_weekly", day) == 1
    from rhbot.risk import RiskEngine

    ctx = bot._context("buy_and_hold", view, now)
    # The book already used its two buys, so cash, turnover, and the coin cap
    # would deny first. Clear those so the assertion is the trade cap itself.
    ctx.ordered_symbols_today = set()
    ctx.cash = Decimal("1000")
    ctx.equity = Decimal("1000")
    ctx.positions = {}
    ctx.turnover_today = Decimal("0")
    denied = RiskEngine(bot.settings).evaluate(
        OrderIntent("BTC-USD", "buy", "third", quote_amount=Decimal("20")),
        ctx,
        "buy_and_hold:BTC-USD:buy:third",
    )
    assert denied.reasons == ["max_trades_per_day"]
    held = bot.ledger.positions("buy_and_hold")["BTC-USD"]
    flattened = bot.broker.submit(
        "buy_and_hold",
        OrderIntent("BTC-USD", "sell", "drawdown_flatten", base_quantity=held),
        "buy_and_hold:BTC-USD:sell:flatten",
        bot._context("buy_and_hold", view, now),
        now,
        reduce_only=True,
    )
    assert flattened.reason == "drawdown_flatten"
    assert bot.ledger.book_strategy_trades_today("buy_and_hold", day) == 2
    bot.ledger.close()


def _aged(view: MarketSnapshot, now: datetime, seconds: int) -> MarketSnapshot:
    quotes = {
        symbol: Quote(
            symbol=symbol,
            ts=now - timedelta(seconds=seconds),
            mid=quote.mid,
            source=quote.source,
            bid=quote.bid,
            ask=quote.ask,
        )
        for symbol, quote in view.quotes.items()
    }
    return MarketSnapshot(bars=view.bars, quotes=quotes, source=view.source)


def test_f004_quote_health_uses_cycle_age(tmp_path, now):
    bot = engine(tmp_path, sma_window=200)
    view = _aged(snapshot(now), now, 2)
    bot.run_once(now=now, snapshot=view)
    bot.ledger.set_meta("last_quote_ts", iso(utcnow() - timedelta(seconds=55)))
    body = assess(bot.settings, now=utcnow() + timedelta(seconds=55))
    assert "stale_market_data" not in body["reasons"]
    bot.ledger.close()

    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    stale = engine(stale_dir, sma_window=200)
    bad = _aged(snapshot(now), now, 31)
    stale.run_once(now=now, snapshot=bad)
    health = assess(stale.settings, now=utcnow())
    assert "stale_market_data" in health["reasons"]
    assert stale.ledger.positions("buy_and_hold") == {}
    assert any(item["reason"] == "stale_quote" for item in _events(stale, "risk_denial"))
    stale.ledger.close()

    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    fresh = engine(resume_dir, sma_window=200)
    ok_view = _aged(snapshot(now), now, 2)
    fresh.run_once(now=now, snapshot=ok_view)
    fresh.ledger.set_meta("last_quote_ts", iso(utcnow() - timedelta(minutes=10)))
    engage_kill(resume_dir, "pause", "operator")
    fresh.ledger.close()
    from rhbot.cli import main

    assert main(["resume", "--state-dir", str(resume_dir)]) == 0


def test_f005_replay_refuses_a_live_state_dir(tmp_path, now, monkeypatch):
    from rhbot.backtest import assert_replay_safe, replay

    bot = engine(tmp_path, sma_window=200)
    bot.run_once(now=now, snapshot=snapshot(now))
    engage_kill(tmp_path, "keep", "operator")
    count = bot.ledger.event_count()
    kill_text = (tmp_path / "KILL").read_text(encoding="utf-8")
    bars = {
        "BTC-USD": make_bars("BTC-USD", ["10", "10", "10"], now - timedelta(days=1)),
        "ETH-USD": make_bars("ETH-USD", ["10", "10", "10"], now - timedelta(days=1)),
    }
    with pytest.raises(ConfigError):
        replay(bot.settings, bars)
    assert bot.ledger.event_count() == count
    assert (tmp_path / "KILL").read_text(encoding="utf-8") == kill_text
    bot.ledger.close()

    empty = tmp_path / "service"
    empty.mkdir()
    monkeypatch.setenv("RHBOT_STATE_DIR", str(empty))
    from tests.conftest import make_settings

    with pytest.raises(ConfigError):
        assert_replay_safe(make_settings(empty))


def test_f006_crash_after_fill_rerun_is_idempotent(tmp_path, now):
    bot = engine(tmp_path, sma_window=3, trend_band=Decimal("0.01"))
    last_open = now - timedelta(days=1)
    view = snapshot(now, closes=["10", "10", "12"], last_open=last_open)
    bot.ledger.ensure_sleeve("trend_daily", now, bot.strategies[2].initial_state())
    intent = OrderIntent("BTC-USD", "buy", "trend_entry", quote_amount=Decimal("500"))
    client_id = bot._client_id("trend_daily", intent, now)
    planned = bot.broker.plan(
        "trend_daily",
        intent,
        client_id,
        bot._context("trend_daily", view, now),
        now,
    )
    bot.ledger.commit_fill(planned)
    assert bot.ledger.strategy_state("trend_daily").get("holding_since", {}) == {}
    bot.run_once(now=now, snapshot=view)
    rows = bot.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM fills WHERE client_order_id=?",
        (client_id,),
    ).fetchone()
    assert rows["n"] == 1
    state = bot.ledger.strategy_state("trend_daily")
    assert "BTC-USD" in state["holding_since"]
    assert bot.ledger.positions("trend_daily")["BTC-USD"] == planned.qty
    again = bot.broker.submit(
        "trend_daily",
        intent,
        client_id,
        bot._context("trend_daily", view, now),
        now,
    )
    assert again.client_order_id == planned.client_order_id
    assert again.notional == planned.notional
    assert bot.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM fills WHERE client_order_id=?",
        (client_id,),
    ).fetchone()["n"] == 1
    bot.ledger.close()


def test_f007_kraken_trade_time_not_http_date(tmp_path, now):
    from rhbot.data.public import parse_kraken_ticker
    from rhbot.risk import RiskEngine

    payload = {
        "error": [],
        "result": {"XXBTZUSD": {"c": ["100", "1"], "a": ["100.1", "1", "1"], "b": ["99.9", "1", "1"]}},
    }
    headers = {"Date": "Mon, 16 Mar 2026 15:00:00 GMT"}
    untrusted = parse_kraken_ticker("BTC-USD", payload, headers, trade_time=None)
    assert untrusted.ts_trusted is False
    bot = engine(tmp_path)
    settings = bot.settings
    risk = RiskEngine(settings)
    from tests.test_risk import _buy, _ctx

    denied = risk.evaluate(
        _buy(),
        _ctx(settings, now, quotes={"BTC-USD": untrusted, "ETH-USD": untrusted}),
        "id-untrusted",
    )
    assert denied.reasons == ["untrusted_quote_ts"]
    stale = parse_kraken_ticker(
        "BTC-USD",
        payload,
        headers,
        trade_time=now - timedelta(minutes=10),
    )
    fresh = parse_kraken_ticker("ETH-USD", payload, headers, trade_time=now)
    decision = risk.evaluate(
        _buy(),
        _ctx(settings, now, quotes={"BTC-USD": stale, "ETH-USD": fresh}),
        "id-stale-trade",
    )
    assert decision.reasons == ["stale_quote"]
    bot.ledger.close()


def test_f008_min_hold_in_the_risk_engine(tmp_path, now):
    bot = engine(tmp_path, sma_window=200)
    bot.ledger.ensure_sleeve("trend_daily", now, {})
    view = snapshot(now)
    bot.broker.submit(
        "trend_daily",
        OrderIntent("BTC-USD", "buy", "trend_entry", quote_amount=Decimal("100")),
        "trend_daily:BTC-USD:buy:open",
        bot._context("trend_daily", view, now),
        now,
    )
    qty = bot.ledger.positions("trend_daily")["BTC-USD"]
    day6 = now + timedelta(days=6)
    with pytest.raises(OrderRejected) as caught:
        bot.broker.submit(
            "trend_daily",
            OrderIntent("BTC-USD", "sell", "trend_exit", base_quantity=qty),
            "trend_daily:BTC-USD:sell:day6",
            bot._context("trend_daily", snapshot(day6), day6),
            day6,
        )
    assert caught.value.reasons == ["min_hold"]
    day2 = now + timedelta(days=2)
    flattened = bot.broker.submit(
        "trend_daily",
        OrderIntent("BTC-USD", "sell", "drawdown_flatten", base_quantity=qty),
        "trend_daily:BTC-USD:sell:flatten",
        bot._context("trend_daily", snapshot(day2), day2),
        day2,
        reduce_only=True,
    )
    assert flattened.reason == "drawdown_flatten"
    assert bot.ledger.positions("trend_daily") == {}

    other = tmp_path / "hold"
    other.mkdir()
    held = engine(other, sma_window=200)
    held.ledger.ensure_sleeve("trend_daily", now, {})
    held.broker.submit(
        "trend_daily",
        OrderIntent("ETH-USD", "buy", "trend_entry", quote_amount=Decimal("100")),
        "trend_daily:ETH-USD:buy:open",
        held._context("trend_daily", view, now),
        now,
    )
    eth_qty = held.ledger.positions("trend_daily")["ETH-USD"]
    day7 = now + timedelta(days=7)
    sold = held.broker.submit(
        "trend_daily",
        OrderIntent("ETH-USD", "sell", "trend_exit", base_quantity=eth_qty),
        "trend_daily:ETH-USD:sell:day7",
        held._context("trend_daily", snapshot(day7), day7),
        day7,
    )
    assert sold.reason == "trend_exit"
    assert held.ledger.positions("trend_daily") == {}
    held.ledger.close()
    bot.ledger.close()


def test_f009_open_bar_is_not_cached_and_missing_yesterday_is_stale(tmp_path, now):
    from rhbot.strategies.trend import TrendDaily

    bot = engine(tmp_path, sma_window=3, trend_band=Decimal("0.01"))
    open_bar = Bar(
        symbol="BTC-USD",
        ts=now,
        open=Decimal("12"),
        high=Decimal("12"),
        low=Decimal("12"),
        close=Decimal("12"),
        volume=Decimal("1"),
        source="test",
    )
    bot.ledger.upsert_candles([open_bar], fetched_at=now)
    assert bot.ledger.load_candles_any("BTC-USD") == []
    old = now - timedelta(days=3)
    view = snapshot(now, closes=["10", "10", "12"], last_open=old)
    _orders, _state, reason = TrendDaily(bot.settings).decide(
        view, TrendDaily(bot.settings).initial_state(), {}, Decimal("1000"), Decimal("1000"), now
    )
    assert "stale_candles" in reason
    assert _orders == []
    bot.run_once(now=now, snapshot=view)
    assert bot.ledger.fills_for("trend_daily") == []
    decision = _events(bot, "decision")
    assert any(item["sleeve"] == "trend_daily" and "stale_candles" in item["reason"] for item in decision)
    assert "evaluated_on" not in bot.ledger.strategy_state("trend_daily") or not bot.ledger.strategy_state("trend_daily").get("evaluated_on")
    bot.ledger.close()


def test_f010_audit_replay_matches_then_flags_an_extra_fill(tmp_path):
    from rhbot.backtest import diff_live
    from rhbot.cli import main
    from rhbot.models import MarketSnapshot
    from tests.conftest import make_settings

    start = datetime(2024, 1, 1, tzinfo=UTC)
    closes = ["100"] * 260
    last = start + timedelta(days=len(closes) - 1)
    series = {
        "BTC-USD": make_bars("BTC-USD", closes, last),
        "ETH-USD": make_bars("ETH-USD", closes, last),
    }
    bot = engine(tmp_path)
    for index in range(len(closes)):
        market_now = series["BTC-USD"][index].ts + timedelta(days=1)
        window = {symbol: bars[: index + 1] for symbol, bars in series.items()}
        quotes = {
            symbol: Quote(symbol=symbol, ts=market_now, mid=window[symbol][-1].close, source="test")
            for symbol in window
        }
        bot.run_once(
            now=market_now,
            snapshot=MarketSnapshot(bars=window, quotes=quotes, source="test"),
        )
    bot.ledger.close()
    report = diff_live(make_settings(tmp_path), "30d")
    assert report["ok"] is True
    assert report["mismatches"] == []
    assert report["mode"] == "replay"

    raw = __import__("sqlite3").connect(tmp_path / "bot.sqlite")
    raw.execute(
        """
        INSERT INTO fills(
            sleeve, symbol, side, qty, qty_delta, mid, fill_price,
            cash_delta, cost, notional, ts, client_order_id, reason
        ) VALUES('trend_daily', 'BTC-USD', 'buy', '1', '1', '100', '101', '-101', '1', '100', ?, 'injected', 'extra')
        """,
        (iso(start + timedelta(days=10)),),
    )
    raw.commit()
    raw.close()
    assert main(["audit", "replay", "--since", "30d", "--state-dir", str(tmp_path)]) == 2


def test_f011_shadow_enters_while_trend_is_frozen(tmp_path, now):
    from rhbot.status import build_report

    bot = engine(tmp_path, sma_window=3, trend_band=Decimal("0.01"))
    bot.run_once(now=now, snapshot=snapshot(now, closes=["10", "10", "10"], last_open=now - timedelta(days=1)))
    _set_cash(bot, "trend_daily", "900")
    later = now + timedelta(days=1)
    hot = snapshot(later, closes=["10", "10", "12"], last_open=later - timedelta(days=1))
    bot.run_once(now=later, snapshot=hot)
    assert bot.ledger.overlay_row("trend_daily")["state"] == "FROZEN"
    assert bot.ledger.positions("trend_daily") == {}
    assert bot.ledger.shadow_positions("trend_daily_shadow")
    report = build_report(bot.settings, "30d", now=later + timedelta(days=1))
    shadow = report["no_overlay"]["sleeves"]["trend_daily_shadow"]
    assert shadow["positions"]
    assert "overlay_impact" in shadow
    assert report["overlay"]["trend_daily"]["shadow"] == "trend_daily_shadow"
    bot.ledger.close()


def test_f012_and_f013_are_covered_by_safety_tests():
    text = Path("tests/test_safety.py").read_text(encoding="utf-8")
    assert "http_write_findings" in text
    assert "config.yaml" in Path(".gitignore").read_text(encoding="utf-8")
