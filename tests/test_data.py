from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from rhbot.data.public import (
    PublicMarketData,
    parse_coinbase_candles,
    parse_coinbase_ticker,
    parse_kraken_candles,
    parse_kraken_ticker,
)
from rhbot.data.robinhood import TokenBucket, parse_best_bid_ask
from rhbot.errors import RateLimitError
from rhbot.errors import DataError


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
        {"price": "199", "time": "2026-03-16T15:00:00Z", "bid": "200", "ask": "201"},
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
    assert quote.mid == Decimal("64000")
    assert quote.bid == Decimal("63990")
    assert quote.spread_included is False


def test_robinhood_quote_parser_marks_spread_included():
    payload = {
        "results": [
            {
                "symbol": "BTC-USD",
                "price": "999",
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
    assert quotes["BTC-USD"].mid == Decimal("100")
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


def test_coinbase_quote_batch_reuses_one_client_and_preserves_order(monkeypatch):
    urls = []
    clients = []

    def respond(request):
        urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "price": "100",
                "time": "2026-03-16T15:00:00Z",
                "bid": "99",
                "ask": "101",
            },
        )

    def make_client():
        client = httpx.Client(transport=httpx.MockTransport(respond))
        clients.append(client)
        return client

    monkeypatch.setattr("rhbot.data.http.json_client", make_client)
    quotes = PublicMarketData().fetch_quotes(["BTC-USD", "ETH-USD"])
    assert list(quotes) == ["BTC-USD", "ETH-USD"]
    assert urls == [
        "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
        "https://api.exchange.coinbase.com/products/ETH-USD/ticker",
    ]
    assert len(clients) == 1
    assert clients[0].is_closed


def test_quote_batch_closes_client_on_failure(monkeypatch):
    clients = []

    def make_client():
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        clients.append(client)
        return client

    monkeypatch.setattr("rhbot.data.http.json_client", make_client)
    with pytest.raises(DataError, match="coinbase ticker http 503"):
        PublicMarketData().fetch_quotes(["BTC-USD", "ETH-USD"])
    assert len(clients) == 1
    assert clients[0].is_closed


def test_injected_quote_transport_does_not_create_client(monkeypatch):
    def no_client():
        raise AssertionError("injected transport should not create an HTTP client")

    monkeypatch.setattr("rhbot.data.http.json_client", no_client)
    seen = []

    def transport(url):
        seen.append(url)
        return 200, {"price": "100", "time": "2026-03-16T15:00:00Z"}, {}

    PublicMarketData(transport=transport).fetch_quotes(["BTC-USD", "ETH-USD"])
    assert [url.split("/")[-2] for url in seen] == ["BTC-USD", "ETH-USD"]
