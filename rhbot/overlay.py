"""Per-book Option B overlay. Paper only.

Reported drawdown is mark-to-bid equity divided by the all-time peak since
paper day 1, minus 1. That peak only rises. It applies to trend_daily and
dca_weekly. buy_and_hold is never frozen, killed, or flattened by this overlay.

After a human restart from a −40% shutoff, the 10% pause and the 40% shutoff
use max(restart_baseline, the highest equity since that restart). A new
all-time high above the old peak makes that reference the peak again.
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


def trigger_reference(
    ath_peak: Decimal,
    equity: Decimal,
    restart_baseline: Decimal,
    restart_high: Decimal,
) -> tuple[Decimal, Decimal]:
    """Reference for the 10% and 40% lines, and the high since a restart.

    The all-time peak is not rewritten here. Before a human restart the
    reference is that peak. Afterward it is the greater of the restart
    baseline and the highest equity since the restart. When equity prints a
    new all-time high, that high is the reference, which is the peak again.
    """
    if restart_baseline <= 0:
        return ath_peak, restart_high
    high = restart_baseline if restart_high <= 0 else restart_high
    if equity > high:
        high = equity
    return max(restart_baseline, high), high


def _cell(row, key: str) -> str:
    keys = row.keys() if hasattr(row, "keys") else ()
    if key not in keys:
        return ""
    value = row[key]
    return "" if value is None else str(value)


def active_drawdown(row) -> Decimal:
    """Drawdown that arms the 10% pause and the 40% shutoff.

    Reported drawdown stays on the all-time peak. This one uses the restart
    baseline once a human restart has recorded it.
    """
    equity = D(row["equity"] or "0")
    ath = D(row["peak"] or "0")
    baseline = D(_cell(row, "restart_baseline") or "0")
    high = D(_cell(row, "restart_high") or "0")
    reference, _high = trigger_reference(ath, equity, baseline, high)
    return signed_drawdown(equity, reference)


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
