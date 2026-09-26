"""Option B paper drawdown: 10% buy pause, 40% kill, ack does not move the peak."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from rhbot.models import OrderIntent
from rhbot.ops import read_kill
from rhbot.status import assess

from tests.conftest import engine, snapshot


def test_ten_percent_pauses_buys_without_flattening_or_moving_the_peak(tmp_path, now):
    bot = engine(tmp_path, sma_window=20)
    bot.run_once(now=now, snapshot=snapshot(now))
    held = dict(bot.ledger.positions("buy_and_hold"))
    peak_before = bot.ledger.get_meta("portfolio_peak")
    sleeve_peak = bot.ledger.sleeve_row("buy_and_hold")["peak_equity"]
    later = now + timedelta(days=1)
    view = snapshot(later, mid="60")
    bot.run_once(now=later, snapshot=view)

    assert read_kill(tmp_path) is None
    assert (tmp_path / "DRAWDOWN_FREEZE").exists()
    assert bot.ledger.positions("buy_and_hold") == held
    assert bot.ledger.positions("dca_weekly") == {}
    assert bot.ledger.get_meta("portfolio_peak") == peak_before
    assert bot.ledger.sleeve_row("buy_and_hold")["peak_equity"] == sleeve_peak
    denials = [
        __import__("json").loads(row["payload"])
        for row in bot.ledger.conn.execute("SELECT payload FROM events WHERE kind='risk_denial'")
    ]
    assert any(
        item["sleeve"] == "dca_weekly" and item["reason"] == "drawdown_freeze" and item["side"] == "buy"
        for item in denials
    )

    status = assess(bot.settings, now=later)
    assert status["buy_pause"] is True
    assert status["kill_switch"] is False
    assert status["peak_equity"] == peak_before
    assert Decimal(status["drawdown_pct"]) >= Decimal("0.10")
    assert Decimal(status["drawdown_pct"]) < Decimal("0.40")
    assert status["ack_required"] is True
    assert status["rearm_eligible"] is False
    beat = bot.heartbeat_body()
    assert beat["buy_pause"] is True
    assert beat["kill_switch"] is False
    assert beat["peak_equity"] == peak_before
    assert Decimal(beat["drawdown_pct"]) >= Decimal("0.10")
    assert beat["ack_required"] is True
    assert beat["rearm_eligible"] is False

    from rhbot.cli import main

    assert main(
        ["ack-drawdown", "--reason", "reviewed the paper pause", "--state-dir", str(tmp_path)]
    ) == 0
    assert bot.ledger.get_meta("portfolio_peak") == peak_before
    assert bot.ledger.sleeve_row("buy_and_hold")["peak_equity"] == sleeve_peak
    assert main(["resume", "--ack", "--state-dir", str(tmp_path)]) == 0
    assert bot.ledger.get_meta("portfolio_peak") == peak_before
    assert bot.ledger.sleeve_row("buy_and_hold")["peak_equity"] == sleeve_peak

    still = later + timedelta(hours=1)
    bot.run_once(now=still, snapshot=snapshot(still, mid="60"))
    assert not (tmp_path / "DRAWDOWN_FREEZE").exists()
    assert read_kill(tmp_path) is None
    paused = assess(bot.settings, now=still)
    assert paused["buy_pause"] is False
    assert paused["kill_switch"] is False
    assert paused["rearm_eligible"] is False
    assert paused["peak_equity"] == peak_before

    recovered = now + timedelta(days=2)
    bot.run_once(now=recovered, snapshot=snapshot(recovered, mid="100"))
    assert assess(bot.settings, now=recovered)["rearm_eligible"] is True
    again = recovered + timedelta(hours=1)
    bot.run_once(now=again, snapshot=snapshot(again, mid="60"))
    assert (tmp_path / "DRAWDOWN_FREEZE").exists()
    assert read_kill(tmp_path) is None
    assert bot.ledger.get_meta("portfolio_peak") == peak_before
    assert bot.ledger.sleeve_row("buy_and_hold")["peak_equity"] == sleeve_peak
    assert bot.ledger.positions("buy_and_hold") == held
    bot.ledger.close()


def test_forty_percent_kills_flattens_and_resume_does_not_rebase(tmp_path):
    from rhbot.cli import main

    wall = datetime.now(timezone.utc)
    opened = wall - timedelta(days=2)
    bot = engine(tmp_path, sma_window=3)
    bot.run_once(now=opened, snapshot=snapshot(opened))
    invest_at = wall - timedelta(days=1)
    view = snapshot(invest_at)
    for symbol in ("BTC-USD", "ETH-USD"):
        bot.broker.submit(
            "trend_daily",
            OrderIntent(symbol, "buy", "trend_entry", quote_amount=Decimal("500")),
            f"trend-{symbol}",
            bot._context("trend_daily", view, invest_at),
            invest_at,
        )
    crashed = snapshot(wall, mid="30")
    bot.run_once(now=wall, snapshot=crashed)
    kill = read_kill(tmp_path)
    assert kill is not None and kill["ack_required"] is True
    assert bot.ledger.positions("buy_and_hold") == {}
    assert bot.ledger.positions("trend_daily") == {}
    peak_before = bot.ledger.get_meta("portfolio_peak")
    sleeve_peaks = {
        name: bot.ledger.sleeve_row(name)["peak_equity"] for name in bot.ledger.sleeve_names()
    }
    status = assess(bot.settings, now=wall)
    assert status["kill_switch"] is True
    assert status["ack_required"] is True
    assert status["peak_equity"] == peak_before
    assert Decimal(status["drawdown_pct"]) >= Decimal("0.40")
    beat = bot.heartbeat_body()
    assert beat["kill_switch"] is True
    assert beat["ack_required"] is True
    bot.ledger.close()

    assert main(["resume", "--state-dir", str(tmp_path)]) == 2
    assert (tmp_path / "KILL").exists()
    assert main(["resume", "--ack", "--state-dir", str(tmp_path)]) == 0
    assert not (tmp_path / "KILL").exists()

    bot = engine(tmp_path, sma_window=3)
    assert bot.ledger.get_meta("portfolio_peak") == peak_before
    for name, peak in sleeve_peaks.items():
        assert bot.ledger.sleeve_row(name)["peak_equity"] == peak
    bot.run_once(now=wall, snapshot=crashed)
    assert read_kill(tmp_path) is None
    assert bot.ledger.get_meta("portfolio_peak") == peak_before
    assert bot.ledger.get_meta("kill_ack_peak") == peak_before
    bot.ledger.close()


def test_same_daily_intent_returns_the_original_fill(tmp_path, now):
    bot = engine(tmp_path, sma_window=20)
    view = snapshot(now)
    bot.run_once(now=now, snapshot=view)
    day = now.date().isoformat()
    intent = OrderIntent("BTC-USD", "buy", "same_day", quote_amount=Decimal("100"))
    client_id = bot._client_id("buy_and_hold", intent, now)
    assert client_id == f"buy_and_hold-BTC-USD-buy-{day}"
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
