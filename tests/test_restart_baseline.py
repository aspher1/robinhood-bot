"""I-R006: a human restart records a baseline and does not move the all-time peak."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from rhbot.backtest import diff_live
from rhbot.models import Bar, MarketSnapshot, OrderIntent, Quote
from rhbot.money import D, money_str
from rhbot.ops import iso, read_kill, utcnow
from rhbot.status import assess, build_report

from tests.conftest import engine, make_bars, make_settings, snapshot

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


def _resume(tmp_path, monkeypatch) -> int:
    from rhbot.cli import main

    secret = tmp_path / "human-code"
    secret.write_text("resume-ok\n", encoding="utf-8")
    monkeypatch.setenv("RHBOT_HUMAN_RESUME_FILE", str(secret))
    return main(["resume", "--ack", "--human-code", "resume-ok", "--state-dir", str(tmp_path)])


def _kill_trend(tmp_path):
    """Open a trend position, crash it through −40%, and leave the book flat and killed."""
    wall = datetime(2026, 4, 2, 15, 0, tzinfo=UTC)
    opened = wall - timedelta(days=2)
    buy_at = wall - timedelta(days=1)
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
    peak = bot.ledger.overlay_row("trend_daily")["peak"]
    bot.run_once(now=wall, snapshot=snapshot(wall, mid="10"))
    assert bot.ledger.overlay_row("trend_daily")["state"] == "KILLED"
    assert bot.ledger.positions("trend_daily") == {}
    assert read_kill(tmp_path) is None
    bot.ledger.close()
    return wall, peak


def test_without_a_human_restart_the_book_stays_off(tmp_path):
    wall, peak = _kill_trend(tmp_path)
    bot = engine(tmp_path, sma_window=200)
    later = wall + timedelta(days=1)
    _set_cash(bot, "trend_daily", peak)
    bot.run_once(now=later, snapshot=snapshot(later, mid="100"))
    assert bot.ledger.overlay_row("trend_daily")["state"] == "KILLED"
    assert bot.ledger.overlay_row("trend_daily")["peak"] == peak
    assert not str(bot.ledger.overlay_row("trend_daily")["restart_baseline"] or "")
    view = snapshot(later)
    from rhbot.errors import OrderRejected

    try:
        bot.broker.submit(
            "trend_daily",
            OrderIntent("ETH-USD", "buy", "after_kill", quote_amount=Decimal("20")),
            "trend_daily:ETH-USD:buy:still-killed",
            bot._context("trend_daily", view, later),
            later,
        )
        raised = False
    except OrderRejected as exc:
        raised = True
        assert exc.reasons == ["killed"]
    assert raised
    bot.ledger.close()


def test_restart_then_ten_percent_pauses_and_forty_shuts_off(tmp_path, monkeypatch):
    wall, peak = _kill_trend(tmp_path)
    assert _resume(tmp_path, monkeypatch) == 0
    bot = engine(tmp_path, sma_window=200)
    row = bot.ledger.overlay_row("trend_daily")
    baseline = D(row["restart_baseline"])
    assert baseline > 0
    assert baseline < D(peak)
    assert row["peak"] == peak
    assert row["state"] == "KILLED"
    events = _events(bot, "restart_baseline")
    assert events[-1]["sleeve"] == "trend_daily"
    assert events[-1]["peak"] == peak
    assert events[-1]["ts"]
    assert D(events[-1]["restart_baseline"]) == baseline
    bot.ledger.close()

    # A new process still sees the baseline.
    bot = engine(tmp_path, sma_window=200)
    assert D(bot.ledger.overlay_row("trend_daily")["restart_baseline"]) == baseline
    armed_at = wall + timedelta(days=1)
    bot.run_once(now=armed_at, snapshot=snapshot(armed_at, mid="100"))
    armed = bot.ledger.overlay_row("trend_daily")
    assert armed["state"] == "ARMED"
    assert armed["peak"] == peak
    assert D(armed["restart_baseline"]) == baseline
    assert D(armed["dd"]) <= Decimal("-0.40")
    status = assess(bot.settings, now=utcnow())
    assert status["overlay"]["trend_daily"]["state"] == "ARMED"
    assert status["overlay"]["trend_daily"]["peak"] == peak
    assert D(status["overlay"]["trend_daily"]["restart_baseline"]) == baseline
    assert D(status["drawdown_pct"]) <= Decimal("-0.40")
    assert D(status["drawdown_pct"]) == D(status["overlay"]["trend_daily"]["dd"])
    report = build_report(bot.settings, "30d", now=armed_at + timedelta(days=1))
    assert report["overlay"]["trend_daily"]["peak"] == peak
    assert D(report["overlay"]["trend_daily"]["dd"]) <= Decimal("-0.40")
    assert D(report["sleeves"]["trend_daily"]["window"]["max_drawdown_pct"]) >= Decimal("40")
    bought = bot.broker.submit(
        "trend_daily",
        OrderIntent("ETH-USD", "buy", "after_restart", quote_amount=Decimal("20")),
        "trend_daily:ETH-USD:buy:after-restart",
        bot._context("trend_daily", snapshot(armed_at), armed_at),
        armed_at,
    )
    assert bought.side == "buy"
    assert bot.ledger.overlay_row("trend_daily")["peak"] == peak
    assert D(bot.ledger.overlay_row("trend_daily")["restart_baseline"]) == baseline

    # Back to the flat restart equity, then 11% and 41% off that baseline.
    _set_cash(bot, "trend_daily", money_str(baseline))
    bot.ledger.conn.execute("DELETE FROM positions WHERE sleeve='trend_daily'")
    bot.ledger.conn.commit()
    paused_at = armed_at + timedelta(days=1)
    _set_cash(bot, "trend_daily", money_str(baseline * Decimal("0.89")))
    bot.run_once(now=paused_at, snapshot=snapshot(paused_at, mid="100"))
    paused = bot.ledger.overlay_row("trend_daily")
    assert paused["state"] == "FROZEN"
    assert paused["peak"] == peak
    assert D(paused["restart_baseline"]) == baseline
    assert D(paused["dd"]) <= Decimal("-0.40")
    killed_at = paused_at + timedelta(days=1)
    _set_cash(bot, "trend_daily", money_str(baseline * Decimal("0.59")))
    bot.run_once(now=killed_at, snapshot=snapshot(killed_at, mid="100"))
    killed = bot.ledger.overlay_row("trend_daily")
    assert killed["state"] == "KILLED"
    assert killed["peak"] == peak
    assert D(killed["restart_baseline"]) == baseline
    assert not str(killed["kill_acked_peak"] or "")
    bot.ledger.close()


def test_ack_does_not_move_the_peak_or_the_baseline(tmp_path, monkeypatch):
    wall, peak = _kill_trend(tmp_path)
    assert _resume(tmp_path, monkeypatch) == 0
    bot = engine(tmp_path, sma_window=200)
    baseline = D(bot.ledger.overlay_row("trend_daily")["restart_baseline"])
    armed_at = wall + timedelta(days=1)
    bot.run_once(now=armed_at, snapshot=snapshot(armed_at, mid="100"))
    assert bot.ledger.overlay_row("trend_daily")["state"] == "ARMED"
    flat_cash = bot.ledger.cash("trend_daily")
    paused_at = armed_at + timedelta(days=1)
    _set_cash(bot, "trend_daily", money_str(baseline * Decimal("0.89")))
    bot.run_once(now=paused_at, snapshot=snapshot(paused_at, mid="100"))
    assert bot.ledger.overlay_row("trend_daily")["state"] == "FROZEN"
    _set_cash(bot, "trend_daily", money_str(flat_cash))
    bot.ledger.close()

    from rhbot.cli import main

    assert (
        main(
            [
                "ack-drawdown",
                "--strategy",
                "trend_daily",
                "--by",
                "operator",
                "--note",
                "reviewed the restart pause",
                "--state-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    bot = engine(tmp_path, sma_window=200)
    acked = bot.ledger.overlay_row("trend_daily")
    assert acked["state"] == "ACKED"
    assert acked["peak"] == peak
    assert D(acked["restart_baseline"]) == baseline
    bought = bot.broker.submit(
        "trend_daily",
        OrderIntent("ETH-USD", "buy", "acked_pause", quote_amount=Decimal("20")),
        "trend_daily:ETH-USD:buy:acked-pause",
        bot._context("trend_daily", snapshot(paused_at), paused_at),
        paused_at,
    )
    assert bought.side == "buy"
    assert bot.ledger.overlay_row("trend_daily")["peak"] == peak
    assert D(bot.ledger.overlay_row("trend_daily")["restart_baseline"]) == baseline
    bot.ledger.conn.execute("DELETE FROM positions WHERE sleeve='trend_daily'")
    bot.ledger.conn.commit()
    _set_cash(bot, "trend_daily", money_str(baseline))
    recovered = paused_at + timedelta(days=1)
    bot.run_once(now=recovered, snapshot=snapshot(recovered, mid="100"))
    assert bot.ledger.overlay_row("trend_daily")["state"] == "ARMED"
    assert bot.ledger.overlay_row("trend_daily")["peak"] == peak
    assert D(bot.ledger.overlay_row("trend_daily")["restart_baseline"]) == baseline
    bot.ledger.close()


def test_a_new_all_time_high_uses_the_peak_again(tmp_path, monkeypatch):
    wall, peak = _kill_trend(tmp_path)
    assert _resume(tmp_path, monkeypatch) == 0
    bot = engine(tmp_path, sma_window=200)
    baseline = D(bot.ledger.overlay_row("trend_daily")["restart_baseline"])
    armed_at = wall + timedelta(days=1)
    bot.run_once(now=armed_at, snapshot=snapshot(armed_at, mid="100"))
    assert bot.ledger.overlay_row("trend_daily")["state"] == "ARMED"
    higher = D(peak) * Decimal("1.10")
    high_at = armed_at + timedelta(days=1)
    _set_cash(bot, "trend_daily", money_str(higher))
    bot.run_once(now=high_at, snapshot=snapshot(high_at, mid="100"))
    raised = bot.ledger.overlay_row("trend_daily")
    assert raised["state"] == "ARMED"
    assert D(raised["peak"]) == D(money_str(higher))
    assert D(raised["peak"]) > D(peak)
    assert D(raised["restart_baseline"]) == baseline
    assert D(raised["restart_high"]) == D(raised["peak"])
    # 11% under the new peak is still far above the restart baseline, and it
    # is only about 2% under the old peak. It pauses because the new high is
    # the reference again.
    faded = high_at + timedelta(days=1)
    _set_cash(bot, "trend_daily", money_str(higher * Decimal("0.89")))
    bot.run_once(now=faded, snapshot=snapshot(faded, mid="100"))
    paused = bot.ledger.overlay_row("trend_daily")
    assert paused["state"] == "FROZEN"
    assert D(paused["peak"]) == D(money_str(higher))
    assert D(paused["restart_baseline"]) == baseline
    assert D(paused["dd"]) <= Decimal("-0.10")
    bot.ledger.close()


def test_replay_matches_live_after_the_human_restart(tmp_path, monkeypatch):
    start = datetime(2024, 6, 1, tzinfo=UTC)
    # 70 is deep enough to shut the invested trend book off, and leaves enough
    # cash that the next hot close can buy again. 10 would flatten it under $10.
    closes = ["100"] * 199 + ["120", "70", "120", "120"]
    last = start + timedelta(days=len(closes) - 1)
    series = {
        "BTC-USD": make_bars("BTC-USD", closes, last),
        "ETH-USD": make_bars("ETH-USD", closes, last),
    }
    bot = engine(tmp_path)
    resumed = False
    for index in range(len(closes)):
        market_now = series["BTC-USD"][index].ts + timedelta(days=1)
        window = {symbol: bars[: index + 1] for symbol, bars in series.items()}
        quotes = {
            symbol: Quote(
                symbol=symbol,
                ts=market_now,
                mid=window[symbol][-1].close,
                bid=window[symbol][-1].close,
                ask=window[symbol][-1].close,
                source="test",
            )
            for symbol in window
        }
        bot.run_once(
            now=market_now,
            snapshot=MarketSnapshot(bars=window, quotes=quotes, source="test"),
        )
        if not resumed and bot.ledger.overlay_row("trend_daily")["state"] == "KILLED":
            anchor = market_now
            bot.ledger.close()
            assert _resume(tmp_path, monkeypatch) == 0
            bot = engine(tmp_path)
            resumed = True
    assert resumed
    restarts = _events(bot, "restart_baseline")
    assert restarts[-1]["peak"]
    assert restarts[-1]["ts"]
    assert restarts[-1]["anchor_ts"] == iso(anchor)
    later_entries = [
        row
        for row in bot.ledger.fills_for("trend_daily")
        if row["reason"] == "trend_entry" and str(row["ts"]) > iso(anchor)
    ]
    assert later_entries
    assert bot.ledger.overlay_row("trend_daily")["peak"]
    peak = bot.ledger.overlay_row("trend_daily")["peak"]
    baseline = D(bot.ledger.overlay_row("trend_daily")["restart_baseline"])
    assert baseline < D(peak)
    bot.ledger.close()
    report = diff_live(make_settings(tmp_path), "30d")
    assert report["ok"] is True, report["mismatches"][:12]
    assert report["mismatches"] == []
    assert report["mode"] == "replay"
