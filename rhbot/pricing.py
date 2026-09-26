"""Fill prices for the paper book.

Every fill costs at least ``cost_per_side`` (hard floor 1% per side).
Public mids are marked up by that rate. A venue bid or ask that is already
wider is used as-is. A tighter inclusive quote is widened to the floor.
"""

from __future__ import annotations

from decimal import Decimal

from rhbot.errors import DataError
from rhbot.models import OrderIntent, Quote
from rhbot.money import D, q8, q_price


def execution_price(quote: Quote, side: str, cost_per_side: Decimal) -> Decimal:
    if side not in ("buy", "sell"):
        raise DataError(f"unknown side {side}")
    if quote.mid <= 0:
        raise DataError("mid must be positive")
    cost = D(cost_per_side)
    if cost < Decimal("0.01"):
        raise DataError("cost_per_side must be at least 0.01")
    factor = (Decimal(1) + cost) if side == "buy" else (Decimal(1) - cost)
    if factor <= 0:
        raise DataError("cost_per_side leaves a non-positive sell price")
    floored = q_price(quote.mid * factor)
    if quote.spread_included:
        raw = quote.ask if side == "buy" else quote.bid
        if raw is None or raw <= 0:
            raise DataError("spread-included quote is missing a bid or ask")
        # A wider venue spread is a real cost. A tighter one is raised to the floor.
        if side == "buy":
            return q_price(max(raw, floored))
        return q_price(min(raw, floored))
    return floored


def plan_fill(
    intent: OrderIntent,
    quote: Quote,
    cost_per_side: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    """Return qty, fill_price, cash_delta, cost, notional_at_mid.

    Buys spend ``qty * fill_price`` (at most the requested quote amount).
    Sells receive ``qty * fill_price``. Cost is the gap versus the mid.
    """
    if quote.symbol != intent.symbol:
        raise DataError("quote symbol does not match the intent")
    px = execution_price(quote, intent.side, cost_per_side)
    if intent.side == "buy":
        if intent.quote_amount is None or intent.quote_amount <= 0:
            raise DataError("buy requires a positive quote_amount")
        qty = q8(intent.quote_amount / px)
        if qty <= 0:
            raise DataError("buy quantity rounded to zero")
        spent = q8(qty * px)
        cash_delta = -spent
        cost = q8(spent - q8(qty * quote.mid))
        if cost < 0:
            cost = Decimal(0)
        notional = q8(qty * quote.mid)
        return qty, px, cash_delta, cost, notional
    if intent.base_quantity is None or intent.base_quantity <= 0:
        raise DataError("sell requires a positive base_quantity")
    qty = q8(intent.base_quantity)
    proceeds = q8(qty * px)
    cash_delta = proceeds
    cost = q8(q8(qty * quote.mid) - proceeds)
    if cost < 0:
        cost = Decimal(0)
    notional = q8(qty * quote.mid)
    return qty, px, cash_delta, cost, notional
