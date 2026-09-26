"""Bounded, read-only observations for the local paper dashboard."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from rhbot.config import Settings
from rhbot.ledger import Ledger
from rhbot.ops import iso, utcnow
from rhbot.status import assess, build_report, parse_since

ACTIVITY_KINDS = (
    "decision",
    "risk_denial",
    "freeze_trip",
    "freeze_ack",
    "kill_trip",
    "restart_baseline",
    "kill_flatten_incomplete",
)


def _sample_indexes(count: int, capacity: int) -> set[int]:
    if count <= capacity:
        return set(range(count))
    return {index * (count - 1) // (capacity - 1) for index in range(capacity)}


def _history(ledger: Ledger, sleeve: str, start: str, end: str, max_points: int) -> tuple[list[dict], int]:
    baseline = ledger.conn.execute(
        """SELECT ts, equity, cash FROM equity_snapshots
        WHERE sleeve=? AND ts<? ORDER BY ts DESC, id DESC LIMIT 1""",
        (sleeve, start),
    ).fetchone()
    row = ledger.conn.execute(
        """SELECT COUNT(*) AS n FROM equity_snapshots
        WHERE sleeve=? AND ts>=? AND ts<=?""",
        (sleeve, start, end),
    ).fetchone()
    in_window_count = int(row["n"])
    count = in_window_count + int(baseline is not None)
    keep = _sample_indexes(count, max_points)
    points = []
    if baseline is not None and 0 in keep:
        points.append({key: str(baseline[key]) for key in ("ts", "equity", "cash")})
    offset = int(baseline is not None)
    for index, snapshot in enumerate(
        ledger.conn.execute(
            """SELECT ts, equity, cash FROM equity_snapshots
            WHERE sleeve=? AND ts>=? AND ts<=? ORDER BY ts, id""",
            (sleeve, start, end),
        ),
        start=offset,
    ):
        if index in keep:
            points.append({key: str(snapshot[key]) for key in ("ts", "equity", "cash")})
    return points, count


def _reason_label(reason: str) -> str:
    labels = {
        "holding": "Keeping existing positions", "hold": "No position change",
        "already_scheduled": "This week's purchase was already handled",
        "already_decided_today": "Today's decision was already made",
        "waiting_next_day": "Waiting until the next UTC day",
        "insufficient_cash": "Not enough virtual cash",
        "below_min": "Order size below the minimum",
        "insufficient_history": "Waiting for enough daily history",
        "stale_candles": "Latest closed daily candle is missing",
        "stale_quote": "Quote too old", "missing_quote": "Quote unavailable",
        "min_hold": "Minimum holding period still applies",
        "deploy": "Initial portfolio purchase", "dca_buy": "Scheduled weekly purchase",
        "enter": "Buy signal", "exit": "Sell signal", "no_trade": "No trade proposed",
        "freeze": "New buys paused by drawdown", "killed": "Book stopped by drawdown",
    }
    parts = []
    for part in reason.split(";"):
        symbol, sep, detail = part.partition(":")
        if sep:
            parts.append(f"{symbol}: {labels.get(detail, detail.replace('_', ' '))}")
        else:
            parts.append(labels.get(part, part.replace("_", " ")))
    return "; ".join(parts)


def _activity_summary(kind: str, payload: dict) -> str:
    if kind == "decision":
        reason = str(payload.get("reason") or "decision")
        orders = payload.get("orders")
        count = len(orders) if isinstance(orders, list) else 0
        unit = "order" if count == 1 else "orders"
        return f"{_reason_label(reason)} ({count} proposed {unit})"
    if kind == "risk_denial":
        return _reason_label(str(payload.get("reason") or "risk denial"))
    if kind == "freeze_trip":
        return "Drawdown freeze triggered"
    if kind == "freeze_ack":
        return "Drawdown freeze acknowledged"
    if kind == "kill_trip":
        return "Drawdown kill triggered"
    if kind == "restart_baseline":
        return "Restart baseline recorded"
    return "Kill flatten incomplete"


def build_dashboard(
    settings: Settings,
    since_text: str = "7d",
    now: datetime | None = None,
    *,
    limit: int = 50,
    max_points: int = 360,
) -> dict:
    """Return recorded paper observations without changing state or fetching prices.

    Report and health describe the latest saved state. Windowed rows come from
    separate read-only queries and are not an atomic cross-query snapshot.
    """
    window = parse_since(since_text)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer from 1 to 50")
    if type(max_points) is not int or not 2 <= max_points <= 360:
        raise ValueError("max_points must be an integer from 2 to 360")
    now = now or utcnow()
    end = iso(now)
    start = iso(now - window)
    report = build_report(settings, since_text, now)
    health = assess(settings, now)
    started = Path(settings.state_dir, "bot.sqlite").is_file()
    history: dict[str, list[dict]] = {}
    history_counts: dict[str, int] = {}
    recent_fills: list[dict] = []
    activity: list[dict] = []
    if started:
        ledger = Ledger(settings, readonly=True)
        try:
            for sleeve in ledger.sleeve_names():
                history[sleeve], history_counts[sleeve] = _history(
                    ledger, sleeve, start, end, max_points
                )
            recent_fills = [
                {key: row[key] for key in ("id", "sleeve", "symbol", "side", "qty", "fill_price", "cost", "ts", "reason")}
                for row in ledger.conn.execute(
                    """SELECT id, sleeve, symbol, side, qty, fill_price, cost, ts, reason
                    FROM fills WHERE ts>=? AND ts<=? ORDER BY ts DESC, id DESC LIMIT ?""",
                    (start, end, limit),
                )
            ]
            placeholders = ",".join("?" for _ in ACTIVITY_KINDS)
            for row in ledger.conn.execute(
                f"""SELECT seq, ts, kind, payload FROM events
                WHERE ts>=? AND ts<=? AND kind IN ({placeholders})
                ORDER BY ts DESC, seq DESC LIMIT ?""",
                (start, end, *ACTIVITY_KINDS, limit),
            ):
                payload = json.loads(row["payload"])
                activity.append(
                    {
                        "seq": int(row["seq"]),
                        "ts": str(row["ts"]),
                        "kind": str(row["kind"]),
                        "sleeve": str(payload.get("sleeve") or ""),
                        "symbol": str(payload.get("symbol") or ""),
                        "summary": _activity_summary(str(row["kind"]), payload),
                    }
                )
        finally:
            ledger.close()
    return {
        "version": 1,
        "generated_at": end,
        "since": since_text,
        "from": start,
        "to": end,
        "started": started,
        "summary_basis": "latest_recorded",
        "report": report,
        "health": health,
        "history": history,
        "history_counts": history_counts,
        "recent_fills": recent_fills,
        "activity": activity,
        "limits": {"activity": limit, "fills": limit, "points_per_sleeve": max_points},
    }
