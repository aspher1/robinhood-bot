"""Buy a fixed dollar amount of each symbol once per ISO week."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from rhbot.config import Settings
from rhbot.models import Fill, MarketSnapshot, OrderIntent


def iso_week(now: datetime) -> str:
    year, week, _day = now.isocalendar()
    return f"{year}-W{week:02d}"


class DcaWeekly:
    name = "dca_weekly"

    def __init__(self, settings: Settings):
        self.settings = settings

    def initial_state(self) -> dict:
        return {"week_fills": {}, "attempt_date": None}

    def decide(
        self,
        view: MarketSnapshot,
        state: dict,
        positions: dict,
        cash: Decimal,
        equity: Decimal,
        now: datetime,
    ) -> tuple[list[OrderIntent], dict, str]:
        del view, positions, cash, equity
        week = iso_week(now)
        today = now.date().isoformat()
        done = set((state.get("week_fills") or {}).get(week, []))
        if set(self.settings.symbols) <= done:
            return [], state, "already_bought_this_week"
        if state.get("attempt_date") == today:
            return [], state, "waiting_next_day"
        pending = [symbol for symbol in self.settings.symbols if symbol not in done]
        orders = [
            OrderIntent(
                symbol=symbol,
                side="buy",
                reason=f"dca {week}",
                quote_amount=self.settings.dca_notional,
            )
            for symbol in pending
        ]
        return orders, {**state, "attempt_date": today}, "weekly_buy"

    def commit(self, state: dict, fills: list[Fill], positions: dict, now: datetime) -> dict:
        del positions
        week = iso_week(now)
        bought = list((state.get("week_fills") or {}).get(week, []))
        for fill in fills:
            if fill.side == "buy" and fill.symbol not in bought:
                bought.append(fill.symbol)
        week_fills = dict(state.get("week_fills") or {})
        week_fills[week] = bought
        keep = sorted(week_fills)[-12:]
        updated = dict(state)
        updated["week_fills"] = {key: week_fills[key] for key in keep}
        return updated
