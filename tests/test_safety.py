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
