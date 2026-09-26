"""Hold when the daily close is above a simple moving average, else cash.

The window, band, and minimum hold were chosen before any backtest.
They are not searched or picked because they looked best.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from rhbot.config import FROZEN_SMA_WINDOW, FROZEN_STARTING_CASH, FROZEN_SYMBOLS, FROZEN_TREND_BAND, Settings
from rhbot.errors import DataError
from rhbot.models import Bar, Fill, MarketSnapshot, OrderIntent, Quote
from rhbot.money import D, q8, q_cent
from rhbot.overlay import mark_to_bid_equity
from rhbot.pricing import plan_fill
from rhbot.risk import EPS


def closed_bars(bars: list[Bar], now: datetime) -> list[Bar]:
    cutoff = now - timedelta(days=1)
    return sorted((bar for bar in bars if bar.ts <= cutoff), key=lambda bar: bar.ts)


def initial_sleeve_cash() -> dict[str, str]:
    """Half the trend book for each coin. Losses stay on that coin."""
    half = q8(FROZEN_STARTING_CASH / Decimal(len(FROZEN_SYMBOLS)))
    return {symbol: format(half, "f") for symbol in FROZEN_SYMBOLS}


def sleeve_cash_map(state: dict) -> dict[str, Decimal]:
    raw = state.get("sleeve_cash")
    if not isinstance(raw, dict):
        raw = initial_sleeve_cash()
    out: dict[str, Decimal] = {}
    for symbol in FROZEN_SYMBOLS:
        try:
            out[symbol] = D(raw.get(symbol, "0"))
        except (TypeError, ValueError):
            out[symbol] = Decimal(0)
    return out


def _exposure_after(symbol: str, added: Decimal, positions: dict, quotes: dict[str, Quote]) -> Decimal | None:
    total = Decimal(0)
    for name in set(positions) | {symbol}:
        quote = quotes.get(name)
        if quote is None:
            return None
        qty = positions.get(name, Decimal(0))
        if name == symbol:
            qty += added
        total += qty * quote.mid
    return total


def order_fits_caps(
    symbol: str,
    amount: Decimal,
    *,
    sleeve_cash: Decimal,
    ledger_cash: Decimal,
    equity: Decimal,
    positions: dict,
    quotes: dict[str, Quote],
    turnover_today: Decimal,
    turnover_base: Decimal,
    settings: Settings,
) -> bool:
    """True when ``amount`` stays inside the existing buy caps, after the cost."""
    if amount <= 0:
        return False
    quote = quotes.get(symbol)
    if quote is None:
        return False
    if amount > sleeve_cash + EPS or amount > ledger_cash + EPS:
        return False
    turnover_cap = turnover_base * settings.max_daily_turnover_pct + EPS
    if turnover_today + amount > turnover_cap:
        return False
    intent = OrderIntent(symbol, "buy", "trend_entry", quote_amount=amount)
    try:
        qty, _px, _cash, _cost, notional = plan_fill(intent, quote, settings.cost_per_side)
    except DataError:
        return False
    trade_cap_pct = min(settings.max_position_pct, settings.max_total_exposure_pct)
    cap = equity * trade_cap_pct + EPS
    if notional > cap:
        return False
    if (positions.get(symbol, Decimal(0)) + qty) * quote.mid > cap:
        return False
    exposure = _exposure_after(symbol, qty, positions, quotes)
    if exposure is None:
        return False
    if exposure > equity * settings.max_total_exposure_pct + EPS:
        return False
    return True


def largest_entry_quote(
    symbol: str,
    *,
    sleeve_cash: Decimal,
    ledger_cash: Decimal,
    equity: Decimal,
    positions: dict,
    quotes: dict[str, Quote],
    turnover_today: Decimal,
    turnover_base: Decimal,
    settings: Settings,
) -> Decimal:
    """Largest cent size that fits this coin's cash and the existing caps."""
    ceiling = min(sleeve_cash, ledger_cash)
    if ceiling < settings.min_order_notional:
        return Decimal(0)
    hi = int(q_cent(ceiling) * 100)
    lo = int(settings.min_order_notional * 100)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        amount = Decimal(mid) / Decimal(100)
        if order_fits_caps(
            symbol,
            amount,
            sleeve_cash=sleeve_cash,
            ledger_cash=ledger_cash,
            equity=equity,
            positions=positions,
            quotes=quotes,
            turnover_today=turnover_today,
            turnover_base=turnover_base,
            settings=settings,
        ):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best == 0:
        return Decimal(0)
    amount = Decimal(best) / Decimal(100)
    while best > 0 and not order_fits_caps(
        symbol,
        amount,
        sleeve_cash=sleeve_cash,
        ledger_cash=ledger_cash,
        equity=equity,
        positions=positions,
        quotes=quotes,
        turnover_today=turnover_today,
        turnover_base=turnover_base,
        settings=settings,
    ):
        best -= 1
        amount = Decimal(best) / Decimal(100)
    return amount if best else Decimal(0)


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
            "sleeve_cash": initial_sleeve_cash(),
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
        opening_cash = sleeve_cash_map(state)
        work_sleeve = dict(opening_cash)
        work_cash = cash
        work_equity = equity
        work_positions = dict(positions)
        turnover = Decimal(0)
        # Same-day turnover room. The risk check measures quote against day-start equity.
        turnover_base = equity
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
            if view.quotes.get(symbol) is None:
                notes.append(f"{symbol}:missing_quote")
                continue
            buy_amount = largest_entry_quote(
                symbol,
                sleeve_cash=work_sleeve[symbol],
                ledger_cash=work_cash,
                equity=work_equity,
                positions=work_positions,
                quotes=view.quotes,
                turnover_today=turnover,
                turnover_base=turnover_base,
                settings=self.settings,
            )
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
            quote = view.quotes[symbol]
            qty, _px, cash_delta, _cost, notional = plan_fill(
                orders[-1], quote, self.settings.cost_per_side
            )
            work_cash = q8(work_cash + cash_delta)
            work_positions[symbol] = q8(work_positions.get(symbol, Decimal(0)) + qty)
            work_sleeve[symbol] = q8(work_sleeve[symbol] + cash_delta)
            turnover = q8(turnover + notional)
            work_equity = mark_to_bid_equity(
                work_cash, work_positions, view.quotes, self.settings.cost_per_side
            )
        updated = dict(state)
        updated["sleeve_cash"] = {
            symbol: format(q8(amount), "f") for symbol, amount in opening_cash.items()
        }
        updated["evaluated_on"] = evaluated
        if evaluated and all(evaluated.get(symbol) == today for symbol in FROZEN_SYMBOLS):
            updated["last_decision_date"] = today
        return orders, updated, ";".join(notes) or "no_trade"

    def commit(self, state: dict, fills: list[Fill], positions: dict, now: datetime) -> dict:
        today = now.astimezone(timezone.utc).date().isoformat()
        holding = dict(state.get("holding_since") or {})
        for symbol in FROZEN_SYMBOLS:
            qty = positions.get(symbol, Decimal(0))
            if qty > 0:
                holding.setdefault(symbol, today)
            else:
                holding.pop(symbol, None)
        cash_map = sleeve_cash_map(state)
        for fill in fills:
            if fill.symbol not in cash_map:
                continue
            cash_map[fill.symbol] = q8(cash_map[fill.symbol] + fill.cash_delta)
        updated = dict(state)
        updated["holding_since"] = holding
        updated["sleeve_cash"] = {symbol: format(q8(amount), "f") for symbol, amount in cash_map.items()}
        return updated

    @staticmethod
    def _days(start: str, today: str) -> int:
        from datetime import date

        return (date.fromisoformat(today) - date.fromisoformat(start)).days
