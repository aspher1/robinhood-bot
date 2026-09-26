"""Per-book Option B overlay. Paper only.

Drawdown is mark-to-bid equity divided by the running peak since paper day 1,
minus 1. It applies to trend_daily and dca_weekly. buy_and_hold is never
frozen, killed, or flattened by this overlay.
"""

from __future__ import annotations

from decimal import Decimal

from rhbot.models import Quote
from rhbot.money import D, q8

OVERLAY_BOOKS = ("trend_daily", "dca_weekly")
SHADOW_NAMES = {
    "trend_daily": "trend_daily_shadow",
    "dca_weekly": "dca_weekly_shadow",
}


def signed_drawdown(equity: Decimal, peak: Decimal) -> Decimal:
    """Equity / peak − 1. Zero when there is no peak yet."""
    if peak <= 0:
        return Decimal(0)
    return equity / peak - Decimal(1)


def unit_mark_to_bid(quote: Quote, cost_per_side: Decimal) -> Decimal:
    """Mid haircut by the cost floor, or the venue bid when that is lower."""
    haircut = quote.mid * (Decimal(1) - D(cost_per_side))
    if quote.bid is not None and quote.bid > 0 and quote.bid < haircut:
        return quote.bid
    return haircut


def mark_to_bid_equity(
    cash: Decimal,
    positions: dict[str, Decimal],
    quotes: dict[str, Quote],
    cost_per_side: Decimal,
) -> Decimal:
    equity = cash
    for symbol, qty in positions.items():
        quote = quotes.get(symbol)
        if quote is None:
            raise RuntimeError(f"no quote to mark {symbol}")
        equity += qty * unit_mark_to_bid(quote, cost_per_side)
    return q8(equity)
