"""Market and order value types. Strategies emit intents; they do not trade."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str


@dataclass(frozen=True)
class Quote:
    symbol: str
    ts: datetime
    mid: Decimal
    source: str
    bid: Decimal | None = None
    ask: Decimal | None = None
    # When True, bid/ask already include the venue spread. The paper fill
    # uses them as-is and does not add cost_per_side again.
    spread_included: bool = False


@dataclass(frozen=True)
class MarketSnapshot:
    bars: dict[str, list[Bar]]
    quotes: dict[str, Quote]
    source: str


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    reason: str
    quote_amount: Decimal | None = None
    base_quantity: Decimal | None = None


@dataclass(frozen=True)
class Fill:
    sleeve: str
    symbol: str
    side: str
    qty: Decimal
    qty_delta: Decimal
    mid: Decimal
    fill_price: Decimal
    cash_delta: Decimal
    cost: Decimal
    notional: Decimal
    ts: datetime
    client_order_id: str
    reason: str

    def event_payload(self) -> dict:
        return {
            "sleeve": self.sleeve,
            "symbol": self.symbol,
            "side": self.side,
            "qty": format(self.qty, "f"),
            "fill_price": format(self.fill_price, "f"),
            "mid": format(self.mid, "f"),
            "cash_delta": format(self.cash_delta, "f"),
            "cost": format(self.cost, "f"),
            "notional": format(self.notional, "f"),
            "client_order_id": self.client_order_id,
            "reason": self.reason,
        }
