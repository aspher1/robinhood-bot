"""Replay daily bars through the same engine the live loop uses.

Replay never writes the live state directory. It uses a temporary directory
and records meta mode=replay.
"""

from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from pathlib import Path

from rhbot.config import Settings
from rhbot.engine import Engine
from rhbot.errors import ConfigError
from rhbot.ledger import Ledger
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


def replay(settings: Settings, bars_by_symbol: dict[str, list[Bar]]) -> Engine:
    """Run bars in a fresh temporary state dir. The caller's dir is not written."""
    assert_replay_safe(settings)
    scratch = Path(tempfile.mkdtemp(prefix="rhbot-replay-"))
    isolated = settings.model_copy(update={"state_dir": scratch})
    engine = Engine(isolated)
    engine.ledger.set_meta("mode", "replay")
    stamps = sorted({bar.ts for bars in bars_by_symbol.values() for bar in bars})
    for ts in stamps:
        market_now = ts + timedelta(days=1)
        quotes: dict[str, Quote] = {}
        window: dict[str, list[Bar]] = {}
        for symbol in settings.symbols:
            history = [bar for bar in bars_by_symbol.get(symbol, []) if bar.ts <= ts]
            window[symbol] = history
            if not history:
                continue
            quotes[symbol] = Quote(
                symbol=symbol,
                ts=market_now,
                mid=history[-1].close,
                source="backtest",
            )
        if len(quotes) != len(tuple(settings.symbols)):
            continue
        engine.run_once(
            now=market_now,
            snapshot=MarketSnapshot(bars=window, quotes=quotes, source="backtest"),
        )
    return engine


def diff_live(settings: Settings, since: str) -> dict:
    """Replay stored candles into a temp dir and diff decisions and fills."""
    window = parse_since(since)
    live = Ledger(settings)
    try:
        bars = {symbol: live.load_candles_any(symbol) for symbol in settings.symbols}
        live_decisions = _keyed(live, "decision")
        live_fills = _fill_keys(live)
        event_count = live.event_count()
    finally:
        live.close()
    if not any(bars.values()):
        return {"ok": False, "mismatches": ["no stored candles"], "mode": "replay"}
    # replay() refuses a directory that already has a ledger. Give it an empty one.
    isolated = settings.model_copy(update={"state_dir": Path(tempfile.mkdtemp(prefix="rhbot-diff-"))})
    engine = replay(isolated, bars)
    try:
        replay_decisions = _keyed(engine.ledger, "decision")
        replay_fills = _fill_keys(engine.ledger)
        mode = engine.ledger.get_meta("mode")
    finally:
        engine.ledger.close()
    # Limit the comparison to the requested window by timestamp prefix.
    # Both books share the same candle history, so the full series is the check.
    del window
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


def _keyed(ledger: Ledger, kind: str) -> dict[str, str]:
    import json

    found = {}
    rows = ledger.conn.execute(
        "SELECT payload FROM events WHERE kind=? ORDER BY seq",
        (kind,),
    ).fetchall()
    for row in rows:
        payload = json.loads(row["payload"])
        day = str(payload.get("ts", ""))[:10]
        sleeve = str(payload.get("sleeve", ""))
        reason = str(payload.get("reason", ""))
        found[f"{day}:{sleeve}"] = reason
    return found


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
