import ast
import re
from pathlib import Path

import pytest

from rhbot.brokers.live import LiveBroker
from rhbot.data.robinhood import QUOTE_PATH, build_signature
from rhbot.errors import LiveTradingDisabled, ReadOnlyViolation

ROOT = Path(__file__).resolve().parents[1] / "rhbot"
REPO = Path(__file__).resolve().parents[1]
ORDER_PATH = re.compile(r"/api/v\d+/crypto/trading/orders", re.I)
COINBASE_ORDER = re.compile(
    r"api\.coinbase\.com/.*/(?:" + "orders|batch_orders)|" + "/api/v3/" + "brokerage/" + "orders",
    re.I,
)
KRAKEN_ORDER = re.compile("/0/private/" + "AddOrder", re.I)
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
    assert HARD_CAPS["freeze_drawdown_pct"] == Decimal("0.10")
    assert HARD_CAPS["kill_drawdown_pct"] == Decimal("0.40")
    assert "pause_drawdown_pct" not in HARD_CAPS
    assert "dd_cut_half" not in HARD_CAPS
    assert HARD_CAPS["max_quote_age_seconds"] == 30
    assert HARD_CAPS["max_spread_per_side"] == Decimal("0.02")


def test_codeowners_covers_risk_caps_and_brokers():
    text = (Path(__file__).resolve().parents[1] / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    assert "/rhbot/risk.py" in text
    assert "/rhbot/config.py" in text
    assert "/rhbot/brokers/" in text
    for path in (
        "/rhbot/engine.py",
        "/rhbot/overlay.py",
        "/rhbot/ops.py",
        "/rhbot/cli.py",
        "/rhbot/pricing.py",
        "/rhbot/models.py",
        "/rhbot/ledger.py",
        "/rhbot/strategies/",
        "/.github/workflows/",
    ):
        assert path in text


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
    assert "rebase_peaks" not in text
    assert "uuid" not in text
    cli = (ROOT.parent / "rhbot" / "cli.py").read_text(encoding="utf-8")
    assert "rebase_peaks" not in cli


_HTTP_WRITE_NAMES = {"request", "stream", "send", "post", "put", "patch", "delete"}


def http_write_findings(source: str) -> list[str]:
    """AST scan: only HTTP GET is allowed. Writes and urllib.request fail the build."""
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "urllib.request" or alias.name.startswith("urllib.request."):
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module in ("urllib.request", "urllib"):
            names = {alias.name for alias in node.names}
            if node.module == "urllib.request" or "request" in names:
                found.append(f"from {node.module} import {sorted(names)}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Call) and _getattr_write(func, node):
                found.append("getattr write")
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in _HTTP_WRITE_NAMES:
                found.append(name)
            if name in ("urlopen", "Request"):
                if any(kw.arg == "data" for kw in node.keywords) or len(node.args) >= 2:
                    found.append(f"{name}(data=...)")
    return found


def _getattr_write(func: ast.Call, call: ast.Call) -> bool:
    """getattr(client, "post")(...) and getattr(client, "request")("POST", ...)."""
    inner = func.func
    if not isinstance(inner, ast.Name) or inner.id != "getattr":
        return False
    if len(func.args) < 2 or not isinstance(func.args[1], ast.Constant):
        return False
    method = str(func.args[1].value)
    if method in _HTTP_WRITE_NAMES - {"request", "stream", "send"}:
        return True
    if method in {"request", "stream", "send"} and call.args:
        verb = call.args[0]
        if isinstance(verb, ast.Constant) and str(verb.value).upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            return True
    return False


def _py_files(root: Path):
    skip = {".git", "__pycache__", ".venv", "venv"}
    for path in root.rglob("*.py"):
        if any(part in skip for part in path.parts):
            continue
        yield path


def test_package_has_no_live_order_path():
    offenders = []
    for path in _py_files(REPO):
        text = path.read_text(encoding="utf-8")
        if (
            ORDER_PATH.search(text)
            or COINBASE_ORDER.search(text)
            or KRAKEN_ORDER.search(text)
            or HTTP_WRITE.search(text)
            or http_write_findings(text)
        ):
            offenders.append(str(path))
        host = "trading.robinhood." + "com"
        if host in text and re.search(r"\borders?\b", text, re.I):
            offenders.append(str(path))
    assert offenders == []


def _order_path(text: str) -> bool:
    return bool(ORDER_PATH.search(text) or COINBASE_ORDER.search(text) or KRAKEN_ORDER.search(text))


def test_http_write_forms_are_rejected(tmp_path):
    del tmp_path
    samples = {
        "post_request.py": "def f(client):\n    client.request('POST', 'https://example')\n",
        "httpx_put.py": "import httpx\nhttpx.request('PUT', 'https://example')\n",
        "stream_post.py": "def f(client):\n    client.stream('POST', 'https://example')\n",
        "urllib_data.py": "import urllib.request\nurllib.request.Request('https://example', data=b'x')\n",
        "urlopen_data.py": "def f(urlopen):\n    urlopen('https://example', data=b'x')\n",
        "getattr_post.py": "def f(client):\n    getattr(client, 'post')('https://example')\n",
        "getattr_request.py": "def f(client):\n    getattr(client, 'request')('POST', 'https://example')\n",
        "coinbase_order.py": "URL = 'https://api.coinbase.com" + "/api/v3/brokerage/" + "orders'\n",
        "kraken_order.py": "URL = 'https://api.kraken.com" + "/0/private/" + "AddOrder'\n",
    }
    outside = REPO / "_audit_outside.py"
    outside.write_text(samples["getattr_post.py"], encoding="utf-8")
    try:
        assert str(outside) in [str(path) for path in _py_files(REPO) if http_write_findings(path.read_text(encoding="utf-8"))]
    finally:
        outside.unlink(missing_ok=True)
    for name, source in samples.items():
        path = ROOT / f"_audit_{name}"
        path.write_text(source, encoding="utf-8")
        try:
            assert http_write_findings(source) or _order_path(source) or HTTP_WRITE.search(source), name
            text = path.read_text(encoding="utf-8")
            offenders = [
                str(item)
                for item in _py_files(REPO)
                if http_write_findings(item.read_text(encoding="utf-8"))
                or _order_path(item.read_text(encoding="utf-8"))
                or HTTP_WRITE.search(item.read_text(encoding="utf-8"))
            ]
            assert str(path) in offenders
            assert http_write_findings(text) or _order_path(text) or HTTP_WRITE.search(text)
        finally:
            path.unlink(missing_ok=True)


def test_live_broker_is_not_reexported():
    import rhbot.brokers as brokers

    assert not hasattr(brokers, "LiveBroker")
    with pytest.raises(ImportError):
        from rhbot.brokers import LiveBroker  # noqa: F401


def test_config_yaml_is_gitignored():
    import subprocess

    root = Path(__file__).resolve().parents[1]
    ignored = subprocess.run(["git", "check-ignore", "config.yaml"], cwd=root, check=False)
    assert ignored.returncode == 0
    example = subprocess.run(["git", "check-ignore", "config.example.yaml"], cwd=root, check=False)
    assert example.returncode != 0


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
            "/api/v1/crypto/trading/" + "orders/",
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
