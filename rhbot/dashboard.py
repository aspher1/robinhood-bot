"""Read-only paper performance page from the ledger. No quotes are fetched."""

from __future__ import annotations

import csv
import html
import io
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from rhbot.config import Settings
from rhbot.ledger import Ledger, parse_ts
from rhbot.money import D
from rhbot.status import assess, build_report

EM = "\u2014"
CENT = Decimal("0.01")
COORD = Decimal("0.01")

STALE_REASONS = frozenset({
    "heartbeat_stale",
    "no_heartbeat",
    "stale_market_data",
    "quote_hard_stop",
    "no_recent_decision",
    "loop_not_completing",
    "no_successful_action",
    "open_order_stale",
})

_INCOMPLETE_ASSESS = ("db_integrity", "heartbeat_unreadable", "kill_flatten_incomplete")

_TRADES_HEADER = [
    "ts",
    "sleeve",
    "symbol",
    "side",
    "qty",
    "fill_price_usd",
    "cash_delta_usd",
    "cost_usd",
    "notional_usd",
    "reason",
    "client_order_id",
]
_EQUITY_HEADER = ["ts", "sleeve", "equity_usd", "cash_usd", "drawdown_fraction"]


def format_ts(value: str | None) -> str:
    """Ledger timestamp as ``YYYY-MM-DD HH:MM:SS UTC``, or an em dash if missing."""
    if value is None or value == "":
        return EM
    try:
        parsed = parse_ts(value)
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")


def percent_text(value: str | None) -> str:
    """Format a value that is already in percent points. Never a dollar amount."""
    if value is None or value == "":
        return EM
    amount = D(value).quantize(CENT, rounding=ROUND_HALF_UP)
    if amount == 0:
        amount = abs(amount)
    text = format(amount, "f")
    if amount > 0:
        return f"+{text}%"
    return f"{text}%"


def usd_text(value: str | None) -> str:
    """Format a dollar amount with cents, or an em dash if missing."""
    if value is None or value == "":
        return EM
    amount = D(value).quantize(CENT, rounding=ROUND_HALF_UP)
    if amount == 0:
        amount = abs(amount)
    body = format(abs(amount), ",.2f")
    if amount < 0:
        return f"-${body}"
    return f"${body}"


def build_dashboard(settings: Settings, since_text: str = "7d", now=None) -> dict:
    """Assemble saved results, stale health, incomplete data, and the decision timeline."""
    report = build_report(settings, since_text, now=now)
    health = assess(settings, now=now)
    sleeves = report["sleeves"] if isinstance(report.get("sleeves"), dict) else {}
    db_exists = _db_file(settings).is_file()
    fills_exist = False
    snapshot_counts: dict[str, int] = {}
    timeline: list[dict] = []
    if db_exists:
        with _reader(settings) as ledger:
            if ledger is not None:
                fills_exist = bool(ledger.all_fills())
                for row in ledger.all_snapshots():
                    name = str(row["sleeve"])
                    snapshot_counts[name] = snapshot_counts.get(name, 0) + 1
                timeline = [_timeline_row(item) for item in ledger.recent_decisions(40)]
    assess_reasons = {str(item) for item in (health.get("reasons") or [])}
    stale_reasons = sorted(assess_reasons & STALE_REASONS)
    incomplete = _incomplete_reasons(assess_reasons, sleeves, snapshot_counts, db_exists)
    return {
        "recorded_only": True,
        "since": since_text,
        "saved": {
            "present": fills_exist or bool(snapshot_counts),
            "sleeves": sleeves,
            "benchmark": {
                "name": "buy_and_hold",
                "return_pct": _benchmark_return(sleeves),
                "unit": "percent",
            },
        },
        "stale": {
            "present": bool(stale_reasons),
            "reasons": stale_reasons,
        },
        "incomplete": {
            "present": bool(incomplete),
            "reasons": incomplete,
        },
        "timeline": timeline,
    }


def render_dashboard(settings: Settings, since_text: str = "7d", now=None) -> str:
    """HTML5 page for one dashboard document. Interpolated text is escaped."""
    data = build_dashboard(settings, since_text, now=now)
    saved = data["saved"]
    stale = data["stale"]
    incomplete = data["incomplete"]
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Paper performance</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        "<h1>Paper performance</h1>",
        "<p>Recorded paper data only. No prices were fetched to build this page.</p>",
        f"<p>Window {_esc(data['since'])}.</p>",
        _saved_section(settings, saved),
        _reason_section(
            "stale-health",
            "stale",
            stale["present"],
            "Stale health",
            "Health is not stale. These figures are the last saved marks.",
            stale["reasons"],
            "These figures are the last saved marks. This page did not fetch a new price.",
        ),
        _reason_section(
            "incomplete-data",
            "incomplete",
            incomplete["present"],
            "Incomplete data",
            "Recorded history is complete enough to read.",
            incomplete["reasons"],
            None,
        ),
        _timeline_section(data["timeline"]),
        "</body>",
        "</html>",
    ]
    return "\n".join(parts) + "\n"


def trades_csv(settings: Settings) -> str:
    """Fills in id order. Timestamps stay in the ledger's raw ISO form."""
    rows: list[list[str]] = []
    with _reader(settings) as ledger:
        if ledger is not None:
            for row in ledger.all_fills():
                rows.append([
                    _cell(row["ts"]),
                    _cell(row["sleeve"]),
                    _cell(row["symbol"]),
                    _cell(row["side"]),
                    _cell(row["qty"]),
                    _cell(row["fill_price"]),
                    _cell(row["cash_delta"]),
                    _cell(row["cost"]),
                    _cell(row["notional"]),
                    _cell(row["reason"]),
                    _cell(row["client_order_id"]),
                ])
    return _csv_text(_TRADES_HEADER, rows)


def equity_csv(settings: Settings) -> str:
    """Equity snapshots in id order. Drawdown stays the stored positive fraction."""
    rows: list[list[str]] = []
    with _reader(settings) as ledger:
        if ledger is not None:
            for row in ledger.all_snapshots():
                rows.append([
                    _cell(row["ts"]),
                    _cell(row["sleeve"]),
                    _cell(row["equity"]),
                    _cell(row["cash"]),
                    _cell(row["drawdown"]),
                ])
    return _csv_text(_EQUITY_HEADER, rows)


def _db_file(settings: Settings) -> Path:
    return Path(settings.state_dir) / "bot.sqlite"


@contextmanager
def _reader(settings: Settings):
    if not _db_file(settings).is_file():
        yield None
        return
    ledger = Ledger(settings, readonly=True)
    try:
        yield ledger
    finally:
        ledger.close()


def _benchmark_return(sleeves: dict) -> str | None:
    sleeve = sleeves.get("buy_and_hold")
    if not isinstance(sleeve, dict):
        return None
    since = sleeve.get("since_start")
    if not isinstance(since, dict) or since.get("return_pct") is None:
        return None
    return str(since["return_pct"])


def _incomplete_reasons(
    assess_reasons: set[str],
    sleeves: dict,
    snapshot_counts: dict[str, int],
    db_exists: bool,
) -> list[str]:
    found: list[str] = []
    if "not_started" in assess_reasons or not db_exists:
        found.append("not_started")
    for code in _INCOMPLETE_ASSESS:
        if code in assess_reasons:
            found.append(code)
    if db_exists:
        for name, body in sleeves.items():
            fidelity = body.get("fidelity") if isinstance(body, dict) else None
            ok = isinstance(fidelity, dict) and bool(fidelity.get("ok"))
            if not ok:
                found.append(f"fidelity:{name}")
            if snapshot_counts.get(str(name), 0) == 0:
                found.append(f"no_equity_snapshots:{name}")
    return sorted(found)


def _timeline_row(item: dict) -> dict:
    raw = "" if item.get("ts") is None else str(item.get("ts"))
    orders = item.get("orders")
    if not isinstance(orders, list):
        orders = []
    return {
        "ts": raw,
        "display_ts": format_ts(raw),
        "sleeve": "" if item.get("sleeve") is None else str(item.get("sleeve")),
        "reason": "" if item.get("reason") is None else str(item.get("reason")),
        "summary": _summary(orders),
    }


def _summary(orders: list) -> str:
    if not orders:
        return "no order"
    parts = [_order_summary(order) for order in orders if isinstance(order, dict)]
    if not parts:
        return "no order"
    return "; ".join(parts)


def _order_summary(order: dict) -> str:
    side = "" if order.get("side") is None else str(order.get("side"))
    symbol = "" if order.get("symbol") is None else str(order.get("symbol"))
    quote = order.get("quote_amount")
    base = order.get("base_quantity")
    if quote:
        return f"{side} {symbol} ${quote}"
    if base:
        return f"{side} {symbol} qty {base}"
    return f"{side} {symbol}"


def _saved_section(settings: Settings, saved: dict) -> str:
    present = bool(saved["present"])
    if present:
        body = "\n".join([
            _sleeve_table(saved["sleeves"]),
            _charts_html(settings, saved["sleeves"]),
            _benchmark_html(saved["benchmark"]),
        ])
    else:
        body = "\n".join([
            "<p>No saved equity or trades are in this state directory.</p>",
            "<p>Not enough saved points to chart.</p>",
            _benchmark_html(saved["benchmark"]),
        ])
    return _section("saved-results", "saved", present, "Saved results", body)


def _sleeve_table(sleeves: dict) -> str:
    rows = []
    for name, body in sleeves.items():
        since = body.get("since_start") if isinstance(body, dict) else None
        window = body.get("window") if isinstance(body, dict) else None
        since = since if isinstance(since, dict) else {}
        window = window if isinstance(window, dict) else {}
        record = body if isinstance(body, dict) else {}
        excess = record.get("excess_return_vs_buy_and_hold_pct")
        trades = window.get("trades")
        trade_text = "" if trades is None else str(trades)
        rows.append(
            "<tr>"
            f'<td data-label="Sleeve">{_esc(name)}</td>'
            f"{_money_cell(usd_text(_as_text(record.get('equity'))), record.get('equity'), 'Equity')}"
            f"{_money_cell(usd_text(_as_text(since.get('pnl'))), since.get('pnl'), 'P&L')}"
            f"{_money_cell(percent_text(_as_text(since.get('return_pct'))), since.get('return_pct'), 'Return')}"
            f"{_money_cell(usd_text(_as_text(window.get('pnl'))), window.get('pnl'), 'Window P&L')}"
            f"{_money_cell(percent_text(_as_text(window.get('return_pct'))), window.get('return_pct'), 'Window return')}"
            f"{_money_cell('' if excess in (None, '') else percent_text(_as_text(excess)), excess, 'vs hold')}"
            f'<td data-label="Trades">{_esc(trade_text)}</td>'
            "</tr>"
        )
    body = "\n".join(rows)
    return (
        '<div class="table-wrap">\n'
        "<table>\n"
        "<thead><tr>"
        "<th>Sleeve</th>"
        "<th>Equity</th>"
        "<th>P&amp;L</th>"
        "<th>Return</th>"
        "<th>Window P&amp;L</th>"
        "<th>Window return</th>"
        "<th>vs hold</th>"
        "<th>Trades</th>"
        "</tr></thead>\n"
        f"<tbody>\n{body}\n</tbody>\n"
        "</table>\n"
        '<p class="scale">P&amp;L is dollars. Return and vs hold are percent.</p>\n'
        "</div>"
    )


def _charts_html(settings: Settings, sleeves: dict) -> str:
    series = _equity_series(settings)
    names = [str(name) for name in sleeves]
    for name in series:
        if name not in sleeves:
            names.append(name)
    if not names:
        return "<p>Not enough saved points to chart.</p>"
    blocks = []
    for name in names:
        points = series.get(name, [])
        if len(points) < 2:
            drawn = "<p>Not enough saved points to chart.</p>"
        else:
            drawn = _sparkline(points, name)
        blocks.append(f'<div class="chart"><h3>{_esc(name)}</h3>{drawn}</div>')
    return "\n".join(blocks)


def _equity_series(settings: Settings) -> dict[str, list[str]]:
    series: dict[str, list[str]] = {}
    with _reader(settings) as ledger:
        if ledger is None:
            return series
        for row in ledger.all_snapshots():
            series.setdefault(str(row["sleeve"]), []).append(str(row["equity"]))
    return series


def _sparkline(equities: list[str], sleeve: str) -> str:
    points = [D(item) for item in equities]
    width = Decimal(280)
    height = Decimal(64)
    pad = Decimal(4)
    low = min(points)
    high = max(points)
    span = high - low
    last = Decimal(len(points) - 1)
    inner_w = width - pad * 2
    inner_h = height - pad * 2
    coords: list[str] = []
    for index, value in enumerate(points):
        x = pad + inner_w * Decimal(index) / last
        if span == 0:
            y = pad + inner_h / 2
        else:
            y = pad + inner_h * (high - value) / span
        coords.append(f"{_coord(x)},{_coord(y)}")
    scale = f"{usd_text(format(low, 'f'))} to {usd_text(format(high, 'f'))}"
    points_attr = _esc(" ".join(coords))
    return (
        f'<svg viewBox="0 0 280 64" width="280" height="64" role="img" aria-label="{_esc(sleeve)} saved equity">'
        '<rect width="280" height="64" fill="#1a222c"></rect>'
        f'<polyline fill="none" stroke="#9bd7ff" stroke-width="2" points="{points_attr}"></polyline>'
        "</svg>"
        f'<p class="scale">{_esc(scale)}</p>'
    )


def _coord(value: Decimal) -> str:
    text = format(value.quantize(COORD, rounding=ROUND_HALF_UP), "f")
    if text == "-0.00":
        return "0.00"
    return text


def _benchmark_html(benchmark: dict) -> str:
    raw = benchmark.get("return_pct")
    data_value = "" if raw is None else str(raw)
    visible = percent_text(None if raw is None else data_value)
    return (
        '<p class="benchmark-label">Buy and hold since start</p>\n'
        f'<p id="benchmark-return" data-unit="percent" data-value="{_esc(data_value)}">{_esc(visible)}</p>\n'
        '<p id="benchmark-unit">percent</p>'
    )


def _reason_section(
    section_id: str,
    kind: str,
    present: bool,
    title: str,
    clear_text: str,
    reasons: list[str],
    extra: str | None,
) -> str:
    if present:
        items = "\n".join(f"<li><code>{_esc(reason)}</code></li>" for reason in reasons)
        extra_html = f"\n<p>{_esc(extra)}</p>" if extra else ""
        body = f"<ul>\n{items}\n</ul>{extra_html}"
    else:
        body = f"<p>{_esc(clear_text)}</p>"
    return _section(section_id, kind, present, title, body)


def _timeline_section(items: list[dict]) -> str:
    rows = []
    for item in items:
        rows.append(
            "<li>"
            f'<time datetime="{_esc(item["ts"])}">{_esc(item["display_ts"])}</time> '
            f'<span class="sleeve">{_esc(item["sleeve"])}</span> '
            f'<span class="reason">{_esc(item["reason"])}</span> '
            f'<span class="summary">{_esc(item["summary"])}</span>'
            "</li>"
        )
    listing = "\n".join(rows)
    body = f"<ol>\n{listing}\n</ol>" if rows else "<p>No decisions are saved.</p>"
    return (
        '<section id="decision-timeline">\n'
        "<h2>Decisions</h2>\n"
        f"{body}\n"
        "</section>"
    )


def _section(section_id: str, kind: str, present: bool, title: str, body: str) -> str:
    flag = "yes" if present else "no"
    return (
        f'<section id="{section_id}" data-kind="{kind}" data-present="{flag}">\n'
        f"<h2>{title}</h2>\n"
        f"{body}\n"
        "</section>"
    )


def _money_cell(text: str, recorded: object = None, label: str = "") -> str:
    kind = ""
    if text.startswith("+"):
        kind = ' class="up"'
    elif text.startswith("-"):
        kind = ' class="down"'
    recorded_attr = ""
    if recorded not in (None, ""):
        recorded_attr = f' data-recorded="{_esc(recorded)}"'
    return f'<td{kind}{recorded_attr} data-label="{_esc(label)}">{_esc(text)}</td>'


def _as_text(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _cell(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _csv_text(header: list[str], rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


_CSS = """
html, body {
  margin: 0;
  background: #101418;
  color: #eef2f6;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  line-height: 1.45;
}
body { padding: 1.5rem; max-width: 64rem; }
h1, h2, h3 { font-weight: 600; line-height: 1.25; }
h1 { font-size: 1.75rem; margin: 0 0 0.75rem; }
h2 { font-size: 1.25rem; margin: 0 0 0.75rem; }
h3 { font-size: 1rem; margin: 0 0 0.35rem; }
p, ul, ol { margin: 0.4rem 0 0.8rem; }
a { color: #9bd7ff; }
section {
  margin: 1.25rem 0;
  padding: 1rem 1.1rem;
  background: #171e26;
  border: 1px solid #33404d;
  border-left-width: 4px;
  border-radius: 0.5rem;
}
section[data-present="no"] { color: #b7c2ce; }
section[data-kind="saved"][data-present="yes"] { border-left-color: #3f8f62; }
section[data-kind="stale"][data-present="yes"] { border-left-color: #d1a441; }
section[data-kind="incomplete"][data-present="yes"] { border-left-color: #d16b6b; }
.benchmark-label { margin-bottom: 0.15rem; color: #d5dde6; }
#benchmark-return { font-size: 1.75rem; font-weight: 600; margin: 0.1rem 0; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
@media (max-width: 720px) {
  .table-wrap thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
  .table-wrap tr { display: block; margin: 0 0 0.75rem; border: 1px solid #3d4a58; }
  .table-wrap td { display: flex; justify-content: space-between; gap: 1rem; border: none; }
  .table-wrap td::before { content: attr(data-label); color: #d5dde6; }
}
th, td {
  border: 1px solid #3d4a58;
  padding: 0.45rem 0.6rem;
  text-align: left;
  vertical-align: top;
}
th { background: #243140; color: #f7fafc; }
td.up { color: #b6f3c0; }
td.down { color: #ffc7c7; }
code, .reason {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.chart { margin: 0.8rem 0 1rem; }
.scale { color: #d5dde6; }
svg { display: block; max-width: 100%; height: auto; }
time { color: #d5dde6; }
""".strip()
