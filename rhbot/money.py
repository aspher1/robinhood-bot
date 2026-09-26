"""Decimal helpers. Money is never a binary float."""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

Q8 = Decimal("0.00000001")
CENT = Decimal("0.01")


def D(value: object) -> Decimal:
    """Parse a config or ledger value. Floats are refused so YAML cannot sneak in."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise ConfigFloatError(value)
    return Decimal(str(value))


def api_decimal(value: object) -> Decimal:
    """Parse a number from a JSON API payload, which may be a JSON float."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        raise ValueError(f"bad number {value!r}")
    return Decimal(str(value))


class ConfigFloatError(ValueError):
    def __init__(self, value: float):
        super().__init__(
            f"refusing binary float {value!r}; write decimals as strings in YAML"
        )


def q8(value: Decimal) -> Decimal:
    return D(value).quantize(Q8, rounding=ROUND_DOWN)


def q_price(value: Decimal) -> Decimal:
    return D(value).quantize(Q8, rounding=ROUND_HALF_UP)


def q_cent(value: Decimal) -> Decimal:
    return D(value).quantize(CENT, rounding=ROUND_DOWN)


def money_str(value: Decimal) -> str:
    return format(q8(value), "f")


def canonical(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
