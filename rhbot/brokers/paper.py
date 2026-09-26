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
from rhbot.risk import RiskContext, RiskDecision, RiskEngine, deny


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
            self._record_denial(
                sleeve,
                intent,
                client_order_id,
                now,
                deny("duplicate_client_order_id", "client_order_id", "unique"),
            )
            raise OrderRejected(["duplicate_client_order_id"])
        decision = self.risk.evaluate(
            intent, ctx, client_order_id, reduce_only=reduce_only
        )
        if not decision.allowed:
            self._record_denial(sleeve, intent, client_order_id, now, decision)
            raise OrderRejected(decision.reasons, kill=decision.kill)
        if kill_active(self.settings.state_dir) and not reduce_only:
            blocked = deny("kill_switch", "kill_switch", "engaged")
            self._record_denial(sleeve, intent, client_order_id, now, blocked)
            raise OrderRejected(blocked.reasons)
        quote = ctx.quotes[intent.symbol]
        try:
            qty, price, cash_delta, cost, notional = plan_fill(
                intent, quote, self.settings.cost_per_side
            )
        except DataError as exc:
            blocked = deny("fail_closed", "fail_closed", "deny", type(exc).__name__, detail=f"fail_closed:{exc}")
            self._record_denial(sleeve, intent, client_order_id, now, blocked)
            raise OrderRejected(blocked.reasons) from exc
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
            blocked = deny("duplicate_client_order_id", "client_order_id", "unique")
            self._record_denial(sleeve, intent, client_order_id, now, blocked)
            raise OrderRejected(blocked.reasons) from exc
        return fill

    def _record_denial(
        self,
        sleeve: str,
        intent: OrderIntent,
        client_order_id: str,
        now: datetime,
        decision: RiskDecision,
    ) -> None:
        hit = decision.breaches[0] if decision.breaches else None
        reason = hit.reason if hit is not None else (decision.reasons[0] if decision.reasons else "rejected")
        limit_name = hit.limit_name if hit is not None else reason
        limit_value = hit.limit if hit is not None else "deny"
        observed = hit.observed if hit is not None else ""
        detail = decision.reasons[0] if decision.reasons else reason
        self.ledger.record_risk_event(
            "risk_denial",
            now,
            sleeve=sleeve,
            symbol=intent.symbol,
            side=intent.side,
            reason=reason,
            limit_name=limit_name,
            limit_value=limit_value,
            observed=observed,
            client_order_id=client_order_id,
            detail=detail,
        )
        self.ledger.bump("error_risk_reject")
