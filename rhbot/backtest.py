"""Replay daily bars through the same engine the live loop uses.

Replay never writes the live state directory. It uses a temporary directory
and records meta mode=replay.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

from rhbot.config import Settings, frozen_params_hash
from rhbot.engine import Engine, ensure_utc
from rhbot.errors import ConfigError
from rhbot.ledger import Ledger, parse_ts
from rhbot.models import Bar, MarketSnapshot, Quote
from rhbot.status import parse_since


def _service_dirs() -> set[Path]:
    found = {Path("state").resolve()}
    env = os.environ.get("RHBOT_STATE_DIR")
    if env:
        found.add(Path(env).resolve())
    return found


def assert_replay_safe(settings: Settings) -> None:
    requested = Path(settings.state_dir).resolve()
    db = requested / "bot.sqlite"
    if db.exists() and db.stat().st_size > 0:
        raise ConfigError("replay will not write into a state dir that already has a ledger")
    if requested in _service_dirs():
        raise ConfigError("replay will not use the service state dir")


def replay(
    settings: Settings,
    bars_by_symbol: dict[str, list[Bar]],
    *,
    paper_day1: str | None = None,
) -> Engine:
    """Run one cycle per day in a fresh temporary state dir.

    Candles before paper day 1 are indicator warmup only. The caller's dir
    is not written. ``paper_day1`` is the live meta value so DCA indexes match.
    """
    assert_replay_safe(settings)
    scratch = Path(tempfile.mkdtemp(prefix="rhbot-replay-"))
    isolated = settings.model_copy(update={"state_dir": scratch})
    engine = Engine(isolated)
    engine.ledger.set_meta("mode", "replay")
    if paper_day1:
        engine.ledger.set_meta("paper_day1", paper_day1)
        engine.ledger.set_meta("frozen_params_hash", frozen_params_hash())
        start = ensure_utc(parse_ts(paper_day1))
        times = _days_from(start, bars_by_symbol)
    else:
        stamps = sorted({bar.ts for bars in bars_by_symbol.values() for bar in bars})
        times = [ensure_utc(ts) + timedelta(days=1) for ts in stamps]
    for market_now in times:
        quotes: dict[str, Quote] = {}
        window: dict[str, list[Bar]] = {}
        cutoff = market_now - timedelta(days=1)
        for symbol in settings.symbols:
            history = [bar for bar in bars_by_symbol.get(symbol, []) if ensure_utc(bar.ts) <= market_now]
            window[symbol] = history
            closed = [bar for bar in history if ensure_utc(bar.ts) <= cutoff]
            if not closed:
                continue
            close = closed[-1].close
            quotes[symbol] = Quote(
                symbol=symbol,
                ts=market_now,
                mid=close,
                bid=close,
                ask=close,
                source="backtest",
            )
        if len(quotes) != len(tuple(settings.symbols)):
            continue
        engine.run_once(
            now=market_now,
            snapshot=MarketSnapshot(bars=window, quotes=quotes, source="backtest"),
        )
    return engine


def _days_from(start, bars_by_symbol: dict[str, list[Bar]]) -> list:
    """Daily clocks from paper day 1 through the day after the last candle."""
    stamps = [ensure_utc(bar.ts) for bars in bars_by_symbol.values() for bar in bars]
    if not stamps:
        return []
    last_date = (max(stamps) + timedelta(days=1)).date()
    times = []
    day = 0
    while True:
        market_now = start + timedelta(days=day)
        if market_now.date() > last_date:
            break
        times.append(market_now)
        day += 1
        if day > 20000:
            break
    return times


def diff_live(settings: Settings, since: str) -> dict:
    """Replay stored candles into a temp dir and diff decisions and fills."""
    window = parse_since(since)
    live = Ledger(settings)
    try:
        bars = {symbol: live.load_candles_any(symbol) for symbol in settings.symbols}
        paper_day1 = live.get_meta("paper_day1")
        live_decisions = _keyed(live, "decision")
        live_fills = _fill_keys(live)
        event_count = live.event_count()
    finally:
        live.close()
    if not any(bars.values()):
        return {"ok": False, "mismatches": ["no stored candles"], "mode": "replay"}
    # replay() refuses a directory that already has a ledger. Give it an empty one.
    isolated = settings.model_copy(update={"state_dir": Path(tempfile.mkdtemp(prefix="rhbot-diff-"))})
    engine = replay(isolated, bars, paper_day1=paper_day1)
    try:
        replay_decisions = _keyed(engine.ledger, "decision")
        replay_fills = _fill_keys(engine.ledger)
        mode = engine.ledger.get_meta("mode")
    finally:
        engine.ledger.close()
    start = _since_day(window, live_decisions, live_fills)
    live_decisions = _clip(live_decisions, start)
    replay_decisions = _clip(replay_decisions, start)
    live_fills = _clip(live_fills, start)
    replay_fills = _clip(replay_fills, start)
    mismatches = []
    for key, live_reason in live_decisions.items():
        other = replay_decisions.get(key)
        if other != live_reason:
            mismatches.append(f"decision {key}: live={live_reason} replay={other}")
    for key, replay_reason in replay_decisions.items():
        if key not in live_decisions:
            mismatches.append(f"decision {key}: missing live, replay={replay_reason}")
    for key in sorted(set(live_fills) | set(replay_fills)):
        if live_fills.get(key) != replay_fills.get(key):
            mismatches.append(f"fill {key}: live={live_fills.get(key)} replay={replay_fills.get(key)}")
    return {
        "ok": not mismatches,
        "mismatches": mismatches,
        "mode": mode,
        "live_events_before": event_count,
    }


def _since_day(window: timedelta, *books: dict[str, str]) -> str | None:
    days = [key[:10] for book in books for key in book if len(key) >= 10]
    if not days:
        return None
    end = date.fromisoformat(max(days))
    return (end - window).isoformat()


def _clip(found: dict[str, str], start: str | None) -> dict[str, str]:
    if start is None:
        return found
    return {key: value for key, value in found.items() if key[:10] >= start}


def _keyed(ledger: Ledger, kind: str) -> dict[str, str]:
    """First decision of the day, or the first one that carried an order."""
    import json

    first: dict[str, str] = {}
    with_order: dict[str, str] = {}
    rows = ledger.conn.execute(
        "SELECT payload FROM events WHERE kind=? ORDER BY seq",
        (kind,),
    ).fetchall()
    for row in rows:
        payload = json.loads(row["payload"])
        day = str(payload.get("ts", ""))[:10]
        sleeve = str(payload.get("sleeve", ""))
        if not day or not sleeve:
            continue
        key = f"{day}:{sleeve}"
        reason = str(payload.get("reason", ""))
        if key not in first:
            first[key] = reason
        if payload.get("orders") and key not in with_order:
            with_order[key] = reason
    return {key: with_order.get(key, reason) for key, reason in first.items()}


def _fill_keys(ledger: Ledger) -> dict[str, str]:
    rows = ledger.conn.execute(
        "SELECT sleeve, symbol, side, ts, client_order_id FROM fills ORDER BY id"
    ).fetchall()
    found = {}
    for row in rows:
        day = str(row["ts"])[:10]
        key = f"{day}:{row['sleeve']}:{row['symbol']}:{row['side']}"
        found[key] = str(row["client_order_id"])
    return found
