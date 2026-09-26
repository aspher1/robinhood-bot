"""Optional read-only Robinhood quote adapter.

Not used unless ``market_data`` is ``robinhood`` and both environment
variables are set:

- ``RH_API_KEY``
- ``RH_PRIVATE_KEY_BASE64`` (base64 of the 32-byte Ed25519 seed)

The variables are read from the environment only. Nothing in this repo
stores them. Leave them unset and the bot uses public prices.

This client signs GET requests for best bid/ask only, at most 100 times
per minute. A key that was created with trade permission still cannot
trade through this code: non-GET methods are refused, and any other path
is refused. Create the key as read-only anyway.

When a quote is used, buys fill at ``ask_inclusive_of_buy_spread`` and
sells at ``bid_inclusive_of_sell_spread``. The configurable 1% cost is
not added again.
"""

from __future__ import annotations

import base64
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from nacl.signing import SigningKey

from rhbot.errors import ClockSkewError, ConfigError, DataError, RateLimitError, ReadOnlyViolation
from rhbot.models import Quote
from rhbot.money import api_decimal as D

ENV_KEY = "RH_API_KEY"
ENV_SECRET = "RH_PRIVATE_KEY_BASE64"
BASE_URL = "https://trading.robinhood.com"
QUOTE_PATH = "/api/v1/crypto/marketdata/best_bid_ask/"


class TokenBucket:
    """100 tokens per 60 seconds. One token per read."""

    def __init__(self, capacity: int = 100, window_seconds: float = 60.0, clock=None):
        self.capacity = float(capacity)
        self.window = float(window_seconds)
        self.clock = clock or time.monotonic
        self.tokens = float(capacity)
        self.updated = self.clock()

    def take(self) -> None:
        now = self.clock()
        elapsed = max(0.0, now - self.updated)
        self.updated = now
        self.tokens = min(self.capacity, self.tokens + elapsed * (self.capacity / self.window))
        if self.tokens < 1.0:
            raise RateLimitError("read budget exhausted (100 per minute)")
        self.tokens -= 1.0


def build_signature(api_key: str, secret_b64: str, timestamp: str, path: str, method: str, body: str = "") -> str:
    """Sign one read. The message is ``api_key + timestamp + path + GET``.

    An empty body is omitted, matching the public docs for reads. A non-GET
    method or a non-empty body is refused.
    """
    if method != "GET" or body:
        raise ReadOnlyViolation("only GET with an empty body is signed")
    base = path.split("?", 1)[0]
    if not base.endswith("/"):
        base += "/"
    if base != QUOTE_PATH:
        raise ReadOnlyViolation("path is not on the read-only allowlist")
    try:
        seed = base64.b64decode(secret_b64, validate=True)
    except Exception as exc:
        raise ConfigError("private key is not valid base64") from exc
    if len(seed) != 32:
        raise ConfigError("private key must be a 32-byte seed, base64-encoded")
    message = f"{api_key}{timestamp}{path}GET".encode("utf-8")
    signed = SigningKey(seed).sign(message)
    return base64.b64encode(signed.signature).decode("utf-8")


def auth_headers(api_key: str, secret_b64: str, timestamp: str, path: str) -> dict[str, str]:
    signature = build_signature(api_key, secret_b64, timestamp, path, "GET", "")
    return {
        "x-api-key": api_key,
        "x-timestamp": timestamp,
        "x-signature": signature,
        "accept": "application/json",
    }


def _parse_time(value: object) -> datetime:
    if isinstance(value, (int, float)) or (isinstance(value, str) and str(value).isdigit()):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise DataError("quote timestamp missing")


def parse_best_bid_ask(payload: object, symbols: list[str]) -> dict[str, Quote]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise DataError("quote payload missing results")
    wanted = set(symbols)
    found: dict[str, Quote] = {}
    for row in payload["results"]:
        if not isinstance(row, dict):
            raise DataError("quote row is not an object")
        symbol = str(row.get("symbol") or "")
        if symbol not in wanted:
            continue
        try:
            bid = D(row["bid_inclusive_of_sell_spread"])
            ask = D(row["ask_inclusive_of_buy_spread"])
        except Exception as exc:
            raise DataError("quote row missing inclusive bid or ask") from exc
        if ask < bid:
            raise DataError("crossed quote")
        mid = (bid + ask) / Decimal(2)
        if row.get("timestamp") in (None, ""):
            raise DataError("quote timestamp missing")
        found[symbol] = Quote(
            symbol=symbol,
            ts=_parse_time(row["timestamp"]),
            mid=mid,
            source="robinhood",
            bid=bid,
            ask=ask,
            spread_included=True,
        )
    missing = [symbol for symbol in symbols if symbol not in found]
    if missing:
        raise DataError(f"quote response missing {missing}")
    return found


class RobinhoodMarketData:
    name = "robinhood"

    def __init__(self, api_key: str, private_key_b64: str, transport=None, sleep=None, clock=None):
        if not api_key or not private_key_b64:
            raise ConfigError("read-only credentials are incomplete")
        self._api_key = api_key
        self._secret = private_key_b64
        self._transport = transport
        self._sleep = sleep or time.sleep
        self.bucket = TokenBucket(clock=clock)

    def __repr__(self) -> str:
        return "RobinhoodMarketData(configured=True)"

    @classmethod
    def from_env(cls) -> RobinhoodMarketData | None:
        api_key = os.environ.get(ENV_KEY, "").strip()
        secret = os.environ.get(ENV_SECRET, "").strip()
        if not api_key and not secret:
            return None
        if not api_key or not secret:
            raise ConfigError(f"{ENV_KEY} and {ENV_SECRET} must both be set, or both left unset")
        return cls(api_key, secret)

    def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        query = urlencode([("symbol", symbol) for symbol in symbols])
        path = f"{QUOTE_PATH}?{query}"
        payload = self._get(path)
        return parse_best_bid_ask(payload, symbols)

    def _get(self, path: str) -> object:
        delay = 1.0
        for attempt in range(3):
            self.bucket.take()
            timestamp = str(int(time.time()))
            headers = auth_headers(self._api_key, self._secret, timestamp, path)
            status, body, response_headers = self._transport_call(BASE_URL + path, headers)
            if status == 429 and attempt < 2:
                self._sleep(delay)
                delay *= 2
                continue
            if status == 429:
                raise RateLimitError("quote endpoint returned 429")
            if status != 200 or not isinstance(body, dict):
                raise DataError(f"quote http status {status}")
            self._check_clock(response_headers)
            return body
        raise RateLimitError("quote endpoint returned 429")

    def _transport_call(self, url: str, headers: dict[str, str]):
        if self._transport is not None:
            return self._transport(url, headers)
        from rhbot.data.http import get_json

        return get_json(url, headers=headers)

    @staticmethod
    def _check_clock(headers: dict[str, str]) -> None:
        raw = headers.get("Date") or headers.get("date")
        if not raw:
            return
        server = parsedate_to_datetime(raw)
        if server.tzinfo is None:
            server = server.replace(tzinfo=timezone.utc)
        skew = abs((datetime.now(timezone.utc) - server.astimezone(timezone.utc)).total_seconds())
        if skew > 25:
            raise ClockSkewError(f"clock skew {skew:.1f}s exceeds the 30s signing window")
