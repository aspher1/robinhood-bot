"""Free public BTC/ETH prices. No API key.

Coinbase is the default because its ticker includes a trade time, so the
risk engine can reject stale data. Kraken is the alternate. Fills on these
mids pay ``cost_per_side`` (default 1% per side). The venue's own bid/ask,
when present, is only a sanity check. It is not added on top of the 1%.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from rhbot.errors import DataError
from rhbot.models import Bar, Quote
from rhbot.money import api_decimal as D

KRAKEN_PAIRS = {"BTC-USD": "XBTUSD", "ETH-USD": "ETHUSD"}


def _ts_from_unix(value: object) -> datetime:
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _parse_time(value: object) -> datetime:
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return _ts_from_unix(value)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise DataError("unreadable timestamp")


def _header_time(headers: dict[str, str]) -> datetime:
    raw = headers.get("Date") or headers.get("date")
    if not raw:
        raise DataError("ticker response has no timestamp")
    parsed = parsedate_to_datetime(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_coinbase_candles(symbol: str, payload: object) -> list[Bar]:
    if not isinstance(payload, list):
        raise DataError("coinbase candles payload is not a list")
    bars: list[Bar] = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 6:
            raise DataError("coinbase candle row is short")
        bars.append(
            Bar(
                symbol=symbol,
                ts=_ts_from_unix(row[0]),
                low=D(row[1]),
                high=D(row[2]),
                open=D(row[3]),
                close=D(row[4]),
                volume=D(row[5]),
                source="coinbase",
            )
        )
    bars.sort(key=lambda bar: bar.ts)
    return bars


def parse_kraken_candles(symbol: str, payload: object) -> list[Bar]:
    if not isinstance(payload, dict):
        raise DataError("kraken candles payload is not an object")
    errors = payload.get("error") or []
    if errors:
        raise DataError(f"kraken error: {errors}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise DataError("kraken candles missing result")
    series = None
    for key, value in result.items():
        if key != "last" and isinstance(value, list):
            series = value
            break
    if series is None:
        raise DataError("kraken candles missing series")
    bars: list[Bar] = []
    for row in series:
        if not isinstance(row, list) or len(row) < 7:
            raise DataError("kraken candle row is short")
        bars.append(
            Bar(
                symbol=symbol,
                ts=_ts_from_unix(row[0]),
                open=D(row[1]),
                high=D(row[2]),
                low=D(row[3]),
                close=D(row[4]),
                volume=D(row[6]),
                source="kraken",
            )
        )
    bars.sort(key=lambda bar: bar.ts)
    return bars


def parse_coinbase_ticker(symbol: str, payload: object) -> Quote:
    if not isinstance(payload, dict) or "price" not in payload or "time" not in payload:
        raise DataError("coinbase ticker missing price or time")
    bid = D(payload["bid"]) if payload.get("bid") not in (None, "") else None
    ask = D(payload["ask"]) if payload.get("ask") not in (None, "") else None
    return Quote(
        symbol=symbol,
        ts=_parse_time(payload["time"]),
        mid=D(payload["price"]),
        source="coinbase",
        bid=bid,
        ask=ask,
        spread_included=False,
    )


def parse_kraken_ticker(symbol: str, payload: object, headers: dict[str, str]) -> Quote:
    if not isinstance(payload, dict):
        raise DataError("kraken ticker payload is not an object")
    errors = payload.get("error") or []
    if errors:
        raise DataError(f"kraken error: {errors}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise DataError("kraken ticker missing result")
    series = None
    for key, value in result.items():
        if isinstance(value, dict) and "c" in value:
            series = value
            break
    if series is None:
        raise DataError("kraken ticker missing last price")
    last = series["c"]
    if not isinstance(last, list) or not last:
        raise DataError("kraken ticker last price is empty")
    bid = D(series["b"][0]) if isinstance(series.get("b"), list) and series["b"] else None
    ask = D(series["a"][0]) if isinstance(series.get("a"), list) and series["a"] else None
    return Quote(
        symbol=symbol,
        ts=_header_time(headers),
        mid=D(last[0]),
        source="kraken",
        bid=bid,
        ask=ask,
        spread_included=False,
    )


class PublicMarketData:
    def __init__(self, provider: str = "coinbase", transport=None):
        if provider not in ("coinbase", "kraken"):
            raise DataError(f"unknown provider {provider}")
        self.provider = provider
        self.name = provider
        self._transport = transport

    def fetch_daily_bars(self, symbol: str) -> list[Bar]:
        if symbol not in KRAKEN_PAIRS:
            raise DataError(f"unsupported symbol {symbol}")
        if self.provider == "coinbase":
            url = (
                "https://api.exchange.coinbase.com/products/"
                f"{quote(symbol, safe='')}/candles?granularity=86400"
            )
            status, body, _headers = self._get(url)
            if status != 200:
                raise DataError(f"coinbase candles http {status}")
            return parse_coinbase_candles(symbol, body)
        pair = KRAKEN_PAIRS[symbol]
        url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440"
        status, body, _headers = self._get(url)
        if status != 200:
            raise DataError(f"kraken candles http {status}")
        return parse_kraken_candles(symbol, body)

    def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        for symbol in symbols:
            if symbol not in KRAKEN_PAIRS:
                raise DataError(f"unsupported symbol {symbol}")
            if self.provider == "coinbase":
                url = (
                    "https://api.exchange.coinbase.com/products/"
                    f"{quote(symbol, safe='')}/ticker"
                )
                status, body, _headers = self._get(url)
                if status != 200:
                    raise DataError(f"coinbase ticker http {status}")
                quotes[symbol] = parse_coinbase_ticker(symbol, body)
            else:
                pair = KRAKEN_PAIRS[symbol]
                url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
                status, body, headers = self._get(url)
                if status != 200:
                    raise DataError(f"kraken ticker http {status}")
                quotes[symbol] = parse_kraken_ticker(symbol, body, headers)
        return quotes

    def _get(self, url: str) -> tuple[int, object, dict[str, str]]:
        if self._transport is not None:
            return self._transport(url)
        from rhbot.data.http import get_json

        return get_json(url)
