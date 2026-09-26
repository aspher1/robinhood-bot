"""Offline preflight. Uses a throwaway directory so the real kill file is untouched."""

from __future__ import annotations

import base64
import tempfile
from decimal import Decimal
from pathlib import Path

from nacl.signing import SigningKey

from rhbot.config import Settings
from rhbot.data.robinhood import QUOTE_PATH, build_signature
from rhbot.engine import Engine
from rhbot.errors import OrderRejected
from rhbot.models import MarketSnapshot, OrderIntent, Quote
from rhbot.ops import clear_kill, engage_kill, utcnow


def signature_roundtrip() -> None:
    seed = b"\x22" * 32
    secret = base64.b64encode(seed).decode("ascii")
    path = f"{QUOTE_PATH}?symbol=BTC-USD"
    message = f"rh-api-selftest1700000000{path}GET".encode("utf-8")
    signature = build_signature("rh-api-selftest", secret, "1700000000", path, "GET", "")
    SigningKey(seed).verify_key.verify(message, base64.b64decode(signature))


def run_selftest(settings: Settings) -> dict:
    checks: list[dict] = [{"name": "config", "ok": settings.mode == "paper"}]
    try:
        signature_roundtrip()
        checks.append({"name": "signature", "ok": True})
    except Exception as exc:
        checks.append({"name": "signature", "ok": False, "error": str(exc)})

    with tempfile.TemporaryDirectory(prefix="rhbot-selftest-") as tmp:
        drill = settings.model_copy(update={"state_dir": Path(tmp)})
        now = utcnow()
        quotes = {
            symbol: Quote(symbol=symbol, ts=now, mid=Decimal("100"), source="selftest")
            for symbol in drill.symbols
        }
        snapshot = MarketSnapshot(
            bars={symbol: [] for symbol in drill.symbols},
            quotes=quotes,
            source="selftest",
        )
        try:
            engine = Engine(drill)
            engine.run_once(now=now, snapshot=snapshot)
            positions = engine.ledger.positions("buy_and_hold")
            bought = all(positions.get(symbol, Decimal(0)) > 0 for symbol in drill.symbols)
            checks.append({"name": "paper_buy", "ok": bought})
            flattened = engine.flatten(now=now, snapshot=snapshot)
            flat = all(
                engine.ledger.position_qty("buy_and_hold", symbol) == 0 for symbol in drill.symbols
            )
            costs = engine.ledger.conn.execute(
                "SELECT COALESCE(SUM(CAST(cost AS REAL)), 0) AS n FROM fills"
            ).fetchone()["n"]
            checks.append(
                {
                    "name": "flatten",
                    "ok": flat and not flattened["errors"] and float(costs) > 0,
                }
            )
            engage_kill(drill.state_dir, "selftest", "selftest")
            rejected = False
            try:
                intent = OrderIntent(
                    "BTC-USD", "buy", "selftest", quote_amount=Decimal("10")
                )
                engine.broker.submit(
                    "buy_and_hold",
                    intent,
                    "selftest-blocked",
                    engine._context("buy_and_hold", snapshot, now),
                    now,
                )
            except OrderRejected as exc:
                rejected = "kill_switch" in exc.reasons
            checks.append({"name": "kill_switch", "ok": rejected})
            clear_kill(drill.state_dir)
            chain_ok, detail = engine.ledger.verify_chain()
            checks.append({"name": "audit", "ok": chain_ok, "detail": detail})
            engine.ledger.close()
        except Exception as exc:
            checks.append({"name": "drill", "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    return {"ok": all(item["ok"] for item in checks), "checks": checks, "mode": "paper"}
