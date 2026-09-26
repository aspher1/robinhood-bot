"""Offline, self-contained HTML view of one read-only paper snapshot."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from html import escape
from math import isfinite

NAMES = {"buy_and_hold": "Buy and hold", "dca_weekly": "Weekly DCA", "trend_daily": "Daily trend"}


def _text(value: object) -> str:
    return escape("N/A" if value is None or value == "" else str(value), quote=True)


def _time(value: object) -> str:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            return stamp.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    except (ValueError, OverflowError):
        pass
    return str(value) if value else "N/A"


def _number(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _money(value: object, signed: bool = False) -> str:
    amount = _number(value)
    if amount is None:
        return "N/A"
    sign = "+" if signed and amount > 0 else "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def _percent(value: object, signed: bool = False) -> str:
    amount = _number(value)
    if amount is None:
        return "N/A"
    sign = "+" if signed and amount > 0 else "-" if amount < 0 else ""
    return f"{sign}{abs(amount):,.2f}%"


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _chart(points: list, count: object, limit: object) -> str:
    valid = [(str(p.get("ts", "")), _number(p.get("equity"))) for p in points if isinstance(p, dict)]
    valid = [(ts, eq) for ts, eq in valid if eq is not None]
    if not valid:
        return '<p class="empty">No equity observations in this window.</p>'
    vals = [eq for _, eq in valid]
    low, high = min(vals), max(vals)
    span = high - low
    timestamps = []
    for ts, _ in valid:
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            timestamps.append(parsed.timestamp())
        except (ValueError, OverflowError, OSError):
            timestamps = []
            break
    use_times = len(timestamps) == len(valid) and max(timestamps) > min(timestamps)
    coords = []
    for index, (_, value) in enumerate(valid):
        fraction = ((timestamps[index] - min(timestamps)) / (max(timestamps) - min(timestamps))) if use_times else index / max(1, len(valid) - 1)
        x = 48 + fraction * 684
        y = 104 if span == 0 else 166 - float((value - low) / span) * 124
        if isfinite(y):
            coords.append(f"{x:.1f},{y:.1f}")
    if not coords:
        return '<p class="empty">No equity observations in this window.</p>'
    polyline = " ".join(coords)
    line = (f'<circle cx="{coords[0].split(",")[0]}" cy="{coords[0].split(",")[1]}" r="5" class="dot"/>'
            if len(coords) == 1 else f'<polyline points="{polyline}" class="line"/>')
    guides = '<path d="M48 104H732" class="gridline"/>' if span == 0 else '<path d="M48 42H732M48 166H732" class="gridline"/>'
    labels = f'<text x="0" y="108">{_money(low)}</text>' if span == 0 else f'<text x="0" y="46">{_money(high)}</text><text x="0" y="170">{_money(low)}</text>'
    svg = (f'<svg viewBox="0 0 760 207" role="img" aria-label="Paper equity from {_text(valid[0][0])} '
           f'to {_text(valid[-1][0])}, range {_money(low)} to {_money(high)}" preserveAspectRatio="xMidYMid meet">'
           f'{guides}{line}{labels}'
           f'<text x="48" y="196">{_text(valid[0][0][:10])}</text>'
           f'<text x="732" y="196" text-anchor="end">{_text(valid[-1][0][:10])}</text></svg>')
    total, cap = _number(count), _number(limit)
    note = f'<p class="hint">Showing {len(valid)} of {int(total)} observations (sampled).</p>' if total is not None and total > len(valid) and (cap is None or cap > 0) else f'<p class="hint">{len(valid)} recorded observation(s) shown.</p>'
    note += f'<p class="hint">Equity range: {_money(low)} to {_money(high)}. Observations: {_text(_time(valid[0][0]))} to {_text(_time(valid[-1][0]))}.</p>'
    return svg + note


def _table(headers: tuple[str, ...], rows: list[str], empty: str) -> str:
    head = "".join(f"<th scope=\"col\">{_text(h)}</th>" for h in headers)
    body = "".join(rows) if rows else f'<tr><td colspan="{len(headers)}" class="empty">{_text(empty)}</td></tr>'
    return f'<div class="table-scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _row(*cells: object) -> str:
    return "<tr>" + "".join(f"<td>{_text(cell)}</td>" for cell in cells) + "</tr>"


def render_dashboard(data: dict) -> str:
    """Render supplied snapshot only; never fetch quotes, modify state, or execute scripts."""
    report, health = _dict(data.get("report")), _dict(data.get("health"))
    started = bool(data.get("started", report.get("started", False)))
    sleeves = _dict(report.get("sleeves"))
    histories = _dict(data.get("history"))
    counts = _dict(data.get("history_counts"))
    limits = _dict(data.get("limits"))
    positions = _dict(health.get("positions"))
    overlays = _dict(health.get("overlay"))
    shadows = _dict(_dict(report.get("no_overlay")).get("sleeves"))
    window = data.get("since", report.get("since", ""))
    status = str(health.get("health") or "unknown")
    reason_labels = {"not_started": "Not started", "kill_switch": "Manual kill switch engaged", "drawdown_breach": "A strategy is killed", "drawdown_freeze": "A strategy is frozen", "quote_hard_stop": "Quote hard stop", "stale_market_data": "Market data stale", "heartbeat_stale": "Heartbeat stale", "no_heartbeat": "No heartbeat", "reconciliation": "Ledger reconciliation failed"}
    reasons = ", ".join(reason_labels.get(str(r), str(r).replace("_", " ").capitalize()) for r in _list(health.get("reasons"))) or "No health alerts"
    fidelity_problem = report.get("fidelity_ok") is False or _dict(_dict(health.get("checks")).get("reconciliation")).get("ok") is False
    warning = '<div class="warning" role="alert"><b>Ledger integrity warning.</b> Stored cash or positions may not match the recorded fills. Treat performance figures as unverified until reconciliation passes.</div>' if fidelity_problem else ""
    cards = []
    if started:
        for name in NAMES:
            item = _dict(sleeves.get(name))
            if not item:
                continue
            total, period = _dict(item.get("since_start")), _dict(item.get("window"))
            overlay = _dict(overlays.get(name))
            state = overlay.get("state", "Benchmark" if name == "buy_and_hold" else "Unknown")
            held = _dict(positions.get(name))
            held_text = ", ".join(f"{_text(sym)} {_text(qty)}" for sym, qty in held.items() if _number(qty) != 0) or "No open positions"
            shadow_name = {"dca_weekly": "dca_weekly_shadow", "trend_daily": "trend_daily_shadow"}.get(name)
            shadow = _dict(shadows.get(shadow_name))
            shadow_html = ""
            if shadow:
                effect = _dict(shadow.get("overlay_effect"))
                shadow_html = (f'<p class="shadow">Without drawdown overlay: {_money(shadow.get("equity"))} equity. '
                               f'Current equity difference: {_money(effect.get("equity_delta"), signed=True)}.</p>')
            points = _list(histories.get(name))
            excess = item.get("excess_return_vs_buy_and_hold_pct")
            benchmark_label = f'{_percent(excess, signed=True).removesuffix("%")} percentage points' if excess is not None else "Benchmark"
            cards.append(f'''<article class="strategy">
              <div class="strategy-head"><div><p class="eyebrow">PAPER PORTFOLIO</p><h3>{_text(NAMES[name])}</h3></div><span class="state">{_text(state)}</span></div>
              <div class="metrics"><div><small>Last recorded equity</small><strong>{_money(item.get("equity"))}</strong></div><div><small>Cash available</small><strong>{_money(item.get("cash"))}</strong></div><div><small>Since start P&amp;L</small><strong>{_money(total.get("pnl"), signed=True)}</strong><span>{_percent(total.get("return_pct"), signed=True)} return</span></div></div>
              <div class="window"><div><small>Window P&amp;L</small><b>{_money(period.get("pnl"), signed=True)}</b></div><div><small>Window return</small><b>{_percent(period.get("return_pct"), signed=True)}</b></div><div><small>Trades in window</small><b>{_text(period.get("trades"))}</b></div><div><small>Trading costs in window</small><b>{_money(period.get("fees"))}</b></div><div><small>Max window drawdown</small><b>{_percent(period.get("max_drawdown_pct"))}</b></div><div><small>Vs. buy and hold return</small><b>{benchmark_label}</b></div></div>
              <div class="chart">{_chart(points, counts.get(name), limits.get("points_per_sleeve"))}</div>
              <p class="positions"><b>Open positions:</b> {held_text}</p>{shadow_html}
            </article>''')
    if not cards:
        cards.append('<div class="not-started"><h3>No paper results yet</h3><p>Run the paper bot to record its first simulated cycle. Performance, positions, and equity history will appear here after observations exist.</p></div>')
    fill_rows = []
    for fill in _list(data.get("recent_fills")):
        if isinstance(fill, dict):
            fill_rows.append(_row(_time(fill.get("ts")), NAMES.get(str(fill.get("sleeve")), fill.get("sleeve")), fill.get("side"), fill.get("symbol"), fill.get("qty"), _money(fill.get("fill_price")), _money(fill.get("cost")), fill.get("reason")))
    activity_rows = []
    for event in _list(data.get("activity")):
        if isinstance(event, dict):
            kind = str(event.get("kind") or "Event").replace("_", " ").capitalize()
            activity_rows.append(_row(_time(event.get("ts")), kind, NAMES.get(str(event.get("sleeve")), event.get("sleeve")), event.get("summary")))
    quote_age = health.get("data_age_seconds")
    age_text = f"{_text(quote_age)} seconds at snapshot" if quote_age is not None else "No quote age available"
    html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper Trading Dashboard</title><style>
:root{{--navy:#10273a;--ink:#203547;--muted:#526779;--teal:#167d79;--line:#d9e2e2;--ivory:#f5f4ef;--white:#fff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--ivory);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}}main{{max-width:1180px;margin:auto;padding:28px 24px 80px}}
header{{background:var(--navy);color:#fff;padding:48px max(24px,calc((100vw - 1132px)/2));border-bottom:5px solid var(--teal)}}h1{{font-size:clamp(2rem,5vw,3.6rem);line-height:1.08;letter-spacing:-.04em;margin:6px 0 12px}}h2{{font-size:1.45rem;margin:42px 0 16px;letter-spacing:-.02em}}h3{{font-size:1.35rem;line-height:1.2;margin:0}}p{{margin:0 0 12px}}.eyebrow{{font-size:.72rem;letter-spacing:.15em;font-weight:800;color:#a0dfd7;margin:0 0 8px}}.subtitle{{color:#d4e0e5;max-width:700px}}.badges{{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}}.badge{{background:#d9f0eb;color:#123c43;border-radius:100px;padding:7px 13px;font-weight:700;font-size:.85rem}}.badge.critical{{background:#fce5db;color:#832d25}}.badge.degraded{{background:#fff0c9;color:#704f00}}.summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin:22px 0}}.tile,.strategy,.not-started,.panel{{background:var(--white);border:1px solid var(--line);border-radius:15px;box-shadow:0 5px 24px #10273a0a}}.tile{{padding:18px 20px}}small{{display:block;font-size:.76rem;color:var(--muted);font-weight:700;letter-spacing:.025em}}.tile b{{display:block;margin-top:7px;font-size:1.05rem;overflow-wrap:anywhere}}.strategy{{margin:16px 0;padding:26px}}.strategy .eyebrow{{color:var(--teal)}}.strategy-head{{display:flex;align-items:start;justify-content:space-between;gap:12px}}.state{{border:1px solid var(--line);border-radius:30px;padding:4px 11px;font-size:.8rem;font-weight:700;white-space:nowrap}}.metrics,.window{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:24px}}.metrics strong{{display:block;font-size:clamp(1.15rem,2.4vw,1.75rem);color:var(--navy);line-height:1.25;margin-top:5px}}.metrics span{{font-size:.8rem;color:var(--muted)}}.window{{grid-template-columns:repeat(4,minmax(0,1fr));padding:17px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}}.window b{{font-size:1rem}}.chart{{margin-top:18px;min-width:0}}svg{{display:block;width:100%;height:auto;max-height:250px;font-size:12px;fill:var(--muted)}}.gridline{{stroke:var(--line);stroke-width:1}}.line{{fill:none;stroke:var(--teal);stroke-width:3;stroke-linejoin:round;stroke-linecap:round}}.dot{{fill:var(--teal)}}.hint,.positions,.shadow,.empty{{color:var(--muted);font-size:.88rem}}.positions,.shadow{{margin-top:11px;overflow-wrap:anywhere}}.not-started{{padding:28px}}.warning{{padding:17px 20px;background:#fff0e9;border:1px solid #dba58a;border-radius:12px;color:#723d25;margin:20px 0}}.panel{{padding:20px;margin:16px 0}}.table-scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:700px;text-align:left;font-size:.87rem}}th,td{{padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}}th{{color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.05em}}tr:last-child td{{border-bottom:0}}.foot{{border-top:1px solid var(--line);padding-top:20px;color:var(--muted);font-size:.85rem}}@media(max-width:700px){{main{{padding:18px 15px 55px}}header{{padding:32px 18px}}.strategy{{padding:19px}}.metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}.window{{grid-template-columns:repeat(2,minmax(0,1fr))}}.strategy-head{{flex-wrap:wrap}}}}
</style></head><body><header><p class="eyebrow">SIMULATION · NO REAL ORDERS</p><h1>Paper trading dashboard</h1><p class="subtitle">A read-only snapshot of three virtual BTC and ETH portfolios. Market quotes come from public Coinbase data. Fills and P&amp;L are simulated after estimated costs. Past results do not predict future returns.</p><div class="badges"><span class="badge">PAPER ONLY</span><span class="badge {_text(status) if status in ("critical", "degraded") else ""}">Health: {_text(status.replace("_", " "))}</span></div></header>
<main>{warning}<section aria-label="Snapshot details"><div class="summary"><div class="tile"><small>Snapshot generated</small><b>{_text(_time(data.get("generated_at")))}</b></div><div class="tile"><small>Reporting window</small><b>{_text(window)} · {_text(_time(data.get("from", report.get("from"))))} to {_text(_time(data.get("to", report.get("to"))))}</b></div><div class="tile"><small>Last successful cycle</small><b>{_text(_time(health.get("last_successful_action_at")))}</b></div><div class="tile"><small>Quote age</small><b>{age_text}</b></div></div><p class="hint">This file does not update itself. Generate a new snapshot for current health and results. Health: {_text(reasons)}.</p><p class="hint">Window P&amp;L uses the latest recorded equity before the window as its baseline when available. If no earlier observation exists, it uses starting cash. Recent fills and audit activity show at most {_text(limits.get("fills", 50))} and {_text(limits.get("activity", 50))} rows respectively.</p></section>
<section aria-label="Paper portfolios"><h2>Strategy results</h2>{''.join(cards)}</section>
<section aria-label="Recent paper fills"><h2>Recent simulated fills</h2><div class="panel">{_table(("Time", "Portfolio", "Side", "Market", "Quantity", "Fill price", "Trading cost", "Reason"),fill_rows,"No simulated fills in this snapshot.")}</div></section>
<section aria-label="Recent activity"><h2>Recent audit activity</h2><div class="panel">{_table(("Time", "Event", "Portfolio", "Detail"),activity_rows,"No audit activity in this snapshot.")}</div></section><p class="foot">Paper simulation only. Per-strategy portfolios each start with {_money(report.get("starting_cash_per_sleeve"))} in virtual cash. Costs and metrics are drawn from the saved report; this page sends no requests and contains no live trading controls.</p></main></body></html>'''
    return html
