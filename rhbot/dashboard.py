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


def _signed(value: object, formatter) -> str:
    """Signed money/percent wrapped in a pos/neg span for color coding."""
    amount = _number(value)
    cls = "pos" if amount is not None and amount > 0 else "neg" if amount is not None and amount < 0 else ""
    text = formatter(value, signed=True)
    return f'<span class="{cls}">{text}</span>' if cls else text


def _state_pill(state: object) -> str:
    label = _text(state)
    lowered = str(state).lower()
    cls = "pill"
    if "kill" in lowered:
        cls += " pill-kill"
    elif "fro" in lowered:  # frozen / freeze
        cls += " pill-frozen"
    elif "active" in lowered or "benchmark" in lowered:
        cls += " pill-live"
    return f'<span class="{cls}">{label}</span>'


def _side_pill(side: object) -> str:
    lowered = str(side).lower()
    cls = "side-buy" if "buy" in lowered else "side-sell" if "sell" in lowered else ""
    return f'<span class="side {cls}">{_text(side)}</span>'


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _chart(points: list, count: object, limit: object, gid: str = "eq") -> str:
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
            coords.append((x, y))
    if not coords:
        return '<p class="empty">No equity observations in this window.</p>'
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    first_x, first_y = coords[0]
    last_x, last_y = coords[-1]
    if len(coords) == 1:
        graphic = f'<circle cx="{first_x:.1f}" cy="{first_y:.1f}" r="6" class="dot"/>'
    else:
        area = f'M{first_x:.1f},176 ' + " ".join(f'L{x:.1f},{y:.1f}' for x, y in coords) + f' L{last_x:.1f},176 Z'
        graphic = (f'<path d="{area}" fill="url(#{gid})"/>'
                   f'<polyline points="{pts}" class="line"/>'
                   f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="5.5" class="dot"/>')
    guides = '<path d="M48 104H732" class="gridline"/>' if span == 0 else '<path d="M48 42H732M48 166H732" class="gridline"/>'
    labels = (f'<text x="0" y="108" class="axis">{_money(low)}</text>' if span == 0
              else f'<text x="0" y="46" class="axis">{_money(high)}</text><text x="0" y="170" class="axis">{_money(low)}</text>')
    svg = (f'<svg viewBox="0 0 760 207" role="img" aria-label="Paper equity from {_text(valid[0][0])} '
           f'to {_text(valid[-1][0])}, range {_money(low)} to {_money(high)}" preserveAspectRatio="xMidYMid meet">'
           f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
           f'<stop offset="0" stop-color="#0e7c7b" stop-opacity="0.32"/>'
           f'<stop offset="1" stop-color="#0e7c7b" stop-opacity="0.02"/></linearGradient></defs>'
           f'{guides}{graphic}{labels}'
           f'<text x="48" y="198" class="axis">{_text(valid[0][0][:10])}</text>'
           f'<text x="732" y="198" text-anchor="end" class="axis">{_text(valid[-1][0][:10])}</text></svg>')
    total, cap = _number(count), _number(limit)
    if total is not None and total > len(valid) and (cap is None or cap > 0):
        note = f'<p class="hint">Showing {len(valid)} of {int(total)} observations (sampled).</p>'
    else:
        note = f'<p class="hint">{len(valid)} recorded observation(s) shown.</p>'
    note += (f'<p class="hint">Equity range: {_money(low)} to {_money(high)}. '
             f'Observations: {_text(_time(valid[0][0]))} to {_text(_time(valid[-1][0]))}.</p>')
    return svg + note


def _table(headers: tuple[str, ...], rows: list[str], empty: str) -> str:
    head = "".join(f"<th scope=\"col\">{_text(h)}</th>" for h in headers)
    body = "".join(rows) if rows else f'<tr><td colspan="{len(headers)}" class="empty">{_text(empty)}</td></tr>'
    return f'<div class="table-scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _row(*cells: object) -> str:
    return "<tr>" + "".join(f"<td>{_text(cell)}</td>" for cell in cells) + "</tr>"


CSS = """
:root{
  --ink:#0f1e2e;--ink2:#33475c;--muted:#64748b;--faint:#94a3b8;
  --bg:#edf0f4;--card:#ffffff;--line:#e3e9f0;
  --teal:#0e7c7b;--teal-deep:#0a5f5e;
  --pos:#12805c;--pos-bg:#e3f4ec;--neg:#cf3f31;--neg-bg:#fbe9e6;
  --amber:#b7791f;--amber-bg:#fdf3dd;
  --navy1:#0c2237;--navy2:#14395c;
  --radius:16px;
  --shadow:0 1px 2px rgba(15,30,46,.06),0 10px 30px rgba(15,30,46,.08);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:1180px;margin:auto;padding:30px 24px 90px}
/* ---------- header ---------- */
header{background:radial-gradient(1100px 380px at 18% -20%,#1d5178 0%,var(--navy2) 45%,var(--navy1) 78%,#081627 100%);color:#fff;padding:56px max(24px,calc((100vw - 1132px)/2)) 52px;position:relative;overflow:hidden}
header::after{content:"";position:absolute;inset:0;background:radial-gradient(600px 200px at 85% 120%,rgba(20,125,121,.35),transparent 70%);pointer-events:none}
.eyebrow{font-size:.72rem;letter-spacing:.22em;font-weight:800;color:#7fd8cf;margin:0 0 10px;position:relative}
h1{font-size:clamp(2.1rem,5vw,3.4rem);line-height:1.05;letter-spacing:-.035em;margin:0 0 14px;position:relative;font-weight:800}
.subtitle{color:#c3d4e2;max-width:720px;margin:0;position:relative;font-size:1.02rem}
.badges{display:flex;gap:10px;flex-wrap:wrap;margin-top:24px;position:relative}
.badge{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.22);color:#fff;border-radius:100px;padding:7px 14px;font-weight:700;font-size:.82rem;backdrop-filter:blur(4px)}
.badge.critical{background:#fbe9e6;border-color:#f0b9b0;color:#8f2f24}
.badge.degraded{background:#fdf3dd;border-color:#ecd9a8;color:#7c5a12}
/* ---------- summary tiles ---------- */
.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin:26px 0 8px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:20px 22px;position:relative;overflow:hidden}
.tile::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:linear-gradient(var(--teal),#37b3ae);border-radius:4px 0 0 4px}
.tile small{display:block;font-size:.74rem;color:var(--muted);font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.tile b{display:block;margin-top:8px;font-size:1.04rem;font-weight:650;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.hint{color:var(--muted);font-size:.87rem;max-width:900px}
h2{font-size:1.5rem;margin:44px 0 6px;letter-spacing:-.02em;font-weight:800}
.section-sub{color:var(--muted);margin:0 0 18px;font-size:.95rem}
/* ---------- strategy cards ---------- */
.strategy{background:var(--card);border:1px solid var(--line);border-radius:20px;box-shadow:var(--shadow);margin:18px 0;padding:28px;overflow:hidden}
.strategy-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;flex-wrap:wrap}
.strategy-head .eyebrow{color:var(--teal)}
h3{font-size:1.5rem;line-height:1.2;margin:0;letter-spacing:-.02em;font-weight:750}
.pill{border-radius:100px;padding:6px 14px;font-size:.8rem;font-weight:700;white-space:nowrap;background:#eef2f6;color:var(--ink2);border:1px solid var(--line)}
.pill-live{background:var(--pos-bg);color:var(--pos);border-color:#bfe6d2}
.pill-frozen{background:var(--amber-bg);color:var(--amber);border-color:#ecd9a8}
.pill-kill{background:var(--neg-bg);color:var(--neg);border-color:#f0b9b0}
.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-top:26px}
.metrics small{display:block;font-size:.74rem;color:var(--muted);font-weight:700;letter-spacing:.05em;text-transform:uppercase}
.metrics strong{display:block;font-size:clamp(1.5rem,3vw,2.1rem);color:var(--ink);line-height:1.2;margin-top:6px;font-variant-numeric:tabular-nums;letter-spacing:-.02em;font-weight:750}
.metrics .delta{font-size:.85rem;color:var(--muted);font-variant-numeric:tabular-nums}
.pos{color:var(--pos);font-weight:700}.neg{color:var(--neg);font-weight:700}
.window{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px 20px;margin-top:22px;padding:20px 0;border-top:1px solid var(--line)}
.window small{display:block;font-size:.74rem;color:var(--muted);font-weight:600}
.window b{font-size:1.02rem;font-variant-numeric:tabular-nums;font-weight:650}
.chart{margin-top:20px;background:linear-gradient(#fbfcfd,#f5f8fa);border:1px solid var(--line);border-radius:14px;padding:14px 10px 6px;min-width:0}
svg{display:block;width:100%;height:auto;max-height:260px}
svg .axis{font-size:12px;fill:var(--faint);font-variant-numeric:tabular-nums}
.gridline{stroke:var(--line);stroke-width:1}
.line{fill:none;stroke:var(--teal);stroke-width:3;stroke-linejoin:round;stroke-linecap:round}
.dot{fill:var(--teal);stroke:#fff;stroke-width:2}
.positions,.shadow{margin-top:14px;font-size:.9rem;color:var(--ink2);overflow-wrap:anywhere}
.shadow{background:#f4faf9;border:1px dashed #bfe0dc;border-radius:10px;padding:10px 14px}
.empty{color:var(--faint);font-style:italic}
/* ---------- tables ---------- */
.panel{background:var(--card);border:1px solid var(--line);border-radius:20px;box-shadow:var(--shadow);padding:8px 8px;margin:14px 0;overflow:hidden}
.table-scroll{overflow-x:auto;border-radius:12px}
table{border-collapse:collapse;width:100%;min-width:760px;text-align:left;font-size:.88rem}
th,td{padding:12px 14px;border-bottom:1px solid var(--line);vertical-align:middle;overflow-wrap:anywhere}
th{color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;font-weight:700;background:#f8fafc;position:sticky;top:0}
tbody tr:nth-child(even){background:#fafbfc}
tbody tr:hover{background:#f1f7f6}
tr:last-child td{border-bottom:0}
td{font-variant-numeric:tabular-nums}
.side{border-radius:100px;padding:3px 11px;font-size:.78rem;font-weight:700}
.side-buy{background:var(--pos-bg);color:var(--pos)}
.side-sell{background:var(--neg-bg);color:var(--neg)}
/* ---------- alerts & footer ---------- */
.not-started{background:var(--card);border:1px dashed #b9c7d4;border-radius:20px;padding:34px;text-align:center;color:var(--muted)}
.warning{padding:18px 22px;background:#fff4ec;border:1px solid #eec39a;border-left:5px solid var(--amber);border-radius:12px;color:#6e4413;margin:22px 0;box-shadow:var(--shadow)}
.foot{border-top:1px solid var(--line);padding-top:22px;color:var(--muted);font-size:.85rem;margin-top:40px}
@media(max-width:700px){
  main{padding:20px 14px 60px}header{padding:36px 20px 40px}.strategy{padding:20px}
  .metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.window{grid-template-columns:repeat(2,minmax(0,1fr))}
}
"""


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
    warning = ('<div class="warning" role="alert"><b>Ledger integrity warning.</b> Stored cash or positions may not '
               'match the recorded fills. Treat performance figures as unverified until reconciliation passes.</div>'
               if fidelity_problem else "")
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
                               f'Current equity difference: {_signed(effect.get("equity_delta"), _money)}.</p>')
            points = _list(histories.get(name))
            excess = item.get("excess_return_vs_buy_and_hold_pct")
            benchmark_label = (f'{_percent(excess, signed=True).removesuffix("%")} percentage points'
                               if excess is not None else "Benchmark")
            cards.append(f'''<article class="strategy">
              <div class="strategy-head"><div><p class="eyebrow">PAPER PORTFOLIO</p><h3>{_text(NAMES[name])}</h3></div>{_state_pill(state)}</div>
              <div class="metrics">
                <div><small>Last recorded equity</small><strong>{_money(item.get("equity"))}</strong></div>
                <div><small>Cash available</small><strong>{_money(item.get("cash"))}</strong></div>
                <div><small>Since start P&amp;L</small><strong>{_signed(total.get("pnl"), _money)}</strong><span class="delta">{_signed(total.get("return_pct"), _percent)} return</span></div>
              </div>
              <div class="window">
                <div><small>Window P&amp;L</small><b>{_signed(period.get("pnl"), _money)}</b></div>
                <div><small>Window return</small><b>{_signed(period.get("return_pct"), _percent)}</b></div>
                <div><small>Trades in window</small><b>{_text(period.get("trades"))}</b></div>
                <div><small>Trading costs in window</small><b>{_money(period.get("fees"))}</b></div>
                <div><small>Max window drawdown</small><b>{_percent(period.get("max_drawdown_pct"))}</b></div>
                <div><small>Vs. buy and hold</small><b>{benchmark_label}</b></div>
              </div>
              <div class="chart">{_chart(points, counts.get(name), limits.get("points_per_sleeve"), f"eq-{name}")}</div>
              <p class="positions"><b>Open positions:</b> {held_text}</p>{shadow_html}
            </article>''')
    if not cards:
        cards.append('<div class="not-started"><h3>No paper results yet</h3><p>Run the paper bot to record its first simulated cycle. Performance, positions, and equity history will appear here after observations exist.</p></div>')
    fill_rows = []
    for fill in _list(data.get("recent_fills")):
        if isinstance(fill, dict):
            cells = (f"<td>{_text(_time(fill.get('ts')))}</td>"
                     f"<td>{_text(NAMES.get(str(fill.get('sleeve')), fill.get('sleeve')))}</td>"
                     f"<td>{_side_pill(fill.get('side'))}</td>"
                     f"<td>{_text(fill.get('symbol'))}</td>"
                     f"<td>{_text(fill.get('qty'))}</td>"
                     f"<td>{_money(fill.get('fill_price'))}</td>"
                     f"<td>{_money(fill.get('cost'))}</td>"
                     f"<td>{_text(fill.get('reason'))}</td>")
            fill_rows.append(f"<tr>{cells}</tr>")
    activity_rows = []
    for event in _list(data.get("activity")):
        if isinstance(event, dict):
            kind = str(event.get("kind") or "Event").replace("_", " ").capitalize()
            activity_rows.append(_row(_time(event.get("ts")), kind, NAMES.get(str(event.get("sleeve")), event.get("sleeve")), event.get("summary")))
    quote_age = health.get("data_age_seconds")
    age_text = f"{_text(quote_age)} seconds at snapshot" if quote_age is not None else "No quote age available"
    status_class = status if status in ("critical", "degraded") else ""
    html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper Trading Dashboard</title><style>{CSS}</style></head><body>
<header><p class="eyebrow">SIMULATION &middot; NO REAL ORDERS</p><h1>Paper trading dashboard</h1>
<p class="subtitle">A read-only snapshot of three virtual BTC and ETH portfolios. Market quotes come from public Coinbase data. Fills and P&amp;L are simulated after estimated costs. Past results do not predict future returns.</p>
<div class="badges"><span class="badge">PAPER ONLY</span><span class="badge {status_class}">Health: {_text(status.replace("_", " "))}</span></div></header>
<main>{warning}
<section aria-label="Snapshot details"><div class="summary">
<div class="tile"><small>Snapshot generated</small><b>{_text(_time(data.get("generated_at")))}</b></div>
<div class="tile"><small>Reporting window</small><b>{_text(window)} &middot; {_text(_time(data.get("from", report.get("from"))))} to {_text(_time(data.get("to", report.get("to"))))}</b></div>
<div class="tile"><small>Last successful cycle</small><b>{_text(_time(health.get("last_successful_action_at")))}</b></div>
<div class="tile"><small>Quote age</small><b>{age_text}</b></div>
</div>
<p class="hint">This file does not update itself. Generate a new snapshot for current health and results. Health: {_text(reasons)}.</p>
<p class="hint">Window P&amp;L uses the latest recorded equity before the window as its baseline when available. If no earlier observation exists, it uses starting cash. Recent fills and audit activity show at most {_text(limits.get("fills", 50))} and {_text(limits.get("activity", 50))} rows respectively.</p></section>
<section aria-label="Paper portfolios"><h2>Strategy results</h2><p class="section-sub">One virtual $1,000 portfolio per strategy, simulated after trading costs.</p>{''.join(cards)}</section>
<section aria-label="Recent paper fills"><h2>Recent simulated fills</h2><p class="section-sub">Every simulated order, exactly as recorded in the ledger.</p><div class="panel">{_table(("Time", "Portfolio", "Side", "Market", "Quantity", "Fill price", "Trading cost", "Reason"), fill_rows, "No simulated fills in this snapshot.")}</div></section>
<section aria-label="Recent activity"><h2>Recent audit activity</h2><p class="section-sub">Decisions, risk checks, and system events from the hash-chained audit log.</p><div class="panel">{_table(("Time", "Event", "Portfolio", "Detail"), activity_rows, "No audit activity in this snapshot.")}</div></section>
<p class="foot">Paper simulation only. Per-strategy portfolios each start with {_money(report.get("starting_cash_per_sleeve"))} in virtual cash. Costs and metrics are drawn from the saved report; this page sends no requests and contains no live trading controls.</p>
</main></body></html>'''
    return html
