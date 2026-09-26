from datetime import datetime, timezone
from decimal import Decimal

from rhbot.data.public import (
    parse_coinbase_candles,
    parse_coinbase_ticker,
    parse_kraken_candles,
    parse_kraken_ticker,
)
from rhbot.data.robinhood import TokenBucket, parse_best_bid_ask
from rhbot.errors import RateLimitError


def test_coinbase_and_kraken_parsers():
    candles = parse_coinbase_candles(
        "BTC-USD",
        [[1700000000, 1.0, 3.0, 2.0, 2.5, 9.0]],
    )
    assert candles[0].close == Decimal("2.5")
    assert candles[0].open == Decimal("2")
    assert candles[0].source == "coinbase"
    ticker = parse_coinbase_ticker(
        "ETH-USD",
        {"price": "200.5", "time": "2026-03-16T15:00:00Z", "bid": "200", "ask": "201"},
    )
    assert ticker.mid == Decimal("200.5")
    assert ticker.spread_included is False
    assert ticker.ts == datetime(2026, 3, 16, 15, 0, tzinfo=timezone.utc)

    kraken = parse_kraken_candles(
        "BTC-USD",
        {"error": [], "result": {"XXBTZUSD": [[1700000000, "2", "3", "1", "2.5", "2.4", "8", 3]], "last": 1}},
    )
    assert kraken[0].close == Decimal("2.5")
    assert kraken[0].high == Decimal("3")
    quote = parse_kraken_ticker(
        "BTC-USD",
        {"error": [], "result": {"XXBTZUSD": {"c": ["64000.1", "0.1"], "a": ["64010", "1", "1"], "b": ["63990", "1", "1"]}}},
        {"Date": "Mon, 16 Mar 2026 15:00:00 GMT"},
    )
    assert quote.mid == Decimal("64000.1")
    assert quote.bid == Decimal("63990")
    assert quote.spread_included is False


def test_robinhood_quote_parser_marks_spread_included():
    payload = {
        "results": [
            {
                "symbol": "BTC-USD",
                "price": "100",
                "bid_inclusive_of_sell_spread": "99",
                "ask_inclusive_of_buy_spread": "101",
                "timestamp": "2026-03-16T15:00:00Z",
            },
            {
                "symbol": "ETH-USD",
                "price": "200",
                "bid_inclusive_of_sell_spread": "198",
                "ask_inclusive_of_buy_spread": "202",
                "timestamp": "2026-03-16T15:00:01Z",
            },
        ]
    }
    quotes = parse_best_bid_ask(payload, ["BTC-USD", "ETH-USD"])
    assert quotes["BTC-USD"].spread_included is True
    assert quotes["BTC-USD"].ask == Decimal("101")
    assert quotes["ETH-USD"].bid == Decimal("198")


def test_read_budget_is_100_per_minute():
    clock = {"now": 0.0}

    def tick():
        return clock["now"]

    bucket = TokenBucket(capacity=100, window_seconds=60, clock=tick)
    for _ in range(100):
        bucket.take()
    try:
        bucket.take()
        raised = False
    except RateLimitError:
        raised = True
    assert raised
    clock["now"] = 60.0
    bucket.take()
