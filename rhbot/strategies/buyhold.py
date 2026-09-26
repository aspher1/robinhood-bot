"""Buy once, split across the allowlist, then hold.

If a position is flattened back to flat cash, the sleeve will try to
redeploy on a later cycle. Kill the bot first if the flatten should stick.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from rhbot.config import Settings
from rhbot.models import Fill, MarketSnapshot, OrderIntent
from rhbot.money import q_cent


class BuyAndHold:
    name = "buy_and_hold"

    def __init__(self, settings: Settings):
        self.settings = settings

    def initial_state(self) -> dict:
        return {"done": False, "attempt_date": None}

    def decide(
        self,
        view: MarketSnapshot,
        state: dict,
        positions: dict,
        cash: Decimal,
        equity: Decimal,
        now: datetime,
    ) -> tuple[list[OrderIntent], dict, str]:
        del view, equity
        today = now.astimezone(timezone.utc).date().isoformat()
        if all(positions.get(symbol, Decimal(0)) > 0 for symbol in self.settings.symbols):
            return [], {**state, "done": True}, "holding"
        if state.get("attempt_date") == today:
            return [], state, "waiting_next_day"
        pending = [
            symbol
            for symbol in self.settings.symbols
            if positions.get(symbol, Decimal(0)) <= 0
        ]
        amount = q_cent(cash / Decimal(len(pending)))
        new_state = {**state, "attempt_date": today, "done": False}
        if amount < self.settings.min_order_notional:
            return [], new_state, "insufficient_cash"
        orders = [
            OrderIntent(
                symbol=symbol,
                side="buy",
                reason="buy_and_hold",
                quote_amount=amount,
            )
            for symbol in pending
        ]
        return orders, new_state, "deploy"

    def commit(self, state: dict, fills: list[Fill], positions: dict, now: datetime) -> dict:
        del now
        updated = dict(state)
        held = all(positions.get(symbol, Decimal(0)) > 0 for symbol in self.settings.symbols)
        fully_flat = all(positions.get(symbol, Decimal(0)) == 0 for symbol in self.settings.symbols)
        updated["done"] = held
        # A flatten should be allowed to redeploy later. A rejected buy must
        # keep today's attempt stamp so the loop does not retry every minute.
        if fully_flat and any(fill.side == "sell" for fill in fills):
            updated["attempt_date"] = None
            updated["done"] = False
        return updated
