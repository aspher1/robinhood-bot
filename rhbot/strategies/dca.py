"""Weekly DCA schedule. The dollar amount is waiting on Randy.

Buys would be every 7 days from paper day 1, at the same UTC time.
The amount in the spec is below the $10 minimum, so this book stays disabled
until that decision. The schedule index is still computed and persisted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from rhbot.config import Settings
from rhbot.models import Fill, MarketSnapshot, OrderIntent
from rhbot.ledger import parse_ts

WEEK = timedelta(days=7)


def schedule_index(now: datetime, day1: datetime) -> int:
    """floor((now − day1) / 7 days). Day 1 is index 0, day 8 is index 1."""
    elapsed = ensure_aware(now) - ensure_aware(day1)
    if elapsed.total_seconds() < 0:
        return 0
    return int(elapsed.total_seconds() // WEEK.total_seconds())


def ensure_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return ts.astimezone(timezone.utc)


class DcaWeekly:
    name = "dca_weekly"

    def __init__(self, settings: Settings):
        self.settings = settings

    def initial_state(self) -> dict:
        return {"day1": None, "filled_indexes": [], "skipped_indexes": [], "seen_index": None}

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
        day1_raw = state.get("day1")
        if not day1_raw:
            day1 = ensure_aware(now)
            day1_raw = day1.isoformat()
        else:
            day1 = parse_ts(str(day1_raw))
        index = schedule_index(now, day1)
        updated = dict(state)
        updated["day1"] = day1_raw
        filled = {int(item) for item in (state.get("filled_indexes") or [])}
        skipped = {int(item) for item in (state.get("skipped_indexes") or [])}
        if index in filled or index in skipped:
            updated["seen_index"] = index
            return [], updated, "already_scheduled"
        # The amount is not approved. Do not emit an order.
        updated["seen_index"] = index
        return [], updated, "dca_amount_pending_owner_decision"

    def commit(self, state: dict, fills: list[Fill], positions: dict, now: datetime) -> dict:
        del positions
        updated = dict(state)
        day1_raw = updated.get("day1")
        if not day1_raw:
            return updated
        index = schedule_index(now, parse_ts(str(day1_raw)))
        if any(fill.side == "buy" for fill in fills):
            filled = [int(item) for item in (updated.get("filled_indexes") or [])]
            if index not in filled:
                filled.append(index)
            updated["filled_indexes"] = filled
        return updated
