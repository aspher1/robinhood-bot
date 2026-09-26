from datetime import timedelta
from decimal import Decimal

import pytest

from rhbot.config import Settings
from rhbot.errors import ConfigError, OrderRejected
from rhbot.models import OrderIntent, Quote
from rhbot.ops import engage_kill
from rhbot.risk import RiskContext, RiskEngine

from tests.conftest import engine, make_quote


def _settings(path, **overrides):
    return Settings(state_dir=path, **overrides)


def _ctx(path_settings, now, **overrides) -> RiskContext:
    quotes = {
        "BTC-USD": make_quote("BTC-USD", "100", now),
        "ETH-USD": make_quote("ETH-USD", "100", now),
    }
    base = dict(
        now=now,
        sleeve="buy_and_hold",
        equity=Decimal("1000"),
        cash=Decimal("1000"),
        positions={},
        quotes=quotes,
        day_start_equity=Decimal("1000"),
        peak_equity=Decimal("1000"),
        trades_today=0,
        turnover_today=Decimal(0),
        known_client_ids=set(),
    )
    base.update(overrides)
    return RiskContext(**base)


def _buy(amount="100", symbol="BTC-USD") -> OrderIntent:
    return OrderIntent(symbol, "buy", "test", quote_amount=Decimal(amount))


def test_position_cap_and_exposure_cap(tmp_path, now):
    settings = _settings(tmp_path, max_total_exposure_pct=Decimal("0.60"))
    risk = RiskEngine(settings)
    too_big = risk.evaluate(_buy("600"), _ctx(settings, now), "id-1")
    assert not too_big.allowed
    assert too_big.reasons == ["per_trade_cap"]

    held = _ctx(
        settings,
        now,
        cash=Decimal("500"),
        positions={"BTC-USD": Decimal("5")},
        turnover_today=Decimal("500"),
        trades_today=1,
    )
    exposed = risk.evaluate(_buy("200", "ETH-USD"), held, "id-2")
    assert not exposed.allowed
    assert "exposure_cap" in exposed.reasons


def test_stale_quote_blocks(tmp_path, now):
    settings = _settings(tmp_path)
    risk = RiskEngine(settings)
    stale = make_quote("BTC-USD", "100", now - timedelta(minutes=10))
    fresh = make_quote("ETH-USD", "100", now)
    decision = risk.evaluate(
        _buy(),
        _ctx(settings, now, quotes={"BTC-USD": stale, "ETH-USD": fresh}),
        "id-stale",
    )
    assert decision.reasons == ["stale_quote"]
    assert not decision.kill


def test_kill_file_blocks_before_fill(tmp_path, now):
    settings = _settings(tmp_path)
    engage_kill(tmp_path, "operator stop", "operator")
    risk = RiskEngine(settings)
    decision = risk.evaluate(_buy(), _ctx(settings, now), "id-kill")
    assert decision.reasons == ["kill_switch"]


def test_drawdown_trips_kill(tmp_path, now):
    settings = _settings(tmp_path)
    risk = RiskEngine(settings)
    daily = risk.evaluate(
        _buy(),
        _ctx(settings, now, equity=Decimal("940"), cash=Decimal("940")),
        "id-daily",
    )
    assert daily.kill
    assert any(reason.startswith("daily_drawdown") for reason in daily.reasons)

    deep = risk.evaluate(
        _buy(),
        _ctx(settings, now, equity=Decimal("890"), cash=Decimal("890")),
        "id-max",
    )
    assert deep.kill
    assert any(reason.startswith("max_drawdown") for reason in deep.reasons)


def test_duplicate_symbol_and_trade_cap(tmp_path, now):
    settings = _settings(tmp_path, max_trades_per_day=1)
    risk = RiskEngine(settings)
    ctx = _ctx(settings, now, known_client_ids={"again"})
    assert risk.evaluate(_buy(), ctx, "again").reasons == ["duplicate_client_order_id"]
    foreign = OrderIntent("DOGE-USD", "buy", "no", quote_amount=Decimal("10"))
    assert risk.evaluate(foreign, ctx, "id-x").reasons == ["symbol_not_allowed"]
    capped = risk.evaluate(_buy(), _ctx(settings, now, trades_today=1), "id-cap")
    assert capped.reasons == ["max_trades_per_day"]


def test_short_is_refused(tmp_path, now):
    settings = _settings(tmp_path)
    risk = RiskEngine(settings)
    sell = OrderIntent("BTC-USD", "sell", "test", base_quantity=Decimal("2"))
    decision = risk.evaluate(sell, _ctx(settings, now, positions={"BTC-USD": Decimal("1")}), "id-short")
    assert decision.reasons == ["short_not_allowed"]


def test_fail_closed_on_bad_quote(tmp_path, now):
    settings = _settings(tmp_path)
    risk = RiskEngine(settings)
    bad = Quote(symbol="BTC-USD", ts=now, mid=Decimal("0"), source="test")
    other = make_quote("ETH-USD", "100", now)
    decision = risk.evaluate(
        _buy("10"),
        _ctx(settings, now, quotes={"BTC-USD": bad, "ETH-USD": other}),
        "id-bad",
    )
    assert not decision.allowed
    assert decision.reasons[0].startswith("fail_closed:")


def test_config_may_only_tighten(tmp_path):
    with pytest.raises(ValueError):
        Settings(state_dir=tmp_path, max_position_pct=Decimal("0.80"))
    with pytest.raises(ValueError):
        Settings(state_dir=tmp_path, max_drawdown_pct=Decimal("0.20"))
    with pytest.raises(ValueError):
        Settings(state_dir=tmp_path, max_trades_per_day=8)
    with pytest.raises(ValueError):
        Settings(state_dir=tmp_path, mode="live")
    with pytest.raises(ValueError):
        Settings(state_dir=tmp_path, symbols=("BTC-USD", "SOL-USD"))
    tightened = Settings(
        state_dir=tmp_path,
        max_position_pct=Decimal("0.25"),
        trend_target_weight=Decimal("0.25"),
        max_quote_age_seconds=30,
    )
    assert tightened.max_position_pct == Decimal("0.25")


def test_yaml_float_is_rejected(tmp_path):
    from rhbot.config import load_settings

    path = tmp_path / "config.yaml"
    path.write_text("cost_per_side: 0.01\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(str(path), str(tmp_path / "state"))


def test_example_config_loads(tmp_path):
    from rhbot.config import load_settings

    loaded = load_settings("config.example.yaml", str(tmp_path / "state"))
    assert loaded.mode == "paper"
    assert loaded.starting_cash == Decimal("1000")
    assert loaded.cost_per_side == Decimal("0.01")
    assert loaded.symbols == ("BTC-USD", "ETH-USD")


def test_engine_stale_data_rejects_without_killing(tmp_path, now):
    bot = engine(tmp_path, sma_window=3, min_hold_days=0)
    view = snapshot_at(now - timedelta(minutes=10), now)
    bot.run_once(now=now, snapshot=view)
    assert bot.ledger.fills_for("buy_and_hold") == []
    assert bot.ledger.count_events("risk_reject", "buy_and_hold") >= 1
    assert not (tmp_path / "KILL").exists()
    bot.ledger.close()


def snapshot_at(quote_time, market_now):
    from tests.conftest import snapshot

    view = snapshot(market_now)
    return type(view)(
        bars=view.bars,
        quotes={
            symbol: Quote(symbol=symbol, ts=quote_time, mid=quote.mid, source="test")
            for symbol, quote in view.quotes.items()
        },
        source="test",
    )


def test_broker_rejects_when_risk_rejects(tmp_path, now):
    bot = engine(tmp_path)
    bot.ledger.ensure_sleeve("buy_and_hold", now, {})
    view = snapshot_at(now - timedelta(hours=1), now)
    with pytest.raises(OrderRejected) as caught:
        bot.broker.submit(
            "buy_and_hold",
            _buy(),
            "id-broker",
            bot._context("buy_and_hold", view, now),
            now,
        )
    assert caught.value.reasons == ["stale_quote"]
    bot.ledger.close()
