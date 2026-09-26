"""Replay daily bars through the same engine the live loop uses.

This is an offline helper. It does not fetch data and it does not tune parameters.
"""

from __future__ import annotations

from datetime import timedelta

from rhbot.config import Settings
from rhbot.engine import Engine
from rhbot.models import Bar, MarketSnapshot, Quote


def replay(settings: Settings, bars_by_symbol: dict[str, list[Bar]]) -> Engine:
    engine = Engine(settings)
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
