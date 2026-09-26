"""Simulated fills. Risk runs inside submit, so a fill cannot skip it."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from rhbot.config import Settings
from rhbot.errors import DataError, OrderRejected
from rhbot.ledger import Ledger
from rhbot.models import Fill, OrderIntent
from rhbot.ops import kill_active
from rhbot.pricing import plan_fill
from rhbot.risk import RiskContext, RiskEngine


class PaperBroker:
    def __init__(self, settings: Settings, ledger: Ledger, risk: RiskEngine):
        self.settings = settings
        self.ledger = ledger
        self.risk = risk

    def submit(
        self,
        sleeve: str,
        intent: OrderIntent,
        client_order_id: str,
        ctx: RiskContext,
        now: datetime,
        *,
        reduce_only: bool = False,
    ) -> Fill:
        # Same client id returns the original fill and does not trade again.
        existing = self.ledger.get_fill(client_order_id)
        if existing is not None:
            return existing
        if client_order_id in ctx.known_client_ids or client_order_id in self.ledger.known_client_ids():
            raise OrderRejected(["duplicate_client_order_id"])
        decision = self.risk.evaluate(
            intent, ctx, client_order_id, reduce_only=reduce_only
        )
        if not decision.allowed:
            raise OrderRejected(decision.reasons, kill=decision.kill)
        if kill_active(self.settings.state_dir) and not reduce_only:
            raise OrderRejected(["kill_switch"])
        quote = ctx.quotes[intent.symbol]
        try:
            qty, price, cash_delta, cost, notional = plan_fill(
                intent, quote, self.settings.cost_per_side
            )
        except DataError as exc:
            raise OrderRejected([f"fail_closed:{exc}"]) from exc
        qty_delta = qty if intent.side == "buy" else -qty
        fill = Fill(
            sleeve=sleeve,
            symbol=intent.symbol,
            side=intent.side,
            qty=qty,
            qty_delta=qty_delta,
            mid=quote.mid,
            fill_price=price,
            cash_delta=cash_delta,
            cost=cost,
            notional=notional,
            ts=now,
            client_order_id=client_order_id,
            reason=intent.reason,
        )
        try:
            self.ledger.commit_fill(fill)
        except sqlite3.IntegrityError as exc:
            raise OrderRejected(["duplicate_client_order_id"]) from exc
        return fill
