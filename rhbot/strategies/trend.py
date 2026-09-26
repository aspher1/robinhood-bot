"""Hold when the daily close is above a simple moving average, else cash.

The window, band, and minimum hold were chosen before any backtest.
They are not searched or picked because they looked best.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from rhbot.config import FROZEN_SMA_WINDOW, FROZEN_STARTING_CASH, FROZEN_SYMBOLS, FROZEN_TREND_BAND, Settings
from rhbot.models import Bar, Fill, MarketSnapshot, OrderIntent
from rhbot.money import q_cent


def closed_bars(bars: list[Bar], now: datetime) -> list[Bar]:
    cutoff = now - timedelta(days=1)
    return sorted((bar for bar in bars if bar.ts <= cutoff), key=lambda bar: bar.ts)


def has_latest_closed_bar(bars: list[Bar], now: datetime) -> bool:
    """True when yesterday's daily bar is present. A missing one is stale."""
    closed = closed_bars(bars, now)
    if not closed:
        return False
    expected = (now.astimezone(timezone.utc) - timedelta(days=1)).date()
    latest = closed[-1].ts.astimezone(timezone.utc).date()
    return latest >= expected


class TrendDaily:
    name = "trend_daily"

    def __init__(self, settings: Settings):
        self.settings = settings

    def initial_state(self) -> dict:
        return {
            "last_decision_date": None,
            "evaluated_on": {},
            "holding_since": {},
        }

    def decide(
        self,
        view: MarketSnapshot,
        state: dict,
        positions: dict,
        cash: Decimal,
        equity: Decimal,
        now: datetime,
    ) -> tuple[list[OrderIntent], dict, str]:
        today = now.astimezone(timezone.utc).date().isoformat()
        evaluated = dict(state.get("evaluated_on") or {})
        orders: list[OrderIntent] = []
        notes: list[str] = []
        for symbol in FROZEN_SYMBOLS:
            if evaluated.get(symbol) == today:
                notes.append(f"{symbol}:already_decided_today")
                continue
            series = view.bars.get(symbol, [])
            closed = closed_bars(series, now)
            if not has_latest_closed_bar(series, now):
                notes.append(f"{symbol}:stale_candles")
                continue
            if len(closed) < FROZEN_SMA_WINDOW:
                notes.append(f"{symbol}:insufficient_history")
                continue
            window = closed[-FROZEN_SMA_WINDOW :]
            sma = sum((bar.close for bar in window), Decimal(0)) / Decimal(len(window))
            last = window[-1].close
            upper = sma * (Decimal(1) + FROZEN_TREND_BAND)
            lower = sma * (Decimal(1) - FROZEN_TREND_BAND)
            qty = positions.get(symbol, Decimal(0))
            in_pos = qty > 0
            if last > upper:
                want_long = True
            elif last < lower:
                want_long = False
            else:
                want_long = in_pos
            if want_long == in_pos:
                evaluated[symbol] = today
                notes.append(f"{symbol}:hold")
                continue
            if in_pos and not want_long:
                held = (state.get("holding_since") or {}).get(symbol)
                if held is not None and self._days(held, today) < self.settings.min_hold_days:
                    # Not a finished decision. A later cycle the same day may exit.
                    notes.append(f"{symbol}:min_hold")
                    continue
                evaluated[symbol] = today
                orders.append(
                    OrderIntent(
                        symbol=symbol,
                        side="sell",
                        reason="trend_exit",
                        base_quantity=qty,
                    )
                )
                notes.append(f"{symbol}:exit")
                continue
            # One sleeve per coin: half the book, which is the full cash of that sleeve.
            sleeve_cash = q_cent(FROZEN_STARTING_CASH / Decimal(len(FROZEN_SYMBOLS)))
            buy_amount = min(sleeve_cash, q_cent(cash))
            evaluated[symbol] = today
            if buy_amount < self.settings.min_order_notional:
                notes.append(f"{symbol}:below_min")
                continue
            orders.append(
                OrderIntent(
                    symbol=symbol,
                    side="buy",
                    reason="trend_entry",
                    quote_amount=buy_amount,
                )
            )
            notes.append(f"{symbol}:enter")
        updated = dict(state)
        updated["evaluated_on"] = evaluated
        if evaluated and all(evaluated.get(symbol) == today for symbol in FROZEN_SYMBOLS):
            updated["last_decision_date"] = today
        return orders, updated, ";".join(notes) or "no_trade"

    def commit(self, state: dict, fills: list[Fill], positions: dict, now: datetime) -> dict:
        del fills
        today = now.astimezone(timezone.utc).date().isoformat()
        holding = dict(state.get("holding_since") or {})
        for symbol in FROZEN_SYMBOLS:
            qty = positions.get(symbol, Decimal(0))
            if qty > 0:
                holding.setdefault(symbol, today)
            else:
                holding.pop(symbol, None)
        updated = dict(state)
        updated["holding_since"] = holding
        return updated

    @staticmethod
    def _days(start: str, today: str) -> int:
        from datetime import date

        return (date.fromisoformat(today) - date.fromisoformat(start)).days
