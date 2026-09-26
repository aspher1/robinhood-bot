"""v1 paper quotes are Coinbase public bid/ask. A bad quote is a hard stop."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from rhbot.config import Settings
from rhbot.errors import OrderRejected
from rhbot.models import MarketSnapshot, OrderIntent, Quote
from rhbot.ops import utcnow
from rhbot.status import assess

from tests.conftest import engine, snapshot


def _health(bot):
    return assess(bot.settings, now=utcnow())


def test_v1_paper_source_is_coinbase_public(tmp_path):
    with pytest.raises(ValueError):
        Settings(state_dir=tmp_path / "rh", market_data="robinhood")
    with pytest.raises(ValueError):
        Settings(state_dir=tmp_path / "kraken", public_provider="kraken")
    bot = engine(tmp_path / "paper")
    assert bot.public.provider == "coinbase"
    assert bot.quotes is bot.public
    assert bot.ledger.get_meta("quote_source") == "coinbase"
    assert bot.ledger.get_meta("quote_source") != "public_fallback"
    bot.ledger.close()


def test_stale_quote_denies_new_orders_and_is_critical(tmp_path, now, monkeypatch):
    def fail_fetch(self, symbols):
        raise AssertionError(f"quote fallback {symbols}")

    monkeypatch.setattr("rhbot.data.public.PublicMarketData.fetch_quotes", fail_fetch)
    monkeypatch.setattr(
        "rhbot.data.robinhood.RobinhoodMarketData.from_env",
        lambda: (_ for _ in ()).throw(AssertionError("robinhood fallback")),
    )
    bot = engine(tmp_path)
    aged = snapshot(now - timedelta(seconds=30))
    bot.run_once(now=now, snapshot=aged)
    assert bot.ledger.fills_for("buy_and_hold") == []
    health = _health(bot)
    assert health["health"] == "critical"
    assert "quote_hard_stop" in health["reasons"]
    assert health["checks"]["quotes"]["hard_stop"] == "stale_quote"
    assert health["quote_source"] == "coinbase"
    assert "robinhood_quotes_unavailable" not in health["reasons"]
    bot.ledger.close()


def test_missing_bid_ask_is_critical_after_a_good_quote(tmp_path, now):
    bot = engine(tmp_path)
    bot.run_once(now=now, snapshot=snapshot(now))
    before = len(bot.ledger.fills_for("buy_and_hold"))
    assert before == 2
    bare = MarketSnapshot(
        bars=snapshot(now).bars,
        quotes={
            symbol: Quote(
                symbol=symbol,
                ts=now,
                mid=Decimal("100"),
                bid=None,
                ask=None,
                source="test",
            )
            for symbol in ("BTC-USD", "ETH-USD")
        },
        source="test",
    )
    bot.run_once(now=now, snapshot=bare)
    assert len(bot.ledger.fills_for("buy_and_hold")) == before
    with pytest.raises(OrderRejected) as caught:
        bot.broker.submit(
            "buy_and_hold",
            OrderIntent("ETH-USD", "buy", "again", quote_amount=Decimal("10")),
            "buy_and_hold:ETH-USD:buy:no-bid",
            bot._context("buy_and_hold", bare, now),
            now,
        )
    assert caught.value.reasons == ["missing_bid_ask"]
    health = _health(bot)
    assert health["health"] == "critical"
    assert health["checks"]["quotes"]["hard_stop"] == "missing_bid_ask"
    assert health["quote_source"] == "coinbase"
    bot.ledger.close()


def test_missing_quote_is_critical_and_does_not_change_source(tmp_path, now):
    bot = engine(tmp_path)
    bot.run_once(now=now, snapshot=snapshot(now, mid="100"))
    before = len(bot.ledger.fills_for("buy_and_hold"))
    empty = MarketSnapshot(bars={}, quotes={}, source="kraken")
    with pytest.raises(RuntimeError):
        bot.run_once(now=now, snapshot=empty)
    assert len(bot.ledger.fills_for("buy_and_hold")) == before
    held = bot._context("buy_and_hold", snapshot(now, mid="100"), now)
    held.quotes = {}
    denied = bot.risk.evaluate(
        OrderIntent("BTC-USD", "buy", "again", quote_amount=Decimal("10")),
        held,
        "buy_and_hold:BTC-USD:buy:missing",
    )
    assert denied.reasons == ["missing_quote"]
    health = _health(bot)
    assert health["health"] == "critical"
    assert health["checks"]["quotes"]["hard_stop"] == "missing_quote"
    assert health["quote_source"] == "coinbase"
    assert bot.ledger.get_meta("last_valid_quotes")
    assert "kraken" not in bot.ledger.get_meta("last_valid_quotes")
    bot.ledger.close()


def test_kraken_quotes_are_not_a_trading_source(tmp_path, now):
    bot = engine(tmp_path)
    view = snapshot(now)
    kraken = MarketSnapshot(
        bars=view.bars,
        quotes={symbol: replace(quote, source="kraken") for symbol, quote in view.quotes.items()},
        source="kraken",
    )
    with pytest.raises(RuntimeError):
        bot.run_once(now=now, snapshot=kraken)
    assert bot.ledger.fills_for("buy_and_hold") == []
    health = _health(bot)
    assert health["health"] == "critical"
    assert health["checks"]["quotes"]["hard_stop"] == "missing_quote"
    assert health["quote_source"] == "coinbase"
    bot.ledger.close()


def test_reduce_only_flatten_uses_the_last_valid_quote(tmp_path, now):
    bot = engine(tmp_path)
    bot.run_once(now=now, snapshot=snapshot(now, mid="100"))
    market_now = now + timedelta(seconds=31)
    stale = snapshot(market_now, mid="50")
    stale = MarketSnapshot(
        bars=stale.bars,
        quotes={
            symbol: replace(quote, ts=market_now - timedelta(seconds=30))
            for symbol, quote in stale.quotes.items()
        },
        source="test",
    )
    with pytest.raises(OrderRejected) as bought:
        bot.broker.submit(
            "buy_and_hold",
            OrderIntent("BTC-USD", "buy", "again", quote_amount=Decimal("10")),
            "buy_and_hold:BTC-USD:buy:stale-new",
            bot._context("buy_and_hold", stale, market_now),
            market_now,
        )
    assert bought.value.reasons == ["stale_quote"]
    flagged = MarketSnapshot(
        bars=stale.bars,
        quotes={symbol: replace(quote, allow_stale=True) for symbol, quote in stale.quotes.items()},
        source="test",
    )
    with pytest.raises(OrderRejected) as slipped:
        bot.broker.submit(
            "buy_and_hold",
            OrderIntent("ETH-USD", "buy", "again", quote_amount=Decimal("10")),
            "buy_and_hold:ETH-USD:buy:allow-stale",
            bot._context("buy_and_hold", flagged, market_now),
            market_now,
        )
    assert slipped.value.reasons == ["stale_quote"]
    qty = bot.ledger.positions("buy_and_hold")["BTC-USD"]
    with pytest.raises(OrderRejected) as raw:
        bot.broker.submit(
            "buy_and_hold",
            OrderIntent("BTC-USD", "sell", "flatten", base_quantity=qty),
            "manual-stale-reduce",
            bot._context("buy_and_hold", stale, market_now),
            market_now,
            reduce_only=True,
        )
    assert raw.value.reasons == ["stale_quote"]
    result = bot.flatten(now=market_now, snapshot=stale)
    assert result["errors"] == []
    assert result["remaining"] == []
    assert result["fills"]
    for fill in result["fills"]:
        assert Decimal(fill["mid"]) == Decimal("100")
    health = _health(bot)
    assert health["health"] == "critical"
    assert health["quote_source"] == "coinbase"
    bot.ledger.close()
