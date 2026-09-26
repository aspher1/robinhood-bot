"""Fill prices for the paper book.

Public mids are marked up by ``cost_per_side`` (default 1% per side).
Quotes that already include a venue spread are filled at that bid or ask
and are not marked up a second time.
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
    if quote.spread_included:
        raw = quote.ask if side == "buy" else quote.bid
        if raw is None or raw <= 0:
            raise DataError("spread-included quote is missing a bid or ask")
        return q_price(raw)
    cost = D(cost_per_side)
    if cost < 0:
        raise DataError("cost_per_side cannot be negative")
    factor = (Decimal(1) + cost) if side == "buy" else (Decimal(1) - cost)
    if factor <= 0:
        raise DataError("cost_per_side leaves a non-positive sell price")
    return q_price(quote.mid * factor)


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
