from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from rhbot.config import Settings
from rhbot.engine import Engine
from rhbot.models import Bar, MarketSnapshot, Quote

UTC = timezone.utc


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 3, 16, 15, 0, tzinfo=UTC)


def make_settings(path, **overrides) -> Settings:
    return Settings(state_dir=path, **overrides)


def make_quote(symbol: str, mid: str, ts: datetime, **kwargs) -> Quote:
    return Quote(symbol=symbol, ts=ts, mid=Decimal(mid), source="test", **kwargs)


def make_bars(symbol: str, closes: list[str], last_open: datetime) -> list[Bar]:
    start = last_open - timedelta(days=len(closes) - 1)
    bars: list[Bar] = []
    for index, close in enumerate(closes):
        price = Decimal(close)
        bars.append(
            Bar(
                symbol=symbol,
                ts=start + timedelta(days=index),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal("1"),
                source="test",
            )
        )
    return bars


def snapshot(ts: datetime, mid: str = "100", closes: list[str] | None = None, last_open: datetime | None = None) -> MarketSnapshot:
    quotes = {
        "BTC-USD": make_quote("BTC-USD", mid, ts),
        "ETH-USD": make_quote("ETH-USD", mid, ts),
    }
    if closes is None:
        bars = {"BTC-USD": [], "ETH-USD": []}
    else:
        opened = last_open or (ts - timedelta(days=1))
        bars = {
            "BTC-USD": make_bars("BTC-USD", closes, opened),
            "ETH-USD": make_bars("ETH-USD", closes, opened),
        }
    return MarketSnapshot(bars=bars, quotes=quotes, source="test")


def engine(path, **overrides) -> Engine:
    return Engine(make_settings(path, **overrides))
