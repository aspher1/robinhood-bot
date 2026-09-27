from datetime import timedelta
from decimal import Decimal
import sqlite3

import pytest

from rhbot.insights import build_dashboard
from rhbot.ops import iso
from tests.conftest import engine, make_settings, snapshot


def test_dashboard_before_start_is_read_only(tmp_path, now):
    view = build_dashboard(make_settings(tmp_path), now=now)
    assert view["started"] is False
    assert view["history"] == {}
    assert view["recent_fills"] == []
    assert view["activity"] == []
    assert view["report"]["started"] is False
    assert not tmp_path.joinpath("bot.sqlite").exists()
    assert list(tmp_path.iterdir()) == []


def test_dashboard_reads_real_fills_events_and_bounded_snapshots(tmp_path, now):
    bot = engine(tmp_path)
    bot.run_once(now=now, snapshot=snapshot(now))
    ledger = bot.ledger
    for index in range(12):
        ledger.snapshot("buy_and_hold", Decimal(1000 + index), now + timedelta(minutes=index))
    ledger.snapshot("buy_and_hold", Decimal(777), now - timedelta(days=9))
    ledger.snapshot("buy_and_hold", Decimal(999), now - timedelta(hours=2))
    ledger.log_event("decision", {"sleeve": "buy_and_hold", "reason": "test", "orders": []}, now)
    ledger.set_meta("starting_cash", "sentinel")
    path = ledger.path
    ledger.close()
    before = path.stat().st_mtime_ns

    view = build_dashboard(bot.settings, "1d", now + timedelta(minutes=10), limit=2, max_points=4)

    assert view["started"] is True
    assert view["history_counts"]["buy_and_hold"] == 14  # 13 in window and a baseline
    sampled = view["history"]["buy_and_hold"]
    assert len(sampled) == 4
    assert sampled[0]["ts"] == iso(now - timedelta(days=9))
    assert sampled[-1]["ts"] == iso(now + timedelta(minutes=10))
    assert [row["ts"] for row in sampled] == sorted(row["ts"] for row in sampled)
    assert len(view["recent_fills"]) == 2
    assert [row["id"] for row in view["recent_fills"]] == sorted(
        (row["id"] for row in view["recent_fills"]), reverse=True
    )
    assert all(row["ts"] <= view["to"] for row in view["recent_fills"])
    assert len(view["activity"]) == 2
    assert view["activity"][0]["summary"] == "test (0 proposed orders)"
    assert "payload" not in view["activity"][0]
    assert path.stat().st_mtime_ns == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='starting_cash'").fetchone()[0] == "sentinel"
    assert view["report"]["started"]
    assert view["summary_basis"] == "latest_recorded"


def test_dashboard_filters_and_orders_by_recorded_time(tmp_path, now):
    bot = engine(tmp_path)
    bot.run_once(now=now, snapshot=snapshot(now))
    ledger = bot.ledger
    future = now + timedelta(days=1)
    old = now - timedelta(days=2)
    later = now + timedelta(minutes=5)
    earlier = now + timedelta(minutes=1)
    # Deliberately insert rows in a different order than their recorded times.
    for name, when in (("later", later), ("old", old), ("future", future), ("earlier", earlier)):
        ledger.conn.execute(
            """INSERT INTO fills(sleeve, symbol, side, qty, qty_delta, mid,
                fill_price, cash_delta, cost, notional, ts, client_order_id, reason)
                VALUES('buy_and_hold', 'BTC-USD', 'buy', '1', '1', '1', '1',
                       '-1', '0', '1', ?, ?, ?)""",
            (iso(when), f"dashboard-{name}", name),
        )
        ledger.log_event("decision", {"sleeve": "buy_and_hold", "reason": name}, when)
    ledger.close()

    view = build_dashboard(bot.settings, "1d", now + timedelta(minutes=10), limit=2)
    assert [item["reason"] for item in view["recent_fills"]] == ["later", "earlier"]
    assert [item["summary"] for item in view["activity"]] == [
        "later (0 proposed orders)",
        "earlier (0 proposed orders)",
    ]
    assert all(view["from"] <= item["ts"] <= view["to"] for item in view["recent_fills"])
    assert all(view["from"] <= item["ts"] <= view["to"] for item in view["activity"])


@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": 51}, {"limit": True}, {"max_points": 1}, {"max_points": 361}, {"max_points": 2.5}, {"since_text": "0d"}])
def test_dashboard_rejects_invalid_bounds(tmp_path, now, options):
    with pytest.raises(ValueError):
        build_dashboard(make_settings(tmp_path), now=now, **options)


def test_dashboard_rejects_naive_timestamp(tmp_path):
    from datetime import datetime

    with pytest.raises(ValueError, match="timezone-aware"):
        build_dashboard(make_settings(tmp_path), now=datetime(2026, 3, 16))
