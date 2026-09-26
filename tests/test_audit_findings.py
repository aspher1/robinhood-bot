"""Audit round 1 acceptance tests, findings F-001 through F-013."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from rhbot.errors import ConfigError, OrderRejected
from rhbot.models import Bar, MarketSnapshot, OrderIntent, Quote
from rhbot.money import D, money_str, q8
from rhbot.ops import engage_kill, iso, read_kill, utcnow
from rhbot.status import assess

from tests.conftest import engine, make_bars, padded_closes, snapshot

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
    bot = engine(tmp_path)
    flat = snapshot(now, closes=padded_closes("10"), last_open=now - timedelta(days=1))
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
    hot = snapshot(later, closes=padded_closes("12"), last_open=later - timedelta(days=1))
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

    bot = engine(tmp_path)
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
    bot.run_once(now=again, snapshot=snapshot(again, closes=padded_closes("12"), last_open=again - timedelta(days=1)))
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
    assert read_kill(tmp_path) is None
    assert not (tmp_path / "KILL").exists()
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
    sleeve_peaks = {
        str(row["name"]): str(row["peak_equity"])
        for row in bot.ledger.conn.execute("SELECT name, peak_equity FROM sleeves")
    }
    portfolio_peak = bot.ledger.get_meta("portfolio_peak")
    assert portfolio_peak
    bot.ledger.close()

    from rhbot.cli import main

    assert main(["resume", "--state-dir", str(tmp_path)]) == 2
    assert main(["resume", "--ack", "--state-dir", str(tmp_path)]) == 2
    secret = tmp_path / "human-code"
    secret.write_text("resume-ok\n", encoding="utf-8")
    monkeypatch.setenv("RHBOT_HUMAN_RESUME_FILE", str(secret))
    assert main(["resume", "--ack", "--human-code", "nope", "--state-dir", str(tmp_path)]) == 2
    assert not (tmp_path / "KILL").exists()
    assert main(["resume", "--ack", "--human-code", "resume-ok", "--state-dir", str(tmp_path)]) == 0
    assert not (tmp_path / "KILL").exists()

    bot = engine(tmp_path, sma_window=200)
    assert bot.ledger.overlay_row("trend_daily")["peak"] == peak
    assert bot.ledger.get_meta("portfolio_peak") == portfolio_peak
    for name, stored in sleeve_peaks.items():
        assert bot.ledger.sleeve_row(name)["peak_equity"] == stored
    bot.run_once(now=wall, snapshot=snapshot(wall, mid="10"))
    assert read_kill(tmp_path) is None
    assert bot.ledger.overlay_row("trend_daily")["peak"] == peak
    assert bot.ledger.overlay_row("trend_daily")["state"] == "ARMED"
    assert bot.ledger.get_meta("portfolio_peak") == portfolio_peak
    for name, stored in sleeve_peaks.items():
        assert bot.ledger.sleeve_row(name)["peak_equity"] == stored
    # The same flattened mark rearms against the restart baseline. A further
    # 11% off that baseline pauses, and a further 41% kills again. The stored
    # drawdown is still the drop from the original peak, which stays put.
    baseline = D(bot.ledger.overlay_row("trend_daily")["restart_baseline"])
    assert baseline > 0
    assert baseline < D(peak)
    assert D(bot.ledger.overlay_row("trend_daily")["dd"]) <= Decimal("-0.40")
    paused = wall + timedelta(days=1)
    _set_cash(bot, "trend_daily", money_str(baseline * Decimal("0.89")))
    bot.run_once(now=paused, snapshot=snapshot(paused, mid="10"))
    assert read_kill(tmp_path) is None
    assert bot.ledger.overlay_row("trend_daily")["state"] == "FROZEN"
    assert bot.ledger.overlay_row("trend_daily")["peak"] == peak
    assert D(bot.ledger.overlay_row("trend_daily")["dd"]) <= Decimal("-0.40")
    again = paused + timedelta(days=1)
    _set_cash(bot, "trend_daily", money_str(baseline * Decimal("0.59")))
    bot.run_once(now=again, snapshot=snapshot(again, mid="10"))
    assert read_kill(tmp_path) is None
    assert bot.ledger.overlay_row("trend_daily")["state"] == "KILLED"
    assert bot.ledger.overlay_row("trend_daily")["peak"] == peak
    assert bot.ledger.get_meta("portfolio_peak") == portfolio_peak
    assert bot.ledger.sleeve_row("trend_daily")["peak_equity"] == sleeve_peaks["trend_daily"]
    bot.ledger.close()


def test_f024_one_book_kill_does_not_block_the_other(tmp_path, monkeypatch):
    wall = datetime.now(UTC).replace(microsecond=0)
    opened = wall - timedelta(days=2)
    buy_at = wall - timedelta(days=1)
    bot = engine(tmp_path, sma_window=200)
    bot.run_once(now=opened, snapshot=snapshot(opened))
    held_bh = dict(bot.ledger.positions("buy_and_hold"))
    bot.broker.submit(
        "trend_daily",
        OrderIntent("BTC-USD", "buy", "trend_entry", quote_amount=Decimal("500")),
        f"trend_daily:BTC-USD:buy:{buy_at.date().isoformat()}",
        bot._context("trend_daily", snapshot(buy_at), buy_at),
        buy_at,
    )
    view = snapshot(wall, mid="10")
    bot.run_once(now=wall, snapshot=view)
    assert bot.ledger.overlay_row("trend_daily")["state"] == "KILLED"
    assert bot.ledger.positions("trend_daily") == {}
    assert bot.ledger.positions("buy_and_hold") == held_bh
    assert bot.ledger.overlay_row("dca_weekly")["state"] != "KILLED"
    assert read_kill(tmp_path) is None
    with pytest.raises(OrderRejected) as caught:
        bot.broker.submit(
            "trend_daily",
            OrderIntent("ETH-USD", "buy", "after_kill", quote_amount=Decimal("20")),
            "trend_daily:ETH-USD:buy:after-kill",
            bot._context("trend_daily", view, wall),
            wall,
        )
    assert caught.value.reasons == ["killed"]
    bought = bot.broker.submit(
        "dca_weekly",
        OrderIntent("ETH-USD", "buy", "dca_buy", quote_amount=Decimal("19.23")),
        "dca_weekly:ETH-USD:buy:while-trend-killed",
        bot._context("dca_weekly", view, wall),
        wall,
    )
    assert bought.reason == "dca_buy"
    assert "ETH-USD" in bot.ledger.positions("dca_weekly")
    bot.ledger.close()

    from rhbot.cli import main

    assert main(["resume", "--state-dir", str(tmp_path)]) == 2
    assert main(["resume", "--ack", "--state-dir", str(tmp_path)]) == 2
    secret = tmp_path / "human-code"
    secret.write_text("resume-ok\n", encoding="utf-8")
    monkeypatch.setenv("RHBOT_HUMAN_RESUME_FILE", str(secret))
    assert main(["resume", "--ack", "--human-code", "nope", "--state-dir", str(tmp_path)]) == 2
    assert main(["resume", "--ack", "--human-code", "resume-ok", "--state-dir", str(tmp_path)]) == 0
    bot = engine(tmp_path, sma_window=200)
    assert bot.ledger.overlay_row("trend_daily")["state"] == "KILLED"
    assert bot.ledger.overlay_row("trend_daily")["kill_acked_peak"]
    assert bot.ledger.overlay_row("dca_weekly")["state"] != "KILLED"
    assert bot.ledger.positions("buy_and_hold") == held_bh
    bot.ledger.close()


def test_f023_retry_flatten_until_book_is_flat(tmp_path, monkeypatch):
    wall = datetime.now(UTC).replace(microsecond=0)
    opened = wall - timedelta(days=2)
    buy_at = wall - timedelta(days=1)
    bot = engine(tmp_path, sma_window=200)
    bot.run_once(now=opened, snapshot=snapshot(opened))
    held_bh = dict(bot.ledger.positions("buy_and_hold"))
    bot.broker.submit(
        "trend_daily",
        OrderIntent("BTC-USD", "buy", "trend_entry", quote_amount=Decimal("500")),
        f"trend_daily:BTC-USD:buy:{buy_at.date().isoformat()}",
        bot._context("trend_daily", snapshot(buy_at), buy_at),
        buy_at,
    )
    overlay_peak = bot.ledger.overlay_row("trend_daily")["peak"]
    portfolio_peak = bot.ledger.get_meta("portfolio_peak")
    sleeve_peaks = {
        str(row["name"]): str(row["peak_equity"])
        for row in bot.ledger.conn.execute("SELECT name, peak_equity FROM sleeves")
    }
    real_submit = bot.broker.submit
    rejected = {"n": 0}

    def flaky(sleeve, intent, client_order_id, context, now, reduce_only=False):
        if intent.reason == "drawdown_flatten":
            rejected["n"] += 1
            if rejected["n"] == 1:
                raise OrderRejected(["stale_quote"])
        return real_submit(
            sleeve,
            intent,
            client_order_id,
            context,
            now,
            reduce_only=reduce_only,
        )

    monkeypatch.setattr(bot.broker, "submit", flaky)
    crash = snapshot(wall, mid="10")
    bot.run_once(now=wall, snapshot=crash)
    assert rejected["n"] == 1
    assert bot.ledger.overlay_row("trend_daily")["state"] == "KILLED"
    assert bot.ledger.positions("trend_daily")
    assert bot.ledger.positions("buy_and_hold") == held_bh
    assert bot.ledger.overlay_row("trend_daily")["peak"] == overlay_peak
    assert bot.ledger.get_meta("portfolio_peak") == portfolio_peak
    for name, stored in sleeve_peaks.items():
        assert bot.ledger.sleeve_row(name)["peak_equity"] == stored
    assert "trend_daily" in (bot.ledger.get_meta("kill_flatten_incomplete") or "")
    status = assess(bot.settings, now=utcnow())
    assert status["health"] == "critical"
    assert "kill_flatten_incomplete" in status["reasons"]
    flagged = status["checks"]["kill_flatten"]["sleeves"]
    assert flagged[0]["sleeve"] == "trend_daily"
    assert D(flagged[0]["positions"]["BTC-USD"]) > 0
    incomplete = _events(bot, "kill_flatten_incomplete")
    assert incomplete[-1]["sleeve"] == "trend_daily"
    assert D(incomplete[-1]["positions"]["BTC-USD"]) == bot.ledger.positions("trend_daily")["BTC-USD"]
    assert bot.ledger.verify_chain()[0]
    bot.ledger.close()

    from rhbot.cli import main

    secret = tmp_path / "human-code"
    secret.write_text("resume-ok\n", encoding="utf-8")
    monkeypatch.setenv("RHBOT_HUMAN_RESUME_FILE", str(secret))
    assert main(["resume", "--ack", "--human-code", "resume-ok", "--state-dir", str(tmp_path)]) == 2

    bot = engine(tmp_path, sma_window=200)
    assert not str(bot.ledger.overlay_row("trend_daily")["kill_acked_peak"] or "")
    bot.run_once(now=wall, snapshot=crash)
    assert bot.ledger.positions("trend_daily") == {}
    assert bot.ledger.positions("buy_and_hold") == held_bh
    assert bot.ledger.overlay_row("trend_daily")["state"] == "KILLED"
    assert bot.ledger.overlay_row("trend_daily")["peak"] == overlay_peak
    assert bot.ledger.get_meta("portfolio_peak") == portfolio_peak
    for name, stored in sleeve_peaks.items():
        assert bot.ledger.sleeve_row(name)["peak_equity"] == stored
    sells = [
        row
        for row in bot.ledger.fills_for("trend_daily")
        if row["side"] == "sell" and row["reason"] == "drawdown_flatten"
    ]
    assert len(sells) == 1
    assert ":drawdown_flatten:" in str(sells[0]["client_order_id"])
    assert bot.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM fills WHERE sleeve='buy_and_hold' AND reason='drawdown_flatten'"
    ).fetchone()["n"] == 0
    assert not (bot.ledger.get_meta("kill_flatten_incomplete") or "")
    cleared = assess(bot.settings, now=utcnow())
    assert "kill_flatten_incomplete" not in cleared["reasons"]
    bot.ledger.close()

    assert main(["resume", "--state-dir", str(tmp_path)]) == 2
    assert main(["resume", "--ack", "--state-dir", str(tmp_path)]) == 2
    assert main(["resume", "--ack", "--human-code", "nope", "--state-dir", str(tmp_path)]) == 2
    assert main(["resume", "--ack", "--human-code", "resume-ok", "--state-dir", str(tmp_path)]) == 0


def test_f024_kill_halt_stays_on_the_killed_book(tmp_path, monkeypatch):
    wall = datetime.now(UTC).replace(microsecond=0)
    opened = wall - timedelta(days=2)
    buy_at = wall - timedelta(days=1)
    bot = engine(tmp_path, sma_window=200)
    bot.run_once(now=opened, snapshot=snapshot(opened))
    held_bh = dict(bot.ledger.positions("buy_and_hold"))
    bot.broker.submit(
        "trend_daily",
        OrderIntent("BTC-USD", "buy", "trend_entry", quote_amount=Decimal("500")),
        f"trend_daily:BTC-USD:buy:{buy_at.date().isoformat()}",
        bot._context("trend_daily", snapshot(buy_at), buy_at),
        buy_at,
    )
    overlay_peak = bot.ledger.overlay_row("trend_daily")["peak"]
    dca_peak = bot.ledger.overlay_row("dca_weekly")["peak"]
    portfolio_peak = bot.ledger.get_meta("portfolio_peak")
    sleeve_peaks = {
        str(row["name"]): str(row["peak_equity"])
        for row in bot.ledger.conn.execute("SELECT name, peak_equity FROM sleeves")
    }
    bot.run_once(now=wall, snapshot=snapshot(wall, mid="10"))
    assert read_kill(tmp_path) is None
    assert not (tmp_path / "KILL").exists()
    assert bot.ledger.overlay_row("trend_daily")["state"] == "KILLED"
    assert bot.ledger.positions("trend_daily") == {}
    assert bot.ledger.positions("buy_and_hold") == held_bh
    assert bot.ledger.overlay_row("dca_weekly")["state"] != "KILLED"
    assert bot.ledger.overlay_row("trend_daily")["peak"] == overlay_peak
    assert bot.ledger.overlay_row("dca_weekly")["peak"] == dca_peak
    assert bot.ledger.get_meta("portfolio_peak") == portfolio_peak
    for name, stored in sleeve_peaks.items():
        assert bot.ledger.sleeve_row(name)["peak_equity"] == stored
    latest = {}
    for item in _events(bot, "decision"):
        latest[item["sleeve"]] = item
    assert latest["buy_and_hold"]["reason"] == "holding"
    assert latest["buy_and_hold"]["ts"] == iso(wall)
    assert latest["dca_weekly"]["reason"] != "kill_switch"
    assert latest["dca_weekly"]["ts"] == iso(wall)
    assert "kill_switch" not in {item["reason"] for item in _events(bot, "decision")}
    assert bot.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM fills WHERE sleeve='buy_and_hold' AND reason='drawdown_flatten'"
    ).fetchone()["n"] == 0
    bot.ledger.close()

    from rhbot.cli import main

    assert main(["resume", "--state-dir", str(tmp_path)]) == 2
    assert main(["resume", "--ack", "--state-dir", str(tmp_path)]) == 2
    secret = tmp_path / "human-code"
    secret.write_text("resume-ok\n", encoding="utf-8")
    monkeypatch.setenv("RHBOT_HUMAN_RESUME_FILE", str(secret))
    assert main(["resume", "--ack", "--human-code", "resume-ok", "--state-dir", str(tmp_path)]) == 0
    bot = engine(tmp_path, sma_window=200)
    assert bot.ledger.overlay_row("trend_daily")["peak"] == overlay_peak
    assert bot.ledger.get_meta("portfolio_peak") == portfolio_peak
    for name, stored in sleeve_peaks.items():
        assert bot.ledger.sleeve_row(name)["peak_equity"] == stored
    assert bot.ledger.positions("buy_and_hold") == held_bh
    bot.ledger.close()


def test_i003_status_and_report_leave_the_ledger_mtime(tmp_path):
    from rhbot.status import build_report

    wall = datetime.now(UTC).replace(microsecond=0)
    bot = engine(tmp_path, sma_window=200)
    bot.run_once(now=wall, snapshot=snapshot(wall))
    bot.ledger.close()
    path = tmp_path / "bot.sqlite"
    before = path.stat().st_mtime_ns
    size = path.stat().st_size
    body = assess(bot.settings, now=utcnow())
    report = build_report(bot.settings, "7d", now=utcnow())
    assert body["health"] in ("ok", "degraded", "critical", "idle_ok")
    assert report["started"] is True
    assert path.stat().st_mtime_ns == before
    assert path.stat().st_size == size


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
    assert bot.mark("trend_daily", view) == Decimal("895.95000000")
    assert bot.mark_to_bid("trend_daily", view) == bot.mark("trend_daily", view)
    assert bot.ledger.sleeve_row("trend_daily")["last_equity"] == "895.95000000"
    mid_dd = D("905") / D("1000") - 1
    mtb_dd = D("895.95") / D("1000") - 1
    assert mid_dd > Decimal("-0.10")
    assert mtb_dd <= Decimal("-0.10")
    assert bot.ledger.overlay_row("trend_daily")["state"] == "FROZEN"
    assert bot.ledger.overlay_row("dca_weekly")["state"] == "ARMED"
    assert bot.ledger.positions("buy_and_hold")
    bot.ledger.close()


def test_ir002_equity_peak_daily_loss_and_report_use_the_bid(tmp_path, now):
    """Mid can sit above a limit while the venue bid is already through it."""
    from rhbot.models import MarketSnapshot
    from rhbot.risk import RiskEngine, daily_buy_block
    from rhbot.status import build_report

    from tests.conftest import make_quote

    bot = engine(tmp_path, sma_window=200)
    view = snapshot(now)
    bot.run_once(now=now, snapshot=view)
    total = Decimal(0)
    for name in ("buy_and_hold", "dca_weekly", "trend_daily"):
        equity = D(bot.ledger.sleeve_row(name)["last_equity"])
        assert equity == bot.mark(name, view) == bot.mark_to_bid(name, view)
        total += equity
        mid_value = bot.ledger.cash(name)
        for symbol, qty in bot.ledger.positions(name).items():
            mid_value += qty * view.quotes[symbol].mid
        if bot.ledger.positions(name):
            assert equity < q8(mid_value)
    assert D(bot.ledger.get_meta("portfolio_peak")) == q8(total)
    report = build_report(bot.settings, "7d", now=utcnow())
    assert report["sleeves"]["buy_and_hold"]["equity"] == bot.ledger.sleeve_row("buy_and_hold")["last_equity"]

    day_start = D(bot.ledger.sleeve_row("buy_and_hold")["day_start_equity"])
    bot.ledger.conn.execute("DELETE FROM positions WHERE sleeve='buy_and_hold'")
    bot.ledger.conn.execute("UPDATE sleeves SET cash='0.00000000' WHERE name='buy_and_hold'")
    bot.ledger.conn.executemany(
        "INSERT INTO positions(sleeve, symbol, qty) VALUES('buy_and_hold', ?, ?)",
        (("BTC-USD", "5.00000000"), ("ETH-USD", "5.00000000")),
    )
    bot.ledger.conn.commit()
    bid_view = MarketSnapshot(
        bars={"BTC-USD": [], "ETH-USD": []},
        quotes={
            "BTC-USD": make_quote("BTC-USD", "100", now, bid=Decimal("90")),
            "ETH-USD": make_quote("ETH-USD", "100", now, bid=Decimal("90")),
        },
        source="test",
    )
    bid_equity = bot.mark("buy_and_hold", bid_view)
    assert bid_equity == Decimal("900.00000000")
    assert daily_buy_block(Decimal("1000"), day_start, bot.settings) is None
    assert daily_buy_block(bid_equity, day_start, bot.settings) is not None
    ctx = bot._context("buy_and_hold", bid_view, now)
    assert ctx.equity == bid_equity
    ctx.ordered_symbols_today = set()
    # The 90 bid is the mark. The order itself still needs a quote inside the spread cap.
    ctx.quotes = {
        "BTC-USD": make_quote("BTC-USD", "100", now),
        "ETH-USD": make_quote("ETH-USD", "100", now),
    }
    denied = RiskEngine(bot.settings).evaluate(
        OrderIntent("BTC-USD", "buy", "bid_loss", quote_amount=Decimal("20")),
        ctx,
        "buy_and_hold:BTC-USD:buy:bid-loss",
    )
    assert denied.reasons[0].startswith("daily_loss")
    ctx.equity = Decimal("1000")
    mid_ok = RiskEngine(bot.settings).evaluate(
        OrderIntent("BTC-USD", "buy", "mid_inside", quote_amount=Decimal("20")),
        ctx,
        "buy_and_hold:BTC-USD:buy:mid-inside",
    )
    assert "daily_loss" not in mid_ok.reasons
    bot.run_once(now=now, snapshot=bid_view)
    assert bot.ledger.sleeve_row("buy_and_hold")["last_equity"] == "900.00000000"
    assert bot.ledger.sleeve_row("buy_and_hold")["day_start_equity"] == money_str(day_start)
    assert D(bot.ledger.get_meta("portfolio_peak")) == q8(total)
    again = build_report(bot.settings, "7d", now=utcnow())
    assert again["sleeves"]["buy_and_hold"]["equity"] == "900.00000000"
    status = assess(bot.settings, now=utcnow())
    assert status["equity"]["buy_and_hold"] == "900.00000000"
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

    settings_bot = engine(tmp_path)
    strategy = TrendDaily(settings_bot.settings)
    closes = ["100"] * 210 + ["130"] * 20 + ["80"] * 40
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
    bot = engine(tmp_path)
    view = snapshot(now, closes=padded_closes("12"), last_open=now - timedelta(days=1))
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


def test_f004_quote_health_uses_cycle_age(tmp_path, now, monkeypatch):
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

    secret = resume_dir / "human-code"
    secret.write_text("resume-ok\n", encoding="utf-8")
    monkeypatch.setenv("RHBOT_HUMAN_RESUME_FILE", str(secret))
    assert main(["resume", "--ack", "--human-code", "resume-ok", "--state-dir", str(resume_dir)]) == 0


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
    bot = engine(tmp_path)
    last_open = now - timedelta(days=1)
    view = snapshot(now, closes=padded_closes("12"), last_open=last_open)
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

    bot = engine(tmp_path)
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
        (iso(market_now),),
    )
    raw.commit()
    raw.close()
    assert main(["audit", "replay", "--since", "30d", "--state-dir", str(tmp_path)]) == 2


def test_p1a_replay_starts_at_paper_day1_and_honors_since(tmp_path):
    """Warmup candles and later same-day cycles must not look like a mismatch."""
    from rhbot.backtest import diff_live
    from rhbot.cli import main
    from rhbot.models import MarketSnapshot
    from tests.conftest import make_settings

    day1 = datetime(2026, 8, 1, 0, 0, 40, tzinfo=UTC)
    warmup_last = day1 - timedelta(days=1)
    warmup_last = warmup_last.replace(hour=0, minute=0, second=0, microsecond=0)
    closes = ["100"] * 180 + ["110"] * 120
    series = {
        "BTC-USD": make_bars("BTC-USD", closes, warmup_last),
        "ETH-USD": make_bars("ETH-USD", closes, warmup_last),
    }
    bot = engine(tmp_path)
    for offset in range(31):
        day = day1 + timedelta(days=offset)
        bar_ts = day.replace(hour=0, minute=0, second=0, microsecond=0)
        for symbol in series:
            series[symbol] = [
                *series[symbol],
                Bar(
                    symbol=symbol,
                    ts=bar_ts,
                    open=Decimal("110"),
                    high=Decimal("110"),
                    low=Decimal("110"),
                    close=Decimal("110"),
                    volume=Decimal("1"),
                    source="test",
                ),
            ]
        for extra in (timedelta(0), timedelta(seconds=65)):
            market_now = day + extra
            window = {symbol: list(bars) for symbol, bars in series.items()}
            quotes = {
                symbol: Quote(
                    symbol=symbol,
                    ts=market_now,
                    mid=Decimal("110"),
                    bid=Decimal("110"),
                    ask=Decimal("110"),
                    source="test",
                )
                for symbol in window
            }
            bot.run_once(
                now=market_now,
                snapshot=MarketSnapshot(bars=window, quotes=quotes, source="test"),
            )
    assert len(bot.ledger.fills_for("buy_and_hold")) == 2
    assert any(row["reason"] == "dca_buy" for row in bot.ledger.fills_for("dca_weekly"))
    assert any(row["reason"] == "trend_entry" for row in bot.ledger.fills_for("trend_daily"))
    bot.ledger.close()
    report = diff_live(make_settings(tmp_path), "30d")
    assert report["ok"] is True, report["mismatches"][:12]
    assert report["mismatches"] == []

    raw = __import__("sqlite3").connect(tmp_path / "bot.sqlite")
    raw.execute(
        """
        INSERT INTO fills(
            sleeve, symbol, side, qty, qty_delta, mid, fill_price,
            cash_delta, cost, notional, ts, client_order_id, reason
        ) VALUES(
            'trend_daily', 'BTC-USD', 'buy', '1', '1', '110', '111',
            '-111', '1', '110', ?, 'injected-extra', 'extra'
        )
        """,
        (iso(day1 + timedelta(days=30, seconds=65)),),
    )
    raw.commit()
    raw.close()
    assert main(["audit", "replay", "--since", "30d", "--state-dir", str(tmp_path)]) == 2


def test_p1b_seventh_day_exit_fills_and_day_six_does_not(tmp_path):
    opened = datetime(2026, 4, 1, 0, 0, 50, tzinfo=UTC)
    bot = engine(tmp_path)
    bot.run_once(
        now=opened,
        snapshot=snapshot(opened, mid="12", closes=padded_closes("12"), last_open=opened - timedelta(days=1)),
    )
    assert bot.ledger.positions("trend_daily")
    too_soon = opened + timedelta(days=6)
    too_soon = too_soon.replace(hour=23, minute=59)
    bot.run_once(
        now=too_soon,
        snapshot=snapshot(
            too_soon,
            mid="8",
            closes=padded_closes("8", base="12"),
            last_open=too_soon - timedelta(days=1),
        ),
    )
    assert bot.ledger.positions("trend_daily")
    assert any(
        item["sleeve"] == "trend_daily" and "min_hold" in item["reason"]
        for item in _events(bot, "decision")
    )
    exit_at = opened + timedelta(days=7)
    exit_at = exit_at.replace(hour=0, minute=0, second=20)
    stale = snapshot(
        exit_at - timedelta(seconds=45),
        mid="8",
        closes=padded_closes("8", base="12"),
        last_open=exit_at - timedelta(days=1),
    )
    bot.run_once(now=exit_at, snapshot=stale)
    assert bot.ledger.positions("trend_daily")
    marked = (bot.ledger.strategy_state("trend_daily").get("evaluated_on") or {})
    assert marked.get("BTC-USD") != exit_at.date().isoformat()
    retry_at = exit_at + timedelta(minutes=1, seconds=25)
    bot.run_once(
        now=retry_at,
        snapshot=snapshot(
            retry_at,
            mid="8",
            closes=padded_closes("8", base="12"),
            last_open=retry_at - timedelta(days=1),
        ),
    )
    assert bot.ledger.positions("trend_daily") == {}
    assert any(row["reason"] == "trend_exit" for row in bot.ledger.fills_for("trend_daily"))
    bot.ledger.close()


def test_p1b_kill_flatten_on_day_two_still_sells(tmp_path):
    opened = datetime(2026, 5, 1, 0, 0, 50, tzinfo=UTC)
    bot = engine(tmp_path)
    bot.run_once(
        now=opened,
        snapshot=snapshot(opened, mid="12", closes=padded_closes("12"), last_open=opened - timedelta(days=1)),
    )
    held_bh = dict(bot.ledger.positions("buy_and_hold"))
    day2 = opened + timedelta(days=2)
    bot.run_once(
        now=day2,
        snapshot=snapshot(day2, mid="1", closes=padded_closes("1", base="12"), last_open=day2 - timedelta(days=1)),
    )
    assert day2 < opened + timedelta(days=7)
    assert bot.ledger.positions("trend_daily") == {}
    assert any(row["reason"] == "drawdown_flatten" for row in bot.ledger.fills_for("trend_daily"))
    assert bot.ledger.positions("buy_and_hold") == held_bh
    bot.ledger.close()


def test_p2_five_dollar_position_is_fully_sold(tmp_path, monkeypatch):
    from rhbot.cli import main

    wall = datetime.now(UTC).replace(microsecond=0)

    def _plant(bot) -> None:
        bot.ledger.conn.execute("DELETE FROM positions WHERE sleeve='trend_daily'")
        bot.ledger.conn.execute(
            "UPDATE sleeves SET cash='400.00000000' WHERE name='trend_daily'"
        )
        bot.ledger.conn.execute(
            "INSERT INTO positions(sleeve, symbol, qty) VALUES('trend_daily', 'BTC-USD', '0.05000000')"
        )
        bot.ledger.conn.commit()

    bot = engine(tmp_path, sma_window=200)
    bot.run_once(now=wall, snapshot=snapshot(wall))
    held_bh = dict(bot.ledger.positions("buy_and_hold"))
    _plant(bot)
    bot.run_once(now=wall, snapshot=snapshot(wall, mid="100"))
    assert bot.ledger.positions("trend_daily") == {}
    sold = [
        row
        for row in bot.ledger.fills_for("trend_daily")
        if row["reason"] == "drawdown_flatten" and row["symbol"] == "BTC-USD"
    ]
    assert len(sold) == 1
    assert D(sold[0]["qty"]) == Decimal("0.05000000")
    assert bot.ledger.positions("buy_and_hold") == held_bh
    bot.ledger.close()

    retry_dir = tmp_path / "retry"
    retry_dir.mkdir()
    bot = engine(retry_dir, sma_window=200)
    bot.run_once(now=wall, snapshot=snapshot(wall))
    _plant(bot)
    real_submit = bot.broker.submit

    def flaky(sleeve, intent, client_order_id, context, now, reduce_only=False):
        if intent.reason == "drawdown_flatten":
            raise OrderRejected(["stale_quote"])
        return real_submit(
            sleeve, intent, client_order_id, context, now, reduce_only=reduce_only
        )

    monkeypatch.setattr(bot.broker, "submit", flaky)
    bot.run_once(now=wall, snapshot=snapshot(wall, mid="100"))
    assert bot.ledger.positions("trend_daily").get("BTC-USD") == Decimal("0.05000000")
    bot.ledger.close()
    bot = engine(retry_dir, sma_window=200)
    bot.run_once(now=wall, snapshot=snapshot(wall, mid="100"))
    assert bot.ledger.positions("trend_daily") == {}
    assert D(
        next(
            row["qty"]
            for row in bot.ledger.fills_for("trend_daily")
            if row["reason"] == "drawdown_flatten"
        )
    ) == Decimal("0.05000000")
    bot.ledger.close()

    flat_dir = tmp_path / "flat"
    flat_dir.mkdir()
    bot = engine(flat_dir, sma_window=200)
    bot.run_once(now=wall, snapshot=snapshot(wall))
    _plant(bot)
    bot.ledger.close()
    assert main(["flatten", "--paper", "--state-dir", str(flat_dir)]) == 0
    bot = engine(flat_dir, sma_window=200)
    assert bot.ledger.positions("trend_daily") == {}
    assert any(
        row["reason"] == "flatten" and D(row["qty"]) == Decimal("0.05000000")
        for row in bot.ledger.fills_for("trend_daily")
    )
    bot.ledger.close()


def test_f011_shadow_enters_while_trend_is_frozen(tmp_path, now):
    from rhbot.status import build_report

    bot = engine(tmp_path)
    bot.run_once(now=now, snapshot=snapshot(now, closes=padded_closes("10"), last_open=now - timedelta(days=1)))
    _set_cash(bot, "trend_daily", "900")
    later = now + timedelta(days=1)
    hot = snapshot(later, closes=padded_closes("12"), last_open=later - timedelta(days=1))
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


def test_f021_shadow_trade_cap_is_per_sleeve(tmp_path, now):
    """Day 1 with every signal on: each shadow book has its own cap of 2.

    Buy-and-hold has no shadow book. Trend and DCA do. A shared shadow budget
    would let DCA take one slot and leave trend with one coin, and that gap
    would show up as overlay_effect. It must not.
    """
    from rhbot.risk import RiskEngine
    from rhbot.status import build_report

    bot = engine(tmp_path)
    view = snapshot(now, closes=padded_closes("12"), last_open=now - timedelta(days=1))
    bot.run_once(now=now, snapshot=view)
    day = now.date().isoformat()
    assert len(bot.ledger.fills_for("buy_and_hold")) == 2
    assert len(bot.ledger.fills_for("dca_weekly")) == 1
    assert len(bot.ledger.fills_for("trend_daily")) == 2
    assert bot.ledger.shadow_strategy_trades_today("dca_weekly_shadow", day) == 1
    assert bot.ledger.shadow_strategy_trades_today("trend_daily_shadow", day) == 2
    assert set(bot.ledger.shadow_positions("trend_daily_shadow")) == {"BTC-USD", "ETH-USD"}
    assert set(bot.ledger.positions("trend_daily")) == {"BTC-USD", "ETH-USD"}
    report = build_report(bot.settings, "30d", now=now + timedelta(days=1))
    trend_effect = report["no_overlay"]["sleeves"]["trend_daily_shadow"]["overlay_effect"]
    dca_effect = report["no_overlay"]["sleeves"]["dca_weekly_shadow"]["overlay_effect"]
    assert trend_effect["equity_delta"] == "0.00000000"
    assert dca_effect["equity_delta"] == "0.00000000"
    ctx = bot._shadow_context("trend_daily_shadow", view, now)
    ctx.ordered_symbols_today = set()
    ctx.cash = Decimal("1000")
    ctx.equity = Decimal("1000")
    ctx.positions = {}
    ctx.turnover_today = Decimal("0")
    denied = RiskEngine(bot.settings).evaluate(
        OrderIntent("BTC-USD", "buy", "third", quote_amount=Decimal("20")),
        ctx,
        "trend_daily_shadow:BTC-USD:buy:third",
        ignore_overlay=True,
    )
    assert denied.reasons == ["max_trades_per_day"]
    other = bot._shadow_context("dca_weekly_shadow", view, now)
    other.ordered_symbols_today = set()
    other.cash = Decimal("1000")
    other.equity = Decimal("1000")
    other.positions = {}
    other.turnover_today = Decimal("0")
    allowed = RiskEngine(bot.settings).evaluate(
        OrderIntent("ETH-USD", "buy", "still_open", quote_amount=Decimal("19.23")),
        other,
        "dca_weekly_shadow:ETH-USD:buy:still-open",
        ignore_overlay=True,
    )
    assert allowed.allowed
    bot.ledger.close()


def test_f022_kill_flatten_does_not_reuse_same_day_strategy_sell(tmp_path):
    wall = datetime.now(UTC).replace(microsecond=0)
    opened = wall - timedelta(days=9)
    buy_at = wall - timedelta(days=8)
    bot = engine(tmp_path, sma_window=200)
    bot.run_once(now=opened, snapshot=snapshot(opened))
    buy_view = snapshot(buy_at)
    bought = bot.broker.submit(
        "trend_daily",
        OrderIntent("BTC-USD", "buy", "trend_entry", quote_amount=Decimal("500")),
        f"trend_daily:BTC-USD:buy:{buy_at.date().isoformat()}",
        bot._context("trend_daily", buy_view, buy_at),
        buy_at,
    )
    held = bot.ledger.positions("trend_daily")["BTC-USD"]
    assert held == bought.qty
    partial = Decimal("0.10000000")
    exit_intent = OrderIntent("BTC-USD", "sell", "trend_exit", base_quantity=partial)
    exit_id = bot._client_id("trend_daily", exit_intent, wall)
    sold = bot.broker.submit(
        "trend_daily",
        exit_intent,
        exit_id,
        bot._context("trend_daily", snapshot(wall), wall),
        wall,
    )
    assert sold.reason == "trend_exit"
    assert sold.client_order_id == exit_id
    remaining = bot.ledger.positions("trend_daily")["BTC-USD"]
    assert remaining == held - partial
    flatten_intent = OrderIntent("BTC-USD", "sell", "drawdown_flatten", base_quantity=remaining)
    with pytest.raises(OrderRejected) as caught:
        bot.broker.submit(
            "trend_daily",
            flatten_intent,
            exit_id,
            bot._context("trend_daily", snapshot(wall), wall),
            wall,
            reduce_only=True,
        )
    assert caught.value.reasons == ["client_order_reason_mismatch"]
    assert bot.ledger.positions("trend_daily")["BTC-USD"] == remaining
    assert bot.ledger.get_fill(exit_id).reason == "trend_exit"
    bot.run_once(now=wall, snapshot=snapshot(wall, mid="10"))
    assert bot.ledger.positions("trend_daily") == {}
    sells = [
        row
        for row in bot.ledger.fills_for("trend_daily")
        if row["symbol"] == "BTC-USD" and row["side"] == "sell"
    ]
    assert {row["reason"] for row in sells} == {"trend_exit", "drawdown_flatten"}
    flatten = next(row for row in sells if row["reason"] == "drawdown_flatten")
    assert flatten["client_order_id"] != exit_id
    assert ":drawdown_flatten:" in flatten["client_order_id"]
    assert D(flatten["qty"]) == remaining
    assert bot.ledger.get_fill(exit_id).qty == partial
    bot.ledger.close()


def test_f020_stale_open_order_is_critical(tmp_path, now):
    bot = engine(tmp_path, sma_window=200)
    bot.run_once(now=now, snapshot=snapshot(now))
    wall = utcnow()
    client_order_id = "trend_daily:BTC-USD:buy:stuck"
    bot.ledger.insert_open_order(
        client_order_id=client_order_id,
        sleeve="trend_daily",
        symbol="BTC-USD",
        side="buy",
        ts=wall,
        reason="stuck",
    )
    bot.ledger.conn.commit()
    ok, detail = bot.ledger.reconcile("trend_daily", now=wall)
    assert ok, detail
    fresh = assess(bot.settings, now=wall)
    assert "open_order_stale" not in fresh["reasons"]
    bot.ledger.conn.execute(
        "UPDATE orders SET ts=? WHERE client_order_id=?",
        (iso(wall - timedelta(seconds=bot.settings.loop_seconds + 5)), client_order_id),
    )
    bot.ledger.conn.commit()
    ok, detail = bot.ledger.reconcile("trend_daily", now=wall)
    assert ok is False
    assert client_order_id in detail
    bot.ledger.close()
    body = assess(bot.settings, now=wall)
    assert body["health"] == "critical"
    assert "open_order_stale" in body["reasons"]
    assert client_order_id in body["checks"]["open_orders"]["client_order_ids"]


def test_f012_and_f013_are_covered_by_safety_tests():
    text = Path("tests/test_safety.py").read_text(encoding="utf-8")
    assert "http_write_findings" in text
    assert "config.yaml" in Path(".gitignore").read_text(encoding="utf-8")
