"""Dashboard labels and escaping against the real status/report shape."""

from datetime import datetime, timezone

from rhbot.dashboard import _chart, render_dashboard
from rhbot.status import assess, build_report
from tests.conftest import engine, make_settings, snapshot


def _payload(settings, started=False):
    report = build_report(settings, "24h")
    return {
        "version": 1,
        "generated_at": "2026-09-26T12:00:00Z",
        "since": "24h",
        "from": report["from"],
        "to": report["to"],
        "started": started,
        "report": report,
        "health": assess(settings),
        "history": {},
        "history_counts": {},
        "recent_fills": [],
        "activity": [],
        "limits": {"activity": 20, "fills": 20, "points_per_sleeve": 60},
    }


def test_empty_snapshot_is_honest_and_self_contained(tmp_path):
    html = render_dashboard(_payload(make_settings(tmp_path)))
    assert "No paper results yet" in html
    assert "No simulated fills" in html
    assert "PAPER ONLY" in html
    assert "does not update itself" in html
    assert "<script" not in html.lower()
    assert "<link" not in html.lower()
    assert "https://" not in html


def test_metrics_from_real_report_and_status(tmp_path):
    bot = engine(tmp_path)
    now = datetime.now(timezone.utc)
    bot.run_once(now=now, snapshot=snapshot(now))
    bot.ledger.close()
    payload = _payload(make_settings(tmp_path), True)
    payload["history"] = {"buy_and_hold": [{"ts": "2026-09-26T12:00:00Z", "equity": "989.00", "cash": "10"}]}
    payload["history_counts"] = {"buy_and_hold": 5}
    payload["limits"]["points_per_sleeve"] = 1
    html = render_dashboard(payload)
    assert "Buy and hold" in html
    assert "Since start P&amp;L" in html
    assert "Window return" in html
    assert "Trading costs in window" in html
    assert "Showing 1 of 5 observations (sampled)" in html
    assert "Without drawdown overlay" in html
    assert "Open positions" in html
    assert "Quote age" in html
    assert "SIMULATION" in html
    assert "Last recorded equity" in html
    assert "Max window drawdown" in html
    assert "percentage points" in html


def test_every_untrusted_string_is_html_escaped(tmp_path):
    payload = _payload(make_settings(tmp_path))
    attack = '<img src=x onerror="alert(1)">'
    payload["generated_at"] = attack
    payload["since"] = attack
    payload["health"]["reasons"] = [attack]
    payload["recent_fills"] = [{"ts": attack, "sleeve": attack, "symbol": attack, "side": attack, "qty": attack, "fill_price": attack, "cost": attack, "reason": attack}]
    payload["activity"] = [{"ts": attack, "kind": attack, "sleeve": attack, "summary": attack}]
    payload["started"] = True
    payload["report"]["sleeves"] = {"trend_daily": {"equity": "1000", "cash": "1000", "since_start": {"pnl": "0", "return_pct": "0"}, "window": {"pnl": "0", "return_pct": "0", "trades": 0, "fees": "0"}}}
    payload["health"]["overlay"] = {"trend_daily": {"state": attack}}
    payload["health"]["positions"] = {"trend_daily": {attack: attack}}
    payload["history"] = {"trend_daily": [{"ts": attack, "equity": "1000"}]}
    html = render_dashboard(payload)
    assert attack not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert "<script" not in html.lower()


def test_chart_uses_elapsed_time_and_handles_single_flat_observation():
    points = [
        {"ts": "2026-09-26T00:00:00Z", "equity": "1000"},
        {"ts": "2026-09-26T01:00:00Z", "equity": "1100"},
        {"ts": "2026-09-26T10:00:00Z", "equity": "1050"},
    ]
    chart = _chart(points, 3, 50)
    assert 'points="48.0,166.0 116.4,42.0 732.0,104.0"' in chart
    flat = _chart([{"ts": "2026-09-26T00:00:00Z", "equity": "1000"}], 1, 50)
    assert '<circle cx="48.0" cy="104.0"' in flat
    assert '<path d="M48 104H732"' in flat
    assert "$1,000.00" in flat


def test_reconciliation_failure_is_visible(tmp_path):
    payload = _payload(make_settings(tmp_path))
    payload["report"]["fidelity_ok"] = False
    payload["health"]["checks"]["reconciliation"] = {"ok": False}
    html = render_dashboard(payload)
    assert 'role="alert"' in html
    assert "Ledger integrity warning" in html
    assert "unverified" in html
