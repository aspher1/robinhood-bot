import re
from pathlib import Path

import pytest

from rhbot.brokers.live import LiveBroker
from rhbot.data.robinhood import QUOTE_PATH, build_signature
from rhbot.errors import LiveTradingDisabled, ReadOnlyViolation

ROOT = Path(__file__).resolve().parents[1] / "rhbot"
ORDER_PATH = re.compile(r"/api/v\d+/crypto/trading/orders", re.I)
HTTP_WRITE = re.compile(
    r"(?i)(?:\.(?:post|delete|put|patch)\s*\(|\b(?:httpx|requests)\s*\.\s*(?:post|delete|put|patch)\b|method\s*=\s*['\"](?:POST|PUT|PATCH|DELETE)['\"])"
)


def test_hard_caps_match_the_risk_policy():
    from decimal import Decimal

    from rhbot.config import ALLOW_MARGIN, ALLOW_SHORT, HARD_CAPS, LEVERAGE, PRODUCT

    assert PRODUCT == "spot"
    assert LEVERAGE == Decimal("1")
    assert ALLOW_MARGIN is False
    assert ALLOW_SHORT is False
    assert HARD_CAPS["max_position_pct"] == Decimal("0.50")
    assert HARD_CAPS["max_total_exposure_pct"] == Decimal("1")
    assert HARD_CAPS["min_cost_per_side"] == Decimal("0.01")
    assert HARD_CAPS["min_order_notional"] == Decimal("10")
    assert HARD_CAPS["max_trades_per_day"] == 2
    assert HARD_CAPS["max_daily_turnover_pct"] == Decimal("1")
    assert HARD_CAPS["min_hold_days"] == 7
    assert HARD_CAPS["max_daily_loss_pct"] == Decimal("0.04")
    assert "dd_cut_half" not in HARD_CAPS
    assert "dd_cut_quarter" not in HARD_CAPS
    assert "exposure_cap_at_half" not in HARD_CAPS
    assert "exposure_cap_at_quarter" not in HARD_CAPS
    assert HARD_CAPS["drawdown_freeze_pct"] == Decimal("0.10")
    assert HARD_CAPS["max_drawdown_pct"] == Decimal("0.40")
    assert HARD_CAPS["max_quote_age_seconds"] == 30
    assert HARD_CAPS["max_spread_per_side"] == Decimal("0.02")


def test_codeowners_covers_risk_caps_and_brokers():
    text = (Path(__file__).resolve().parents[1] / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    assert "/rhbot/risk.py" in text
    assert "/rhbot/config.py" in text
    assert "/rhbot/brokers/" in text


def test_env_cannot_enable_live(tmp_path, monkeypatch):
    from rhbot.config import load_settings
    from rhbot.errors import ConfigError

    monkeypatch.setenv("RHBOT_LIVE", "1")
    with pytest.raises(ConfigError):
        load_settings(None, str(tmp_path))
    monkeypatch.delenv("RHBOT_LIVE")
    monkeypatch.setenv("RHBOT_MODE", "live")
    with pytest.raises(ConfigError):
        load_settings(None, str(tmp_path))


def test_engine_never_clears_the_kill_file():
    text = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert "clear_kill" not in text
    assert "clear_freeze" not in text
    assert "_sell_down" not in text
    assert "exposure_cut" not in text
    assert "LiveBroker(" not in text


def test_package_has_no_live_order_path():
    offenders = []
    for path in ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if ORDER_PATH.search(text) or HTTP_WRITE.search(text):
            offenders.append(str(path))
        if "trading.robinhood.com" in text and re.search(r"\borders?\b", text, re.I):
            offenders.append(str(path))
    assert offenders == []


def test_live_broker_cannot_submit_cancel_or_amend():
    broker = LiveBroker()
    with pytest.raises(LiveTradingDisabled):
        broker.submit()
    with pytest.raises(LiveTradingDisabled):
        broker.cancel()
    with pytest.raises(LiveTradingDisabled):
        broker.amend()


def test_signer_refuses_writes_and_unlisted_paths():
    secret = __import__("base64").b64encode(b"\x11" * 32).decode("ascii")
    with pytest.raises(ReadOnlyViolation):
        build_signature("rh-api-test", secret, "1700000000", QUOTE_PATH, "POST", "")
    with pytest.raises(ReadOnlyViolation):
        build_signature(
            "rh-api-test",
            secret,
            "1700000000",
            "/api/v1/crypto/trading/orders/",
            "GET",
            "",
        )


def test_get_signature_matches_frozen_vector():
    secret = __import__("base64").b64encode(b"\x11" * 32).decode("ascii")
    path = "/api/v1/crypto/marketdata/best_bid_ask/?symbol=BTC-USD"
    signature = build_signature("rh-api-test", secret, "1700000000", path, "GET", "")
    assert (
        signature
        == "HP24uohKq6QCUilVMams/orlOS1g3CcbWYbZmDYxwIofwNCsjU0Wce0kEO53lmkzHCblJRbDASRmGlCPGiwsCQ=="
    )
