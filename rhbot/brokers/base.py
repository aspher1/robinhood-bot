"""The paper broker is the only broker the engine constructs."""

from __future__ import annotations

from typing import Protocol

from rhbot.models import Fill, OrderIntent
from rhbot.risk import RiskContext


class Broker(Protocol):
    def submit(
        self,
        sleeve: str,
        intent: OrderIntent,
        client_order_id: str,
        ctx: RiskContext,
        now: object,
        *,
        reduce_only: bool = False,
    ) -> Fill: ...
